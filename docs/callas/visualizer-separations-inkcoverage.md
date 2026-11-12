# callas service: визуализатор, цветоделение, покрытие красками, режим наложения

Дата: 10 ноября 2026. Платформа службы: Ubuntu 26.04.1, pdfToolbox CLI 17.0.682.

Это справочник по **серверному** API callas-service (репозиторий службы). Клиент приложения (`services/callas`) эти типы пока не вызывает: поддерживаются только `merge` и `render`. Снимок `docs/callas/openapi.json` также не обновлялся и содержит только `merge`, `render`, `process`; актуальный контракт — `/openapi.json` работающей службы.

Типы заданий `visualizer`, `separations`, `inkcoverage` и необязательные поля `render` добавлены **аддитивно**: `merge`, `render` (при значениях по умолчанию), `process`, `info`, `split`, их ответы и контрактные снимки не менялись. Все новые типы **выключены по умолчанию** (`CALLAS_ENABLED_TYPES`). Эксперименты проведены на настоящем CLI, только на синтетических PDF (собраны вручную: CMYK, плашечная краска `PANTONE 185 C`, overprint), без активации и без реальных документов.

Связанные документы службы (лежат в её репозитории): `callas-service.md`, `callas-full-functionality-plan.md` (раздел 3.2), `callas-split-client.md`.

## 1. Включение и права

```bash
# /etc/callas-service.env (по заданию владельца; в репозитории службы - только пример в deploy/callas-service.env.example)
CALLAS_ENABLED_TYPES=merge,render,process,info,split,visualizer,separations,inkcoverage
CALLAS_VISUALIZER_TIMEOUT=600        # с на все вызовы CLI одного задания (от запуска CLI)
CALLAS_VISUALIZER_MAX_FILES=3000     # больше файлов в наборе - cli_failed
CALLAS_ICC_NAMES=ISO Coated v2 (ECI) # каталог ICC для gamut_*_profile; пусто - только OUTPUTINTENT
```

- Тип вне `CALLAS_ENABLED_TYPES` отвергается кодом 422 (как `info`/`split`).
- **Права клиента.** До 10 ноября 2026 поля `allowed_types` в файле клиентов не было. Добавлено необязательное поле: нет поля — клиенту доступны все включённые типы (прежнее поведение, старые файлы клиентов не меняются); список (`"allowed_types": ["info","split","visualizer"]`) — только перечисленные, иначе 403; неизвестное имя типа — отказ загрузки файла клиентов. Пример — `deploy/callas-clients.json.example` в репозитории службы.
- Входы — внутри `allowed_input_roots` клиента; `out_dir` — личная папка задания `<шара>/.callas/<job_id>/out` либо каталог из `allowed_output_roots` (как у `split`).
- Лицензионные и сетевые опции CLI (`--licensetype`, `--dist`, `--endpoint` ...) в API не выставлены и отвергаются при сборке команды (`cli_args.reject_forbidden`).

## 2. Запросы и ответы

Общее для типов с изображениями: результат публикуется **набором** в `out_dir` (`.tmp`, сверка SHA, атомарное переименование; чужое содержимое под тем же именем — `output_exists`, тот же SHA — идемпотентный повтор; в личной папке задания повтор после сбоя воркера заменяет файлы). Имя файла: `page-<NNNN>_<вид>.<расширение>`, `NNNN` — номер страницы с 4 цифрами. В ответе: `files` (имя, путь, размер, SHA-256), `layers` (страница, вид, имя файла — соответствие «страница/часть» → файл), `document_pages`. В `GET /jobs` (список) массив `layers` не повторяется, остаётся `document_pages`.

### 2.1. `visualizer` — `pdfToolbox --visualizer`

```json
{"type":"visualizer","input":{"path":"/mnt/shared/in/job.pdf","sha256":"(необязательно)"},
 "out_dir":"/mnt/shared/.callas/viz-1/out",
 "pages":"1-3,5",
 "parts":["full","process_CMYK","sep:PANTONE 185 C","ink_sep_above:Cyan=250"],
 "imgformat":"PNG","resolution":72,
 "simulate_overprint":true,"blackpoint_compensation":true,
 "inkcov":280,"only_problems":false}
```

