# План работ от 12 ноября 2026: статусы PDF, PitStop, группировка, красочность, TIFF, PSD

Ветка интеграции: `Admin/perf-jobid-inspect-sborka` (репозиторий `DMIYTRO/fast-api-for-ubuntu`).
Production: `fastapi-app` (uvicorn :8000) запускается из основного checkout
`/home/ubuntu/v2-web-platform-FastApi` (ветка `main`); дубликаты процессов не запускаются.
После приёмки ветка вливается в `main` и прод перезапускается (`/deploy`).

## Исходный поток до выполнения плана

1. `POST /api/checks` → `RunCoordinator.submit` (очередь, один run за раз).
2. `BatchProcessor.iter_inspect_orders`: сканирование, разбор имени, ImageMagick `identify`
   (TIFF: страницы/слои/альфа), правила `resample_policy`, статус заказа
   `passed | warning | error | waiting_confirmation`.
3. `BatchProcessorAdapter.process_order`: `create_pdfs` (только для `passed`) → `PDF/<stem>.pdf`
   → PitStop (если включён) → превью ревизии `Previews/Final/<aggregate>/r0001`.
   Для `error` — только превью исходников и копия в `Troubles/<order>`; PitStop не запускался.
4. `_completed_order_status` сводит локальный статус и вердикт PitStop.
5. Оператор: «в печать» → `FileLifecycle.accept_for_print` (`PDF/Print`, `Processed`,
   `Previews/Processed`), «на доработку» → `Troubles/<order>`. Корректировка размеров —
   план Callas `size_correction` (`services/callas_fix.py`), принятый результат идёт в печать
   новой ревизией через повторную проверку и PitStop.

## Этапы и ветки

| Этап | Ветка / PR | Содержание |
| --- | --- | --- |
| 0 | коммиты в ветку интеграции | Починены устаревшие тесты (prepare-reject синхронный, экспорт слоёв в фоне), тест корпуса PDF пропускается без локальной папки, сетевой скрипт убран из pytest, иконка коррекции размера вместо выпадающего меню |
| 1 | `feat/pdf-status-folders` | `PDF/Print` (зелёный), `PDF/Rework` (требует доработки), `PDF/Failed` (красный); маршрутизация после повторной проверки исправленного PDF; диагностический PDF и PitStop для заказов с ошибками первого этапа; причина, если PitStop не состоялся |
| 2 | `feat/issue-grouping` | `services/issue_groups.py`, ключ `issue_groups` в DTO заказа, группировка PitStop и локальных замечаний по типу, страницы и число объектов в деталях; фронт `issueGroups.js`, `PitStopReport.vue` |
| 3 | `feat/ink-coverage` | `services/callas/ink_coverage.py`, `CallasClient.ink_coverage`, пороги в профиле (`ink_min_percent`/`ink_max_percent`, примеры 20 % / 250 %) и env, ключ `ink_coverage` в DTO, пиктограмма «Недолив»/«Перелив» в карточке |
| 4 | `feat/tiff-flatten-layers` | `core/tiff_flatten.py`: сведение видимых слоёв TIFF через ImageMagick, различие слоёв и страниц, поля `tiff_flattened` и счётчики слоёв в DTO файла, иконка в карточке |
| 5 | `docs/psd-support-research` | Исследование PSD: чтение/сведение/редактирование через ImageMagick, замеры по образцу `benchmarks/`, прототип `core/psd_flatten.py`, рекомендация по включению `.psd` |
| 6 | реализован в ветке интеграции | Единый журнал действий по заказу (проверка, коррекция, Callas fix, сведение TIFF, печать/возврат, шрифты/профили) для выдачи через API |

## Проверки на реальных инструментах

- ImageMagick 7.1.2 и Ghostscript доступны локально: сведение TIFF, сборка PDF и контрольный рендер проверяются по-настоящему в тестах.
- callas pdfToolbox: тестовый сервер `http://192.168.88.109:8765` доступен; `inkcoverage` включён. Боевые флаги `IMAGE_MAGIC_CALLAS_*` лежат в `/etc/default/fastapi-app-callas` (только root).
- PitStop Server: только через прод-конфигурацию (`/etc/default/fastapi-app-pitstop`), в тестах — фейки. Живой прогон делается новым запуском проверки после деплоя.

## Выполнение

Этапы 0–6 реализованы в ветке интеграции. Итоговое поведение и проверки описаны
в [отчёте](validation-2026-11-12-pdf-status.md),
[маршрутизации PDF](pdf-status-routing.md), [красочности](ink-coverage.md),
[исследовании TIFF/PSD](tiff-flatten-psd-research.md) и
[журнале действий](order-actions-api.md). PSD подготовлен как отдельный прототип;
приём PSD основным конвейером не включён. Деплой ещё не выполнялся.
