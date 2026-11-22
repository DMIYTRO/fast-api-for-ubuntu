# Журнал заказа (order journal)

Полная история заказа одним документом: что найдено в файлах, что исправлено
автоматически, что сделал оператор и какое решение принято. Показывается на
странице «История заказов» (кнопка «Журнал» у каждой записи) и предназначен для
передачи во внешнюю API.

## Эндпоинты (нужна авторизация)

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/order-history/{action_id}/journal` | журнал заказа записи истории (`order_actions.id`) |
| `GET` | `/api/checks/{run_id}/orders/{order_id}/journal` | журнал заказа запуска; `order_id` — `клиент:заказ` или номер заказа |

Ошибки: `404 history_not_found` / `run_not_found` / `order_not_found`;
`409 ambiguous_order`, если голый номер заказа есть у разных клиентов (используйте
`клиент:заказ`).

Журнал считается на лету (таблиц и миграций нет) из итогового состояния заказа,
событий `run_events` + `archived_run_events`, `order_actions` и `pdf_revisions`.
Построитель — чистая функция `services/order_journal.py:build_order_journal`,
загрузка — `services/order_journal_loader.py` (число SQL-запросов не зависит от
размера запуска).

## Схема (`schema = "image-magic.order-journal"`, `version = 1`)

```json
{
  "schema": "image-magic.order-journal",
  "version": 1,
  "run_id": "0eec7d46…",
  "order_id": "1001",
  "customer_id": "42",
  "aggregate_id": "42:1001",
  "status": "passed",
  "check_policy": {"key": "basic", "label": "Облегчённая проверка (Input)", "reason": "…"},
  "pdf_revision": 2,
  "generated_at": "2026-11-21T12:00:00+00:00",
  "summary": {"issues": 4, "resolved_issues": 1, "auto_changes": 3, "operator_changes": 2, "decisions": 1},
  "entries": [ … ]
}
```

Каждая запись `entries[]` всегда содержит все ключи:

| Поле | Тип | Смысл |
|---|---|---|
| `kind` | `issue` \| `auto_change` \| `operator_change` \| `decision` | раздел |
| `code` | строка | машинный код (таблица ниже) |
| `source` | `pitstop` \| `callas` \| `local` \| `operator` \| `system` | кто нашёл / сделал |
| `severity` | `error` \| `warning` \| `info` \| `null` | уровень (для изменений обычно `null`; сбой — `warning`) |
| `title` | строка | русский текст для человека |
| `detail` | строка \| `null` | подробности (детали коррекции, комментарий, ошибка) |
| `file` | строка \| `null` | имя файла (без пути) |
| `pages` | `int[]` \| `null` | страницы |
| `count` | `int` \| `null` | число повторов / объектов |
| `at` | ISO-8601 \| `null` | время (UTC); для находок — время проверки |
| `outcome` | строка \| `null` | `resolved`, `accepted`, `rejected`, `failed`, `ready`, `processing`, `converted`, `applied`, `skipped`, `reset`, `done`, `rolled_back`, статус ревизии `passed`/`warning`/`error` |
| `data` | объект | сырые полезные поля; ключи со значением `null` опускаются |

Порядок: находки (ошибки → предупреждения → инфо, устранённые в конце),
автоизменения, действия оператора (по времени), решения (по времени).

Гарантии: абсолютных путей нет ни в одном поле (в тексте ошибок путь заменяется
именем файла), сетевые ответы Sborka не копируются. Новые коды и ключи `data`
добавляются без смены версии; клиент должен игнорировать незнакомые.

## Коды

### Находки (`kind: "issue"`)

| Код | Источник | Что это |
|---|---|---|
| `pitstop:<action_id>` | pitstop | группа находок PitStop по ActionID, напр. `pitstop:2079` (обтравка), `pitstop:3004` (прозрачность), `pitstop:1153` (суммарное покрытие > 310%), `pitstop:2078` (RGB/ICC), `pitstop:1150` (разрешение) |
| `pitstop:other` | pitstop | находка без ActionID (текст в `title`, ключ в `data.group_code`) |
| `local:<code>` | local | локальная проверка (`services/issue_groups.py`, `_LOCAL_RULES`): `low_dpi`, `size_mismatch`, `colorspace_not_allowed`, `tiff_layers`, `pdf_error`… |
| `local:other` | local | локальное сообщение вне таблицы (исходный текст в `title`) |
| `page_rule:<code>` | local | нарушение правила страниц (жёсткая ошибка): `pdf_too_many_pages`, `order_too_many_pages`, `two_page_pdf_one_sided`, `duplex_pages_required`, `two_page_pdf_extra_sides`, `multiple_two_page_pdf` |
| `prepress:black_solid` | callas | плашка 100% K выше порога (`data.share`, `data.page_shares`) |
| `prepress:black_solid_minor` / `prepress:black_manual` | callas | немного чистого K100 / крупное чёрное поле не из K100 |
| `prepress:high_ppi` / `prepress:one_bit_ppi` | callas | растры выше предела ppi / однобитные |
| `prepress:fonts` / `prepress:fonts_not_embedded` | callas | живые шрифты (`data.fonts`) / невстроенные шрифты |
| `prepress:overprint` | callas | overprint итогового PDF (профиль `Image Magic/Overprint - any object`). `severity: "error"` — overprint не только на технических красках, заказ красный в любой политике (`count`/`pages` — блокирующие объекты); `severity: "info"` — только технические краски (белила, лак, контур реза…), статус не меняется (`count`/`pages` — технические). `data`: `by_kind` (`process`, `white`, `spot`, `technical_spot`, `unknown`), `blocking_count`, `technical_count`, `technical_pages`, `colorants`, `technical_colorants`, `samples`, `technical_samples`, `profile` |
| `prepress:<check>:failed` | callas | проверка callas не выполнена (`detail` — ошибка); `prepress:overprint:failed` — с `severity: "warning"` (заказ не зелёный), остальные — `info` |
| `ink:overflow` / `ink:light_areas` / `ink:underflow` | callas | флаги красочности (перелив, светлые участки) |

Флаги находок PitStop в `data`: `policy_demoted` + `original_severity` — понижено
облегчённой политикой до инфо (статус не меняет); `added_by_fix` — находку целиком
добавило исправление Callas (в карточке скрыта, `original_severity` — уровень
PitStop); `introduced_by_fix` / `baseline_occurrences` — частично добавленная
(в `title` число вхождений клиента, как в карточке); `policy_unclassified` —
новая проверка без категории; `category`, `category_label`, `fix_plan`,
`objects`, `samples`, `files`.

Находка первой проверки (снимок события `order.completed`), которой нет в
итоговом состоянии, остаётся с `outcome: "resolved"` и `data.resolved: true`
(«После изменений не найдено») — например обтравка, убранная принятым
исправлением. Если итогового блока нет вовсе (PitStop не перепроверялся),
находка помечается `data.from_initial_check: true` без `resolved`.

### Автоматические изменения (`kind: "auto_change"`)

| Код | Что это |
|---|---|
| `auto:color_conversion:rgb` | RGB переведён в CMYK профилем callas (`data.profile`, `pages`) |
| `auto:icc_removed` | снят ICC-профиль (при RGB→CMYK или отдельно: `data.reason = "icc"`) |
| `auto:output_intent_removed` | удалён выходной профиль (OutputIntent) |
| `auto:color_conversion:<rgb\|icc>` + `outcome: failed` / `skipped` | конвертация не удалась (PDF без изменений, `severity: warning`) / пропущена (`data.skip_reason`, напр. `lab`, `output_intent`) |
| `auto:size_fix:<method>` | автоматическая коррекция размера: `bleed` («Добавлены поля вылета (Callas)»), `reveal`, `stretch`, `shrink`, `fill`, `canvas`, `callas`; `data.from_mm` / `to_mm`, `detail` — описание |
| `auto:callas_source_fix:<plan>` | автоматически принятое исправление Callas исходника без отметки размера |
| `auto:tiff_flatten` | слои TIFF сведены (`data.visible_layers`, `hidden_layers`, `strategy`) |
| `auto:dpi_downsample` | разрешение растра уменьшено (`data.from_dpi`, `target_dpi`) |
| `auto:strip_icc` | ICC-профиль растра CMYK удалён при сборке PDF |
| `auto:size_correction` | коррекция размера при сборке без отметки `size_fix` (старые запуски) |
| `auto:rotate` | сторона повёрнута (`data.rotation_degrees`) |
| `auto:action:<kind>` | любое другое действие обработки файла: сырые поля в `data` |

Состояния берутся из снимка первой проверки, снимков ревизий PDF и итогового
заказа; повторы (тот же код, файл и исход) не дублируются.

### Действия оператора (`kind: "operator_change"`)

| Код | Что это |
|---|---|
| `callas_fix:<plan>` | одна попытка исправления Callas (`remove_clipping`, `reduce_tac`, `black_to_rich`, `fonts_to_outlines`, `downsample_images`, `size_correction`): события started → finished → accepted/rejected/save_failed сведены в одну запись; `outcome` — итог, `data.started_at`/`finished_at`/`decided_at`, `data.pdf_revision` и `revision_status` — ревизия PDF, созданная принятием; `file` — для исправления исходника |
| `correction:confirmed` / `correction:rejected` | решение по коррекции размера (`data.original_mm`, `proposed_mm`, `crop_mm`, `scale`; для подтверждённой — `data.method` из `size_fix`) |
| `correction:callas_source_fix` | заказ продолжен после принятия исправления исходника (если не удалось привязать к попытке Callas) |
| `callas_source_fix:applied` / `:reset` | применение / сброс исправления исходника без найденной попытки |
| `operator:size_fix:<method>` / `operator:callas_source_fix:<plan>` | подтверждённая коррекция без события (старые запуски) |
| `pdf_revision` | PDF заменён не исправлением Callas (`data.pdf_revision`, `status`); `source: system` — ревизия без события |
| `forced_print` | отправлено в печать с ошибками по подтверждению оператора (`severity: warning`) |
| `operator:lowres_preview` | оператор построил разметку разрешения («Показать разрешение», событие `order.lowres_preview.generated`; `source: callas`, `outcome: done`): `count` и `data.marked_images` — число размеченных цветных/полутоновых изображений, `data.pages_total`, `threshold_ppi` (250), `pdf_kind` (`production`/`diagnostic`), `source_pdf_revision` — ревизия, по которой строилась разметка; `data.low_images` и `pages` — только если известны ppi отдельных изображений (сейчас отчёт профиля их не даёт, поля нет). Повторное нажатие при неизменном PDF берёт кэш и записи не добавляет. PDF заказа не меняется |
| `operator:lowres_preview_attached` | картинка разметки приложена к возврату как пользовательское превью для Sborka (событие `order.lowres_preview.attached`, `outcome: applied`): `data.preview` — имя файла превью, `marked_images`, `source_pdf_revision` |

### Решения (`kind: "decision"`)

| Код | Что это |
|---|---|
| `action:print` / `action:reject` | запись `order_actions`: `outcome` `done`/`failed`, `detail` — комментарий, `data.kit_main_id` / `returned_with_kit` / `message` для возврата в составе комплекта |
| `transition:rolled_back` | перемещение файлов отменено |

## Что намеренно не отражается

* `order.file_transition` — механическое перемещение файлов, его отражает решение
  `action:*`; `order.detected`, `order.checked`, `order.waiting_confirmation`,
  `pdf.created`, `preview.created`, `pitstop.check_*` — этапы конвейера, а не
  находки/изменения (результат PitStop уже в итоговых находках);
* «Шрифты переведены в кривые» (`prepress.fonts.converted`) — это результат
  попытки `callas_fix:fonts_to_outlines`, которая уже есть в журнале;
* `resample_*`, `needs_resample`, `pdf_resize_plans` — внутренние параметры
  решения; итог отражает `size_fix` или решение по коррекции;
* полный отчёт PitStop и ответы Sborka — только ссылки/сводки, не копии.
