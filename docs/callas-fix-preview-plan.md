# Исправление Callas и превью «оригинал / результат / наложение»: план

Дата: 10 ноября 2026. Статус: план + тестовые материалы. Продолжает
[callas-preflight-plan.md](callas-preflight-plan.md), этап 5 (`fix`).

## 1. Цель

Для проблемного макета оператор выбирает процесс-план Callas, нажимает «Исправить» и видит
блок «До и после»: оригинал, результат и наложение. Принятие делает результат новой ревизией
печатного PDF; до принятия в печать уходит только исходный PDF. «Безопасная зона» не
дублируется: она уже рисуется локальным превью.

## 2. Тестовые материалы (скопированы в рабочую папку)

Папка `callas_checks_fixups/` в корне проекта (PDF в git не попадают: `*.pdf` в `.gitignore`).

| Файл | Что это |
| --- | --- |
| `profiles/size_correction.kfpx` | копия `коррекция размера макета.kfpx` с шары `soft/callas_checks_fixups` (оригинал на шаре не тронут; имя без кириллицы). Внутри: `QF_Resize`, `QF_Set_Working_Boxes`, `QF_Set_Final_Boxes`, `F_Generate_Fields`, `Set CropBox to MediaBox` |
| `samples/original.pdf` | заказ `09_NP_Bezlam_130_mel_gl_(99x210)_4-4_T1000_(34250-26146648)_offset-face`: 2 стр., MediaBox **104×204 мм**, по имени должно быть 99×210 + 2 мм вылета = **103×214 мм** |
| `samples/desktop_fix.pdf` | результат Desktop (файл `_fix` на шаре): 103×214, TrimBox 99×210, BleedBox +2 мм |
| `samples/desktop_plus.pdf` | файл `…offset-face+.pdf`: геометрия страниц совпадает с оригиналом |

### Что показал разбор (10 ноября 2026)

- Оригинал — векторный (956/1165 объектов на страницах, картинок нет), размер страницы неверный.
- В `desktop_fix.pdf` все элементы масштабированы неравномерно: ×0,990 по ширине, ×1,049 по
  высоте (разница около 5,9 %). **Это и есть задача плана** `QF_Resize`: привести макет к размеру
  заказа 99×210 + 2 мм вылета (103×214), растянув содержимое на новый размер. Решение принадлежит
  владельцу процесса, поэтому план включён в репозиторий как есть.
- Правило проекта «не растягивать ширину и высоту независимо» относится к автоматическим
  коррекциям растров в `resample_policy.py`. Исправление Callas запускает оператор вручную и
  принимает после просмотра, поэтому оно этому правилу не подчиняется. Наложение при этом
  показывает коэффициенты по осям (×0,990 / ×1,049), чтобы степень растяжения была видна до
  принятия; это справка, а не запрет.
- **Профиль чувствителен к имени документа** (скрипт `V_Extract_Size` читает размер из `…(99x210)…`).
  Проверено на VM 10 ноября 2026: CLI видит имя *входного* файла, а после первого изменяющего шага
  плана (у `коррекция размера макета` это «Set CropBox to MediaBox») — имя *выходного*. Служба callas
  называла их `input.pdf` и `result.pdf`, поэтому план не находил размер и ничего не растягивал.
  Исправлено в службе (см. раздел 8): оба файла называются по имени входа.
- Локальный pdfToolbox CLI в `tools/` без лицензии (Trial Expired), поэтому план проверяется только
  на сервере callas. Профиль должен лежать в каталоге профилей сервера (`Image Magic/`) — это
  делает владелец, агенты на сервер не пишут.

## 3. Ход PDF в проекте (где встраивается)

Сейчас: файлы заказа → локальные проверки и коррекция размера растров → сборка PDF
(`processing/batch_processor.py`, ревизия `r0001`) → PitStop → превью той же ревизии →
решение оператора. Ревизии уже есть: `current_pdf_revision`, `checked_revision`,
`Previews/Final/<aggregate>/r0001/`, а `order_workflow.py` уже создаёт следующую ревизию
для печатной сборки из слоёных TIFF.