Ответ (после завершения; поля `type`, `client`, `created`, `attempts`, `bad_inputs`, `error_class` — как у других типов):

```json
{"job_id":"viz-1","type":"visualizer","status":"done","exit_code":0,"error":null,"duration":1.2,
 "document_pages":3,
 "files":[{"name":"page-0001_full.png","path":"/mnt/shared/.callas/viz-1/out/page-0001_full.png","size":751,"sha256":"..."}],
 "layers":[{"page":1,"part":"full","name":"page-0001_full.png"},
           {"page":1,"part":"process_CMYK_noSpot","name":"page-0001_process_CMYK_noSpot.png"},
           {"page":1,"part":"sep_PANTONE 185 C","name":"page-0001_sep_PANTONE_185_C.png"}]}
```

| Поле | Допустимые значения | CLI |
| --- | --- | --- |
| `input`, `out_dir` | как у `split` | вход, `-f=<локальная папка>` |
| `pages` | `N`, `N-M`, списки через запятую (`1-3,5`); по умолчанию все. Страницы вне документа — `range_invalid` | `--pagerange` (`-p`) |
| `parts` | список (1–40, без повторов), по умолчанию `["full"]` (CLI без `--part` выдаёт 22 вида на страницу — слишком много; все виды — `["all"]`); значения см. ниже | `--part=a,b,c` |
| `exclude_processing_steps` | `ALL`, `ALLBUTVARNISHWHITE` | `--excludeprocessingsteps` |
| `use_bleed` | `false` | `--usebleed` |
| `simulate_overprint` | `true` (по умолчанию CLI симулирует overprint); `false` | `--nosimulateoverprint` |
| `blackpoint_compensation` | `true` (по умолчанию); `false` | `--noblackpointcompensation` |
| `safety_outside`, `safety_inside` | число 0–100, мм (по умолчанию CLI 3) | `--safetyoutside`, `--safetyinside` |
| `gamut_method` | `DeltaE`, `CIE2000DeltaE`, `DeltaC`, `CIE2000DeltaC` | `--gamut_method` |
| `gamut_target_profile`, `gamut_current_profile` | `OUTPUTINTENT` или имя ICC (тег `desc`) из `CALLAS_ICC_NAMES`; пути, `/ \ , ; = " '` и управляющие символы запрещены (форма — 422, не из каталога — 403) | `--gamut_targetprofile`, `--gamut_currentprofile` |
| `gamut` | число 0–1000 (порог Delta E) | `--gamut` |
| `sep_colors` | `false` | `--sep_colors` |
| `only_problems` | `false`; при `true` пустой результат допустим | `--onlyproblems` |
| `smlobj` | `low`, `medium`, `high` | `--smlobj` |
| `inkcov` | число 0–400 (порог покрытия красками, %) | `--inkcov` |
| `bmpres`, `imgres` | целое 1–10000 (ppi) | `--bmpres`, `--imgres` |
| `resolution` | ppi 1–1200 (число) или `ШxВ` пикселей, до 20000 (строка); по умолчанию 72 | `--resolution` |
| `colorspace` | `RGB`, `Gray`, `RGBA`, `GrayA` (прозрачность — только PNG и TIFF) | `--colorspace` |
| `imgformat` | `JPEG`, `PNG` (по умолчанию), `TIFF`, `PDF`; регистр не важен. **`BMP` и `WEBP` не выставлены** — на 17.0.682 не работают (см. раздел 4) | `--imgformat` |
| `jpegformat` | `Baseline_Standard`, `Progressive_3_Scan` (JPEG и PDF) | `--jpegformat` |
| `compression` | `JPEG_minimum`…`JPEG_maximum` (JPEG, PDF), `PDF_Flate` (PDF), `TIFF_None`/`TIFF_LZW`/`TIFF_FLATE` (TIFF); для PNG не задаётся; числовое `0…100` не выставлено (см. раздел 4) | `--compression` |

