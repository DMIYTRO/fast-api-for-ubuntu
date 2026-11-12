# Журнал изменений интеграции callas (общий канал агентов)

Правила: каждый агент перед работой читает этот файл, после значимого шага ДОБАВЛЯЕТ запись в конец
(`## [агент] время — что сделано, публичный API, файлы, что нужно от других`). Чужие записи не редактировать.
Каждый агент правит только свои файлы (см. ниже). Секреты/токен не писать.

Контракт API (из docs/callas/openapi.json): PUT /jobs/{job_id} (202) с телом MergeSpec{type:"merge",inputs:[{path,sha256?}],output,optimize?,skip_corrupt=false}
или RenderSpec{type:"render",input,out_dir,dpi=150,format:png|webp}; GET /jobs/{id} -> status, files[{kind?,name,path,size,sha256}], error, bad_inputs;
DELETE /jobs/{id}; заголовки Authorization: Bearer <token>, X-Callas-User. Файлы задания: /mnt/shared/.callas/<job_id>/in и out.

Владение файлами:
- agent-client: services/callas/*, server/settings.py (только новые CALLAS_* поля), tests/test_callas_client.py
- agent-preview: core/preview_generator.py, core/preview_cache.py, services/return_preview.py, services/ftp_preview_uploader.py, control_panel.py (только MIME/поиск превью), tests/test_preview_*.py, test_return_*, test_ftp_*
Оркестратор (главный) подключает merge/render в processing/batch_processor.py и layered_tiff_print.py после готовности клиента.

---

## [agent-client] 2026-11-03 — этап B: настройки и клиент callas готовы (merge/render проверены вживую)
Файлы: `server/settings.py` (только поля `callas_*`), `services/callas/{__init__,client,errors}.py`, `tests/test_callas_client.py`, `scripts/callas_smoke.py` (явный живой smoke, не из pytest).
Тесты: `.venv/bin/python -m pytest tests/test_callas_client.py -q` -> 48 passed (fake HTTP через httpx.MockTransport; живых вызовов нет).

Настройки (env -> поле Settings; токен `repr=False`): `IMAGE_MAGIC_CALLAS_URL` (callas_url), `_TOKEN` (callas_token), `_SHARE` (callas_share, `/mnt/shared`),
`_SERVER_SHARE` (callas_server_share, `/mnt/shared` — как путь видит сервер callas), `_USER` (X-Callas-User, `image-magic`), `_MERGE_ENABLED` и `_RENDER_ENABLED` (оба False),
`_REQUEST_TIMEOUT_SECONDS` (30), `_JOB_TIMEOUT_SECONDS` (общий deadline, 600), `_POLL_INTERVAL_SECONDS` (1), `_MAX_CONCURRENT_JOBS` (2), `_MAX_RETRIES` (4).
Флаги enabled клиент сам не проверяет — их читает вызывающий код (оркестратор).

Публичный API (`from services.callas import ...`):
- `CallasClient.from_settings(settings)` / `CallasClient(CallasConfig(...), http_client=None)`; потокобезопасен, один экземпляр на приложение; `close()`.
- `merge(inputs, output_name, *, job_id, cancel: threading.Event|None=None, optimize=None) -> Path` — локальный путь `<share>/.callas/<job_id>/out/<output_name>` (уже проверен: путь, размер, sha256). Результат нужно скопировать вызывающему коду (клиент его не удаляет).
- `render(input, dpi=150, *, job_id, cancel=None, expected_pages=None) -> list[Path]` — PNG страниц `page-NNN.png`, отсортированы по номеру, нумерация без пропусков обязательна.
- `delete(job_id)` — DELETE задания (404 игнорируется); `job_dir(job_id)` — локальная папка задания для очистки; `deterministic_job_id(prefix, *parts)` — стабильный id (job_id задаёт вызывающий: `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`).
- Исключения (`CallasError` и потомки, сообщения по-русски, без токена): CallasConfigError, CallasAuthError (401/403), CallasConflictError (409), CallasValidationError (422), CallasNotFoundError, CallasUnavailableError (429/502/503/504/сеть после повторов), CallasTimeoutError, CallasCancelledError, CallasProtocolError (плохой JSON), CallasJobFailedError (failed/cancelled, поля status/error_class/bad_inputs; done с bad_inputs тоже отклоняется), CallasResultError (путь/размер/sha).
Поведение: входы копируются в `<share>/.callas/<job_id>/in/NNNN.<ext>` (имена индексные, через .part + replace, sha256 передаётся в InputFile; повтор с тем же содержимым не перекопирует) до PUT; `skip_corrupt=false`; backoff с джиттером на 429/502/503/504 и сетевых ошибках; Retry-After (секунды/HTTP-дата, мусор игнорируется); общий deadline на всё (семафор, PUT, polling, ожидание файла); при таймауте/отмене отправляется best-effort DELETE; потерянный ответ PUT -> повторный PUT, 409 после неоднозначного сбоя считается «задание уже принято»; 409 на первой попытке — ошибка. Семафор на `max_concurrent_jobs` (ожидание слота отменяемо). Автоматического DELETE после успеха нет (этап C решает, когда чистить).

Контракт путей, уточнённый вживую (важно для оркестратора):
- Все пути в теле PUT — АБСОЛЮТНЫЕ, как их видит сервер: `/mnt/shared/.callas/<job_id>/in/0001.pdf`. Относительное имя в `output` даёт HTTP 400 «Путь должен быть абсолютным: merged.pdf».
- merge: `output` = `/mnt/shared/.callas/<job_id>/out/<имя>.pdf` (клиент собирает сам из `output_name`); render: `out_dir` = `/mnt/shared/.callas/<job_id>/out`, `format: "png"`, `dpi` 36..1200.
- В ответе `files[].path` = `/mnt/shared/.callas/<job_id>/out/<name>` + `size` + `sha256`; render называет страницы `page-001.png`, `page-002.png`, …
- Папку `out` создаёт служба; приложение создаёт только `in`. Ответ GET неизвестного задания: 404; неверный токен: 401 `{"detail": ...}`; `/health` без токена.
- Мы и сервер видят одну шару под одним путём `/mnt/shared`, поэтому `SHARE == SERVER_SHARE`; если монтирование изменится — задать `IMAGE_MAGIC_CALLAS_SERVER_SHARE`.

Живой smoke (`scripts/callas_smoke.py`, запуск: `IMAGE_MAGIC_CALLAS_URL=http://192.168.88.109:8765 IMAGE_MAGIC_CALLAS_TOKEN=... .venv/bin/python scripts/callas_smoke.py [--keep]`, токен только из окружения):
- Первый запуск: PUT 400 (относительный output) — после исправления на абсолютный путь: merge двух PDF 200x300 pt -> OK за 2.5 с, 2 страницы, sha/размер совпали; render результата 72 dpi -> OK за 1.0 с, 2 PNG (page-001.png 4699 Б, page-002.png 4737 Б), файлы сразу видны на CIFS-шаре. Задержки видимости файла не наблюдалось (на всякий случай ожидание до 15 с).
- Служба принимает вход, скопированный приложением в `in/` (чтение сервером доказано). DELETE завершённого задания возвращает 200 и JSON задания, но папку `.callas/<job_id>` на шаре НЕ удаляет — очистка локально (`shutil.rmtree(client.job_dir(job_id))`) остаётся за вызывающим кодом. Тестовые папки smoke-* (мои) удалены с шары.
- Не проверено вживую: failed-задание/плохие входы, 429/503, большие файлы, сохранение OutputIntent/боксов/слоёв при merge.
Что нужно от других: оркестратор подключает `CallasClient` в batch_processor/layered_tiff_print, читая `settings.callas_merge_enabled/render_enabled`; чужие файлы (agent-preview) не затрагивались.

## [agent-preview] 2026-11-03 — этап F: вывод превью в JPEG (настраиваемо), чтение PNG и JPEG везде

Состояние по умолчанию: ПОВЕДЕНИЕ НЕ ИЗМЕНЕНО (PNG). JPEG включается оркестратором, см. ниже.

Публичный API (`core/preview_cache.py`):
- Настройки: `IMAGE_MAGIC_PREVIEW_FORMAT` = `png` (по умолчанию) | `jpeg`/`jpg`; `IMAGE_MAGIC_PREVIEW_JPEG_QUALITY` (1-100, по умолчанию 95). Неверное значение -> предупреждение и PNG/95.
- `default_preview_format() -> 'png'|'jpeg'`, `normalize_preview_format(v|None)` (ValueError на неизвестное), `preview_extension(fmt=None) -> '.jpg'|'.png'`,
  `with_preview_extension(path, fmt=None)`, `preview_format_for_path(path)`, `preview_jpeg_quality()`.
- `jpeg_output_arguments(quality=None)`: `-background white -alpha remove -alpha off -colorspace sRGB -sampling-factor 1x1 -quality 95` (4:4:4, белый фон; sRGB нужен, чтобы CMYK-исходник не дал CMYK-JPEG, который браузеры показывают неверно; PNG раньше тоже получал RGB неявно; исходники не меняются).
- Чтение: `PREVIEW_SUFFIXES=('.jpg','.jpeg','.png')`, `preview_variants(path_without_ext)` (существующие варианты, новейший первым; при равном mtime JPEG выигрывает), `preview_name_key(name)`, `is_preview_image(path)`, `preview_media_type(path)` (`image/jpeg` | `image/png`).
- `full_preview_cache_path(thumbnail)`: ключ по-прежнему sha256 абсолютного пути миниатюры; расширение кэша следует формату миниатюры (`.jpg` для JPEG-миниатюры, иначе `.png`) -> исторические `.png`-кэши читаются как раньше.

`core/preview_generator.generate_preview(...)`: формат вывода определяется СУФФИКСОМ `output_preview_path` (`.jpg/.jpeg` -> JPEG, иначе PNG); новые необязательные аргументы `preview_format=None` (утверждение: при несовпадении с суффиксом ValueError) и `jpeg_quality=None`. `full_preview_path` обязан иметь тот же формат (иначе ValueError); `full_preview_cache_path(thumb)` это обеспечивает автоматически. Размеры прежние: 480 / 1800 (`>`). Возвращает `output_preview_path`, как раньше.

Что нужно от оркестратора (processing/batch_processor.py — не моё): чтобы включить JPEG, в `generate_pdf_previews` (стр. ~787-791) и `generate_previews_for_files` (стр. ~887) заменить жёстко заданные `"_preview.png"` на `f"..._preview{preview_extension()}"` (импорт из `core.preview_cache`), затем выставить `IMAGE_MAGIC_PREVIEW_FORMAT=jpeg`. Промежуточные `page-*.png` рендера остаются PNG (это вход, не вывод). `core/report_builder.py:312` (HTML-отчёт CLI) ищет `{stem}_preview.png` — не моё; при включении JPEG использовать `preview_variants(preview_dir / f"{stem}_preview")`. `services/layered_tiff_print.py`/`control_panel.py` `_layered-composite.png` остаётся PNG (не менялось; потребители ниже читают и `.jpg`-вариант).

Потребители:
- `services/return_preview.py`: поиск/regex теперь `_preview.(jpg|jpeg|png)`; face/back определяются по имени как раньше, face слева; пары PNG+JPEG одного превью схлопываются в новейшее; `_layered-composite.(jpg|png)` тоже читается. Коллаж `create_return_preview_collage/_sheet(..., output_format=None)` пишет `<order>_return-preview.<jpg|png>` по `IMAGE_MAGIC_PREVIEW_FORMAT` (по умолчанию PNG; при JPEG — JPEG с теми же параметрами). ВНИМАНИЕ: JPEG-коллаж уходит в Sborka по FTP; убедиться, что Sborka принимает `.jpg` (custom-превью `.jpg` уже допускались).
- `services/ftp_preview_uploader.py`: кода менять не потребовалось (работает по именам, не по расширению; regex там нет). Добавлен тест для .jpg+.png.
- `control_panel.py` (только превью): `/api/files/{id}/preview` отдаёт `media_type` по расширению (`image/jpeg`/`image/png`) для thumbnail и full; `_safe_rework_history_preview_path` ищет `*_return-preview.jpg|.png` (новейший). Остальное не тронуто.
- `services/file_lifecycle.py`: НЕ менялся — перемещение превью идёт по `preview.name`, от расширения не зависит. Известное существующее ограничение: ключ full-кэша привязан к абсолютному пути миниатюры, поэтому после переноса в `Previews/Processed` full-кэш по новому пути не находится (было и для PNG); «устойчивый идентификатор кэша/связь с SHA PDF» из этапа F не делал — нужна запись в БД/DTO, вне моих файлов.

Тесты: добавлены в `tests/test_preview_cache.py`, `test_preview_generator.py` (реальный magick: JPEG 480/1800, 4:4:4, quality>=94, белый фон на прозрачном, CMYK->sRGB, PNG по-прежнему работает), `test_return_preview_collage.py` (JPEG, смесь PNG+JPEG, схлопывание близнецов, layered .jpg, порядок страниц), `test_ftp_preview_uploader.py`, новый `tests/test_preview_formats_api.py` (MIME и кэш для .jpg/.png, история rework).
Результат `.venv/bin/python -m pytest -q`: 291 passed, 3 failed — все 3 не из моих файлов и воспроизводятся без моих правок: `test_control_panel.py::test_layered_tiff_export_is_separate_and_scoped_to_order`, `test_control_panel.py::test_order_files_and_prepared_actions_round_trip_through_api` (проверено на чистом дереве через git stash), `test_pdf_batch_validation.py::test_real_pdf_fixture_has_expected_one_and_two_page_documents` (0 заказов из фикстуры вместо 9; не проверял на чистом дереве).
Не сделано: визуальное сравнение качества на реальных контрольных файлах (quality 95/4:4:4 — стартовый кандидат плана), UI-тесты.

## [orchestrator] 2026-11-03 — этапы D+F подключены
- `processing/batch_processor.py`: kwargs `callas_client, callas_merge, callas_render`; `_merge_page_pdfs` (callas merge или PyMuPDF), `_render_pdf_with_callas` (вместо gs при включении); имена превью через `preview_extension()`; job-папки чистятся после копирования результата.
- `control_panel.py`: `_callas_processor_factory(settings)` подключён в обычную и PitStop-фабрики. `core/report_builder.py` понимает `_preview.jpg`.
- Включение: `IMAGE_MAGIC_CALLAS_URL/TOKEN`, `IMAGE_MAGIC_CALLAS_MERGE_ENABLED=1`, `IMAGE_MAGIC_CALLAS_RENDER_ENABLED=1`, `IMAGE_MAGIC_PREVIEW_FORMAT=jpeg`.
- Живой прогон `scripts/callas_e2e.py`: face/back TIFF -> PDF 2 стр. через callas -> 2 JPEG 480x276 sRGB — OK.
- Тесты: 291 passed + новые; 3 падения старые (test_control_panel x2, test_pdf_batch_validation), на чистом дереве воспроизводятся.
- Не сделано: этап E (layered_tiff_print merge), учёт операций в БД (этап C), PDF-boxes/OutputIntent сравнение, визуальная приёмка JPEG, деплой.

## [orchestrator] 2026-11-03 — этап E (reviewed layered PDF)
- `services/callas/merger.py`: `make_callas_pdf_merger(client)`; `build_reviewed_layered_print_pdf(..., pdf_merger=None)` — финальная склейка частей через merger, иначе PyMuPDF; число страниц результата проверяется; части по-прежнему извлекаются локально.
- `OrderWorkflowService(pdf_merger=...)`, `control_panel._callas_pdf_merger(settings)` — включается тем же флагом `IMAGE_MAGIC_CALLAS_MERGE_ENABLED`.
- Тесты `tests/test_layered_print_merge.py`; живая проверка адаптера против callas — OK. Нужна перезагрузка fastapi-app.

## [docs] 2026-11-10 — справочник по новым типам заданий службы (visualizer, separations, inkcoverage)
- Добавлен `docs/callas/visualizer-separations-inkcoverage.md`: запросы/ответы, допустимые значения полей и `parts`, права клиента (`allowed_types`), ошибки, лимиты, результаты экспериментов на CLI 17.0.682. Источник — отчёт службы от 10 ноября 2026.
- Новые поля `render` (`simulate_overprint`, `simulate_paper`, `blackpoint_compensation`, `pagebox`) необязательны; при значениях по умолчанию запрос и ответ прежние.
- Новые типы на стороне службы выключены по умолчанию (`CALLAS_ENABLED_TYPES`). Клиент `services/callas` и приложение их не вызывают: код не менялся, поддерживаются только `merge` и `render`.
- `docs/callas/openapi.json` не обновлялся (снимок содержит `merge`, `render`, `process`); актуальную схему брать из `/openapi.json` службы.

## [claude] 2026-11-10 — сверка сервера, новый контракт, план проверки и исправления
- `docs/callas/openapi.json` заменён снимком с сервера: добавлены `InfoSpec`, `SplitSpec`, `VisualizerSpec`, `SeparationsSpec`, `InkCoverageSpec`. Абзац «Контракт API» в начале этого файла описывает только `merge`/`render` и устарел.
- На сервере включены `process`, `visualizer`, `separations`, `inkcoverage`; клиенту `image-magic` разрешены все профили и типы. Живая проверка трёх типов на копии реального заказа — OK (1,0–1,5 с на задание).
- Подробности `process` — в XML-отчёте (`hits`/`hit` с правилом, страницей, координатами); `rule_id` порядковый, устойчивый ключ — название правила.
- Новый план: `docs/callas-preflight-plan.md`. Код приложения не менялся; в клиенте по-прежнему только `merge` и `render`.
- Нужно от владельца: собственные `.kfpx` в каталог профилей сервера; решение о включении callas в production.

## [claude] 2026-11-10 — исправление `process`/fix: имя документа для профилей, клиент и служба
- Причина «профиль не растягивает»: pdfToolbox видел имя документа `input.pdf`/`result.pdf`, а профиль `size_correction` берёт размер из имени заказа. Служба callas теперь называет вход и выход по имени входного файла (`process.py`, тесты службы обновлены); клиент: `CallasClient.process(..., keep_name=True)` сохраняет безопасное имя заказа при отправке (кириллическая «х»/«×» в `(99х210)` приводится к «x»).
- Приложение: каталог планов `config/callas_plans.py`, `services/callas_fix.py`, ручки `/api/checks/{run}/orders/{order}/callas/fix*`, шаг печати из принятого исправления, интерфейс «До и после». Включение: `IMAGE_MAGIC_CALLAS_PROCESS_ENABLED=1` (`sudo scripts/enable_callas.sh fix`).
- Профиль на сервере: `Image Magic/size_correction`. Живая проверка пройдена (см. docs/callas-fix-preview-plan.md, раздел 8).