Новое: на карточке заказа кнопка «Исправить» → задание `process` в режиме `fix` с выбранным планом →
результат в `r0002` (исходный `r0001` не трогается) → PitStop и превью для `r0002` →
сборка сравнения `r0002/compare/` → показ блока «До и после» → «Принять» или «Отклонить».

## 4. Контракты между частями

**A. Клиент (`services/callas/`).** `CallasClient.process(pdf, profile, mode, *, variables=None,
report=("xml",), cancel=None)` → `ProcessResult(status, exit_code, summary{errors,warnings,infos,
fixups_applied,fixups_failed}, output_pdf: Path|None, report_xml: Path|None)`. Разбор XML-отчёта:
`services/callas/report.py` → список находок (`rule` по названию, `severity`, `page`, `bbox`).
Флаг `IMAGE_MAGIC_CALLAS_PROCESS_ENABLED` (по умолчанию выключен).

**B. Сравнение (`core/fix_compare.py`).** `build_fix_comparison(original_pdf, fixed_pdf, out_dir,
*, page=1, dpi=100) -> FixComparison(original_image, result_image, overlay_image, summary)`.
`summary`: размеры страницы и TrimBox до/после в мм, смещение, `changed_percent` пикселей,
оценка масштаба по осям (если считается; для векторных PDF — по сопоставлению объектов) и флаг
`distortion` при заметно разных масштабах по x и y. Совмещение по центру TrimBox (если нет — MediaBox).
Наложение — оригинал и результат разными цветами поверх друг друга.

**C. Интерфейс (`frontend/src/components/FixComparison.vue`).** Вход: `comparison {status,
plan{key,title}, original_url, result_url, overlay_url, summary, decision}`. Выход: события
`accept`, `reject`, `retry`. Три плитки + подпись изменений + справка о масштабе по осям при `distortion`.
Подключается в `OrderCard.vue` под блоками PitStop. Методы в `services/api.js`:
`startCallasFix(orderId, planKey)`, `callasFixStatus(orderId)`, `decideCallasFix(orderId, decision)`.

**D. Интеграция (вторая волна, после A–C).** Каталог планов в `config/` (ключ → имя профиля на
сервере, направление, режим, нужна ли подтверждение); эндпоинты `GET /api/callas/plans`,
`POST /api/orders/{id}/callas/fix`, `GET .../callas/fix`, `POST .../callas/fix/decision`;
шаг в `OrderWorkflowService` (новая ревизия, PitStop, превью, сравнение); поле DTO и миграция
Alembic; события SSE; очистка job-папок.

## 5. Тестирование

1. Модульные: клиент на `httpx.MockTransport`, разбор XML на образце отчёта, сравнение на трёх
   файлах из `samples/` (ожидаем: на паре `original`/`desktop_fix` `distortion=true` и
   `changed_percent` > 0; на паре «файл сам с собой» — 0 и наложение пустое).
2. Интерфейс: `vitest` на `FixComparison.vue` с фикстурой контракта C.
3. Интеграционные (волна 2): фейковый адаптер вместо callas, реальное сравнение на `samples/`,
   проверка, что `r0001` не изменён и до принятия в печать уходит исходный PDF.
4. Живая проверка (вручную, не из pytest, после того как профиль окажется на сервере):
   одно задание `fix` на копии `original.pdf`, сверка результата с `desktop_fix.pdf`.
5. Регрессия: `.venv/bin/python -m pytest -q` и `npm test -- --run`, `npm run build`. Известные
   старые падения (3 теста) отмечаются в отчёте отдельно.

## 6. Агенты и владение файлами

Работают в отдельных git worktree, без коммитов; чужие файлы не правят; `docs/callas/CHANGES.md`
не трогают (записи сводит оркестратор).

| Агент | Файлы |
| --- | --- |
| agent-client | `services/callas/*` (client, report, `__init__`), поля `callas_process_*` в `server/settings.py`, `tests/test_callas_process_client.py`, `tests/test_callas_report.py` |
| agent-compare | `core/fix_compare.py`, `tests/test_fix_compare.py` |
| agent-ui | `frontend/src/components/FixComparison.vue` и тест, `frontend/src/services/api.js` (только новые методы), `OrderCard.vue` (только подключение блока) |
| orchestrator (волна 2) | планы в `config/`, эндпоинты в `control_panel.py`, `order_workflow.py`, `batch_adapter.py`, DTO, миграция |