Допустимые значения `parts` (строгие регулярные выражения в `cli_args.check_part`): `all`, `full`, `ink`, `ink_temp`, `ink_topo`, `process`, `process_CMYK`, `process_CMY`, `process_K`, `spot`, `spot_spots`, `spot_spots_K`, `spot_CMYK`, `sep`, `sep_process`, `sep_spot`, `imgres`, `imgres_img`, `imgres_bmp`, `smallobj`, `smallobj_text`, `smallobj_lines`, `safety_zone`, `gamut`, `gamut_current`, `gamut_target`, `gamut_heatmap`; `sep:<ИМЯ>`; `ink_sep:<СПЕЦ>[;<СПЕЦ>…]` (`<СПЕЦ>` = `<КРАСКА>=<нижний>:<верхний>`), `ink_sep_below:…` и `ink_sep_above:…` (`<КРАСКА>=<порог>`, до 20 спецификаций). `<КРАСКА>` — имя канала или `PROCESS`/`SPOT`; в имени (1–64 символа) запрещены `; = : ,` (`,` разделяет элементы `--part`), `/`, `\` и управляющие символы. Пороги — числа 0–400 (нижний не больше верхнего).

### 2.2. `separations` — `pdfToolbox --createseparations`

```json
{"type":"separations","input":{"path":"/mnt/shared/in/job.pdf"},"out_dir":"/mnt/shared/.callas/sep-1/out",
 "pages":"2","parts":["sep_process","sep:PANTONE 185 C"],"smoothing":["lines","text"],
 "colorspace":"Gray","imgformat":"TIFF","compression":"TIFF_LZW","resolution":300}
```

Поля `input`, `out_dir`, `pages`, `exclude_processing_steps`, `sep_colors`, `resolution`, `colorspace`, `imgformat`, `jpegformat`, `compression` — как у `visualizer`. `parts` — `sep` (по умолчанию), `sep_process`, `sep_spot`, `sep:<ИМЯ>`. `smoothing` — список из `NONE`, `ALL`, `LINES`, `IMAGES`, `TEXT`, `NTLH` (без повторов; `NONE` и `ALL` не сочетаются с другими) → `--smoothing=LINES,TEXT`; по умолчанию CLI — `ALL`. Ответ: как у `visualizer` (`files`, `layers`, `document_pages`); имена `page-0001_sep_Cyan.tiff`, `page-0001_sep_PANTONE_185_C.tiff`.

### 2.3. `inkcoverage` — процентное покрытие красками

Команда: `pdfToolbox -a -r=JSONV2,INKCOV,INKCOVRES#<ppi>,INKCOVBOX#<рамка>,PATH=<json> <служебный пустой профиль> <вход>`.

```json
{"type":"inkcoverage","input":{"path":"/mnt/shared/in/job.pdf"},"pages":"1-3","resolution":10,"box":"CropBox",
 "keep_report":true,"out_dir":"/mnt/shared/.callas/ink-1/out"}
```

Ответ (проверено на настоящем CLI, страница 1 синтетического PDF: CMYK-прямоугольник, плашка `PANTONE 185 C`, чёрная линия):

```json
{"job_id":"ink-1","type":"inkcoverage","status":"done","files":[{"name":"ink-coverage.json","path":"...","size":5600,"sha256":"..."}],
 "ink_coverage":{"resolution":10,"box":"CropBox",
  "pages":[{"page":1,
            "inks":[{"name":"Cyan","kind":"process","percent":8.162},{"name":"Magenta","kind":"process","percent":4.9099},
                    {"name":"Yellow","kind":"process","percent":3.252},{"name":"Black","kind":"process","percent":5.0003},
                    {"name":"PANTONE 185 C","kind":"spot","percent":16.2602}],
            "process_sum_percent":21.3242,"total_sum_percent":37.5844}],
  "document":{"inks":[{"name":"Cyan","kind":"process","percent":39.2157},{"name":"Magenta","kind":"process","percent":34.97},
                      {"name":"Yellow","kind":"process","percent":34.4173},{"name":"Black","kind":"process","percent":48.0986},
                      {"name":"PANTONE 185 C","kind":"spot","percent":5.4201}],
             "process_sum_percent":156.7016,"total_sum_percent":162.1217}}}
```

| Поле | Значения |
| --- | --- |
| `pages` | как у `visualizer` (по умолчанию все); вне документа — `range_invalid` |
| `resolution` | целое 1–1200 ppi, по умолчанию 10 (как в CLI); 2400 ppi на трёх страницах 300x200 pt — 3,3 с, поэтому предел 1200 |
| `box` | `MediaBox`, `CropBox` (по умолчанию CLI), `TrimBox`, `BleedBox`, `ArtBox` (имя без учёта регистра) |
| `keep_report`, `out_dir` | `keep_report: true` требует `out_dir`; JSON-отчёт публикуется как `ink-coverage.json`; по умолчанию файлов нет |

**Что такое `percent`.** Среднее по рамке страницы покрытие краской, % (сплошная заливка всей рамки — 100; прямоугольник 200x100 pt на странице 300x200 pt даёт 33,33). Краски, которых на странице нет, в отчёте отсутствуют (`inks` может быть пустым). `kind`: `process` — Cyan/Magenta/Yellow/Black, остальное — `spot`. Расчёт растровый, значение зависит от `resolution`: на синтетической странице 1 для Cyan 10 ppi дали 8,16 %, 72 ppi — 6,69 %, 2400 ppi — 6,70 % (аналитически 6,7 %); для точных значений берите 72 ppi и выше, 10 ppi — быстрая оценка.

**TAC.** Максимальной суммы красок (TAC) по точкам страницы в отчёте **нет** — не выдумывается. `process_sum_percent` и `total_sum_percent` — суммы *средних* покрытий (удобно для оценки расхода краски, но не TAC; страница, целиком залитая 1 1 1 1, даёт 400, а маленькое пятно 400 % на белой странице — почти 0). Для проверки TAC используйте вид `ink_temp` визуализатора с порогом `inkcov` (подсветка участков выше порога) либо профиль с проверкой общего покрытия через `process` (не проверялось). Блок `document` — то же по всему документу (по выбранным страницам).

**Почему отдельный тип, а не расширение `process`.** `process` запускает профиль клиента с исправлениями, требует профиль/`out_dir`/права, его ответ (`result_status`, `summary`, `delivered`) и `result.json` зафиксированы контрактом. Отчёт с покрытием не требует ни проверок, ни выходного PDF: достаточно служебного пустого профиля (без JavaScript и внешних ссылок, запись в рабочий каталог задания), поэтому отдельный тип проще и безопаснее: нет профиля у клиента, нет `-o` и исправления входа, не затрагиваются старые ответы. Нужен профильный отчёт `INKCOV` — берётся из XML/JSON-отчёта `process` вручную (расширение `process.report` отложено, план фаза 3).

**Отчёт для выдачи.** JSON CLI содержит серийный номер лицензии, домашний каталог, пользователя и имя хоста (`env`), путь входа и время выполнения. Служба разбирает отчёт сама; при `keep_report` публикуется копия без `env` (остаются `product`, `version`), без пути входа (`fileName` — имя файла клиента) и без `duration`, ключи отсортированы (содержимое детерминировано, повтор даёт тот же SHA). Исходный отчёт CLI клиенту не отдаётся.

### 2.4. `render`: режим наложения и родственные опции

`render` (`--saveasimg`) поддерживает `--nosimulateoverprint`, `--simulatepaper`, `--blackpointcompensation`, `--pagebox` (и ряд других). Добавлены необязательные поля:

```json
{"type":"render","input":{"path":"/mnt/shared/in/job.pdf"},"out_dir":"/mnt/shared/.callas/r-1/out","dpi":150,"format":"png",
 "simulate_overprint":false,"simulate_paper":false,"blackpoint_compensation":false,"pagebox":"TRIMBOX"}
```

| Поле | По умолчанию | CLI |
| --- | --- | --- |
| `simulate_overprint` | `true` (как раньше: CLI симулирует overprint) | `false` → `--nosimulateoverprint` |
| `simulate_paper` | `false` | `true` → `--simulatepaper` |
| `blackpoint_compensation` | `false` | `true` → `--blackpointcompensation` |
| `pagebox` | не задано (CLI: `CROPBOX`) | `MEDIABOX`, `CROPBOX`, `TRIMBOX`, `BLEEDBOX` → `--pagebox` |