## 7. Открытые вопросы

- Нужен ли порог допустимого растяжения (например, отклонять автоматически при отличии масштабов
  осей более N %) или решение всегда за оператором по наложению?
- Куда положить `.kfpx` на сервере (`Image Magic/`) и кто копирует.
- Принятое исправление блокирует печать до PitStop по `r0002`? Предложение: да, как у печатной
  сборки из TIFF.

## 8. Статус реализации (10 ноября 2026)

Сделано (PR #4):

- Клиент: `CallasClient.process` (analyze/fix), разбор XML-отчёта; флаг `IMAGE_MAGIC_CALLAS_PROCESS_ENABLED`.
- Сравнение: `core/fix_compare.py` (оригинал, результат, наложение, масштаб по осям).
- Каталог планов: `config/callas_plans.py`. Имя профиля `Image Magic/size_correction` **предположительное**:
  оно должно совпасть с именем файла в каталоге профилей сервера callas.
- Сервис `services/callas_fix.py`: запуск, состояние в `PDF/CallasFix/<имя PDF>/state.json` (переживает
  перезапуск; зависшее `processing` помечается как прерванное), результат `fixed.pdf`, картинки `compare/`.
  Исходный PDF не меняется. Решение «принять» отклоняется, если исходный PDF изменился после исправления.
- Ручки (`server/callas_fix_routes.py`): `GET /api/callas/plans`; `GET|POST /api/checks/{run}/orders/{order}/callas/fix`;
  `POST .../fix/decision`; `GET .../fix/files/{original|result|overlay|pdf}`. Без настроенного callas — 404
  `callas_fix_disabled`, интерфейс показывает «Исправление Callas недоступно». Состояние также приходит в
  данных заказа как `callas_fix`.
- Печать: принятое исправление копируется в `<имя>_callas-fixed.pdf` и идёт тем же путём, что PDF из просмотренных TIFF
  (проверка PDF, PitStop, новая ревизия, перенос в `PDF/Print`). Если TIFF-сборка тоже есть, приоритет у неё.
- Интерфейс: блок «До и после», кнопка «Исправить» (маршруты привязаны к запуску проверки).

Не сделано / не проверено:

- Живой запуск `fix` на сервере callas (нет профиля на сервере; формат отчёта и имя `fixed.pdf` взяты из документации).
- Сравнение строится по 1-й странице; для двустраничных заказов страницы 2 в блоке нет.
- Превью новой ревизии (`Previews/Final/.../r0002`) отдельно не строится: после печати используется текущий механизм.
- Подписанная в PR оговорка: в браузере блок не смотрели.

### Исправление службы callas (10 ноября 2026) и живая проверка

- `callas_service/process.py` (VM, `/opt/callas-service` и исходники `~/.local/bin/callas_tulbox`): вход и выход
  CLI называются по безопасному имени входного файла вместо `input.pdf` / `result.pdf`. Публикуемые клиенту
  имена результата не менялись. Тесты службы обновлены (контрактные снимки `process_*`, новый тест имён),
  весь набор: 822 passed; единственное падение — `test_example_file_is_valid` (нет файла-примера в неполной копии).
  Резервная копия: `/opt/callas-service/backup-20261110-doc-name/`. Откат: вернуть `process.py.orig`
  и `systemctl restart callas-worker callas-service`. Изменения в репозитории службы не закоммичены.
- Профиль `size_correction.kfpx` лежит на сервере как `Image Magic/size_correction`.
- Живой прогон через приложение (`CallasFixService`) на копии заказа 99×210: 104×204 → **103×214 мм**,
  TrimBox 99×210, масштаб ×0,99 по ширине и ×1,049 по высоте, исходник не изменён, рабочие папки на шаре убраны.
- Включение в production: `sudo scripts/enable_callas.sh fix` (добавляет `IMAGE_MAGIC_CALLAS_PROCESS_ENABLED=1`) после
  слияния PR в `main` и деплоя (`/deploy`).