При значениях по умолчанию команда **байт-в-байт прежняя** (`test_render_defaults_unchanged`, сравнение с прежней сборкой), а в сохранённую спецификацию задания новые поля не попадают (иначе повтор `PUT` старого задания дал бы 409): явные значения по умолчанию — тот же запрос. Не выставлены намеренно: `--workingspace*` и `--simulationprofile` (профили ICC — только по каталогу администратора, отдельное решение), `--rect*`, `--parallel`, `--digits`, `--noembediccprofile`, `--smoothing`, `--colorspace` (CMYK/Lab-результаты меняют формат выдачи), `--imgformat` кроме PNG/WEBP. Формат `jpeg`/`tiff` в `render` — по-прежнему эксперимент плана.

## 3. Результаты экспериментов на настоящем CLI (10 ноября 2026)

Условия: синтетические PDF 300x200 pt (3 страницы: CMYK + плашка; K с overprint; 100 % всех красок), CLI 17.0.682, каталоги вне репозитория. Скрипт сборки PDF и прогонов в репозиторий службы не добавлялся.

**Схема имён.** CLI пишет `<имя входа>_<NNNN>_<вид>.<ext>` (`NNNN` — 4 цифры, расширения `jpg`, `png`, `tiff`, `pdf`) и печатает строки `Output<TAB><путь>`; при `-p` создаются только выбранные страницы; существующие файлы не перезаписываются. Служба берёт файлы из папки (не из stdout: вывод режется до 8 КБ), называет их `page-<NNNN>_<вид>.<ext>`, очищает вид от неудобных символов (`[^\w.()+-]` → `_`; `PANTONE 185 C` → `PANTONE_185_C`; совпадения получают `-2`, `-3`), упорядочивает по странице и порядку видов CLI; `layers[].part` хранит исходное написание. Пустой файл — `cli_failed`.

Соответствие `--part` → вид (суффикс имени), проверено запуском каждого значения:

| `part` | Вид в имени файла |
| --- | --- |
| `full`, `ink_temp`, `ink_topo`, `process_CMY`, `process_K`, `spot_spots_K`, `safety_zone`, `gamut_current`, `gamut_target`, `gamut_heatmap` | то же имя |
| `ink` | `ink_temp`, `ink_topo` |
| `process` | `process_CMY`, `process_CMYK_noSpot`, `process_K` |
| `process_CMYK` | `process_CMYK_noSpot` |
| `spot` | `spot_CMYK_nospots`, `spot_allspots`, `spot_spots_K` |
| `spot_spots` / `spot_CMYK` | `spot_allspots` / `spot_CMYK_nospots` |
| `sep` / `sep_process` / `sep_spot` | `sep_<краска>`: `sep_Cyan`, `sep_Magenta`, `sep_Yellow`, `sep_Black`, `sep_<плашка>` / только CMYK / только плашки |
| `sep:<ИМЯ>` | `sep_<ИМЯ>` (пробелы допустимы: `sep:PANTONE 185 C`); несуществующая краска — код 0 и **ни одного файла** → `cli_failed` |
| `imgres` / `imgres_img` / `imgres_bmp` | `imgres`, `bitres` / `imgres` / `bitres` |
| `smallobj` / `smallobj_text` / `smallobj_lines` | `sml_lin`, `sml_txt` / `sml_txt` / `sml_lin` |
| `gamut` | `gamut_current`, `gamut_heatmap`, `gamut_target` |
| `ink_sep:…` / `ink_sep_below:…` / `ink_sep_above:…` | `ink_sep_<краски>` / `ink_sep_below_<краска>` / `ink_sep_above_<краска>` (регистр имени нормализуется: `CYAN` → `Cyan`; `PROCESS` разворачивается в имена каналов) |
| `all` | на проверенных документах 22 файла на страницу: 17 общих видов + по одному `sep_*` на краску документа (плашка, описанная в ресурсах страницы, даёт `sep_<плашка>`, даже если не использована) |

**Время и объём.** 3 страницы, `all`: 1,0 с (66 файлов); 3 страницы, 4 части (24 файла): 1,2 с; 100 страниц, `full`+`sep` JPEG 72 ppi: 1,0–1,6 с (600 файлов); 10 страниц, `all`, PNG 150 ppi: 5,1 с (220 файлов); `separations` 2 страницы TIFF: 1,0 с; `inkcoverage` 3 страницы: 0,5–0,7 с, 100 страниц — 0,6 с. Размеры PNG одноцветных страниц — сотни байт. Время растёт с `resolution`, числом видов и страниц; реальные печатные страницы не измерялись.

**Overprint (режим наложения).** Страница с голубой заливкой и пурпурной заливкой поверх с `OP true`, `OPM 1`: `render` по умолчанию даёт в пересечении сине-фиолетовый (61, 52, 131), с `simulate_overprint: false` — чистый пурпурный (230, 12, 128) (knockout). У **визуализатора** на этом примере `--nosimulateoverprint` меняет только виды `gamut_*`; `full`, `sep_*`, `ink_*` и остальные побайтно те же. Не проверено на реальных печатных файлах (DeviceN, прозрачность с overprint).

**Коды выхода (stdout, stderr пуст).** Неверное значение `--part`, `--resolution`, `--imgformat`, `--smoothing` — 101 (`Error 1001`); неверный селектор страниц — 103; неизвестный ICC — 103 (`profile not found`); битый/обрезанный PDF — 104 (`Error 1010`), в режиме отчёта при этом **всё равно пишется заглушка JSON** — наличие отчёта не признак успеха; `-p` за пределами документа — код 0 без файлов (служба проверяет страницы сама, `--quickpdfinfo`).

## 4. Ограничения CLI 17.0.682, выявленные экспериментом

- `--imgformat=WEBP`, `--imgformat=BMP` (в том числе с `--colorspace=Gray`, на который ссылается ошибка), числовое `--compression=50` у `--visualizer` и `--createseparations` завершаются кодом 103 (`Assertion failed ... BoaBOParam.h ... it != values.end()`), хотя справка их описывает. В API не выставлены (422), чтобы не получать `cli_failed` после постановки в очередь. Если версия CLI изменится — перепроверить и расширить `IMGFORMATS`/`_COMPRESSION` в `callas_service/cli_args.py`.
- `RGBA`/`GrayA` с JPEG и PDF — код 101 (CLI: «Only supported for PNG or TIFF or WEBP»); служба отвергает сочетание в запросе (422).
- Отчёт с покрытием требует запуска профиля (`-a`): без профиля CLI отвечает «Argument input files is missing».
- `INKCOVRES#0` — отчёт без значений (значения `null`), отрицательное — `ReportError`; служба принимает 1–1200.

## 5. Ошибки

Классы существующие; повторы только у `share_unavailable` (как у остальных типов).

| `error_class` | Когда |
| --- | --- |
| `range_invalid` | `pages` содержит страницы вне документа |
| `input_corrupt`, `input_encrypted`, `input_missing` | вход битый/обрезан, защищён паролем, не найден (проверка до запуска CLI и по кодам 104/105) |
| `cli_failed` | код CLI не 0 (текст — в `stderr`); набор пуст (`sep:<несуществующая краска>`, не `only_problems`); файлов больше `CALLAS_VISUALIZER_MAX_FILES`; пустое изображение; отчёт покрытия не создан или не разобран |
| `timeout` | дольше `CALLAS_VISUALIZER_TIMEOUT` с начала работы с CLI (все вызовы задания, включая подсчёт страниц); процесс убивается, повтора нет |
| `output_exists` | имя результата в каталоге клиента занято другим содержимым; ничего не публикуется |
| `share_unavailable` | шара недоступна при чтении входа/записи набора (повтор до `CALLAS_MAX_ATTEMPTS`) |
| `profile_invalid` | значение прошло модель, но отвергнуто сборкой команды (например, ICC убран из каталога между запросом и запуском) |

Ошибки запроса: 422 — неверное значение/неизвестное поле/тип выключен; 403 — тип не разрешён клиенту, `out_dir` или ICC вне разрешённого; 400 — форма пути. **Отмена** (`DELETE /jobs/{id}`) убивает группу процессов CLI, статус `cancelled` (не `failed`), локальная папка и временные файлы удаляются.

## 6. Лимиты и очередь

- Один слот на задание. Тип относится к полосе `render` (как `render` и `process`): вместе они занимают не более `slots − CALLAS_MERGE_RESERVED`, зарезервированные слоты остаются для `merge`/`info`/`split`. Изменены `KIND` в `callas_service/jobqueue.py` и (комментарий и тест на совпадение) `kind_of` в Lua `redis_queue.py` (неизвестные типы там уже шли в `render`). Проверено на `sqlite` и `redis` (fakeredis).
- Рабочий каталог — локальный диск воркера (`CALLAS_WORK_DIR`); набор изображений целиком лежит там до публикации. 3000 файлов — предел по умолчанию; крупные `resolution` × много страниц × `all` могут занять гигабайты — ограничивайте `pages` и `parts`.
- `inkcoverage` хранит результат в очереди (SQLite/Redis): тысячи страниц — сотни килобайт на задание; `GET /jobs` его не повторяет.
- Таймаут задания `CALLAS_VISUALIZER_TIMEOUT` (по умолчанию 600 с) — один на все три типа.

## 7. Безопасный пример (`curl`)

Только на копии синтетического PDF, внутри шары, и когда тип включён администратором:

```bash
TOKEN=$(sudo grep ^CALLAS_TOKEN= /etc/callas-service.env | cut -d= -f2-)
JOB=viz-$(date +%s)
sudo -u callas mkdir -p /mnt/shared/.callas/$JOB/in && sudo -u callas cp /mnt/shared/test/synthetic.pdf /mnt/shared/.callas/$JOB/in/in.pdf
curl -s -X PUT http://127.0.0.1:8765/jobs/$JOB -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"type\":\"visualizer\",\"input\":{\"path\":\"/mnt/shared/.callas/$JOB/in/in.pdf\"},\"out_dir\":\"/mnt/shared/.callas/$JOB/out\",\"pages\":\"1\",\"parts\":[\"full\",\"sep_process\"],\"resolution\":72}"
curl -s http://127.0.0.1:8765/jobs/$JOB -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

## 8. Проверено и не проверено

Проверено на настоящем CLI 17.0.682 (синтетические PDF, изолированная служба с локальной шарой, не production): все перечисленные в таблице `part`; JPEG/PNG/TIFF/PDF, `colorspace`, `jpegformat`, `compression`, `smoothing`, `-p`, `resolution` (ppi и `ШxВ`); задания `visualizer`, `separations`, `inkcoverage` целиком через API (публикация, SHA, `range_invalid`, `input_corrupt`, пустой набор, `keep_report`), отмена во время работы (процессы CLI убиты), `render` с `simulate_overprint: false` и без него; структура JSON покрытия и сверка значений с ожидаемой площадью.

Автотесты службы (поддельный CLI, `tests/test_visualizer.py`, 143 проверки × 2 бэкенда там, где применимо): валидация, сборка команд, байт-в-байт `render`, успех, ошибки, отмена с убийством процесса, таймаут, публикация и `output_exists`, права `allowed_types`, каталог ICC, полоса очереди для `sqlite` и Redis (fakeredis).

**Не проверено:** реальные печатные PDF (DeviceN, Lab, прозрачность, изображения, слои, OutputIntent); `--gamut_*` с именем ICC (в каталоге не было профилей администратора, проверен только `OUTPUTINTENT` и отказ неизвестного); `--usebleed` на PDF с BleedBox, `ArtBox` в `INKCOVBOX` (принимается, результат не сверялся с рамкой); поведение на документах с сотнями страниц при большом `resolution` и объёмы памяти/диска; Redis-бэкенд на настоящем CLI; нагрузка при 8 слотах; зашифрованные PDF на уровне кодов самого визуализатора (отсекаются проверкой до запуска; 105 сопоставлен по `process`); `--dist` и сателлиты (не выставлены).
