# Пользовательские профили callas pdfToolbox

Профили `.kfpx` созданы вручную в desktop-версии pdfToolbox (формат проприетарный, внутри XML). Исходники лежат на шаре `/mnt/shared/soft/callas_checks_fixups/` (`\\10.20.2.114\shared\soft\callas_checks_fixups`), копии в репозитории: `callas_profiles/`. **Профили нельзя создавать и править вручную или кодом**: любое изменение делается только в desktop pdfToolbox, затем файл копируется заново (см. «Развёртывание»). Исходники на шаре не менять.

Проверено 2026-11-13 на тестовом сервере 10.20.2.109 (pdfToolbox 17.0.682, запуск от пользователя `callas`).

## Как приложение вызывает профили

- Имя профиля для приложения: `Image Magic/<имя файла без .kfpx>`. Каталог профилей службы: `/opt/pdfToolbox-17.0.682/var/Profiles` (`CALLAS_PROFILES_DIR`).
- `CallasClient._validate_profile` (`services/callas/client.py`) и серверная `normalize_name` (`/opt/callas-service/callas_service/profiles.py`) пропускают пробелы, скобки, `+`, заглавные, цифры (проверено на всех девяти именах, включая `Pure CMYK black to rich black (C60 M40 Y40 K100)`). Запрещены: длина более 300, `\`, начало с `/` или `~`, `..`, элементы с точки, пробелы по краям элемента, глубина более 4. Клиент `image-magic` имеет `allowed_profiles: ["*"]`. Команда CLI собирается списком без shell, спецсимволы имени безопасны.
- Команда службы: `pdfToolbox --noprogress [--setvariable=K:V] -r=XML,PATH=<xml> (-a | -o=<out.pdf>) <профиль> <вход>`. Режим `analyze` = `-a`, `fix` = `-o`.
- Коды выхода (`EXIT_STATUS` службы): 0, 1 чисто (`passed`); 2 предупреждения; 3 ошибки; 5, 6 исправлено (`fixed`); 7 предупреждения после исправления; 8 ошибки после исправления. Всё до 8 включительно не сбой запуска. 101+ это сбой (103 профиль не загрузился, 104/105 вход не открылся/зашифрован, 106 не удалось сохранить результат).
- Сводка (`parse_report`): `errors/warnings/infos` = число `<hit>` внутри `<hits severity=...>`; `fixups_applied` = сумма `count` элементов `<results><fixup count=N severity="SUCCESS">`; `fixups_failed` = сумма `count_fail` (`severity="ERROR"`).
- **В режиме `fix` выходной PDF создаётся всегда**, даже если исправлений нет (pdfToolbox пересохраняет файл, размер и байты меняются). Решение «файл изменён» принимать по `fixups_applied > 0`, а не по наличию `output_pdf`.
- Fixup-профили без правил в режиме `analyze` ничего не находят (код 0, нулевые счётчики): обнаружение проблемы ими невозможно, только применение.
- В fix-режиме профиль вида «только fixup» не отличает «нашёл и исправил» от «применил на пустом месте»: счётчик `count` растёт и для безобидных объектов (см. обтравку ниже). Для определения «нужно ли» использовать собственные проверки приложения.

## Что делает приложение (2026-11-14)

Код: `services/callas/` (`color_conversion.py`, `prepress.py`, `prepress_reports.py`, `black_solid.py`,
`fix_checks.py`), планы в `config/callas_plans.py`, `CallasFixService` (`services/callas_fix.py`),
фронтенд `frontend/src/components/prepress.js`. Результаты проверок лежат в `order["prepress"]`
(и `order["color_conversion"]`), в `order.warnings` они **не** попадают, чтобы не менять статус заказа.

| Действие | Как | Где видно |
|---|---|---|
| RGB → CMYK, авто | `Image Magic/NEW_Convert color to 4c for digital printing` вместо ISO Coated v2; `IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_ENABLED/_PROFILE` как были; «был RGB» по `find_rgb_pages` до и после | бейдж RGB |
| ICC off | до конвертации стандартный профиль «PDF analysis/List objects using ICC or Lab or calibrated color» (hit на объект, страница в `page`); `icc_removed`, `icc_pages` | бейдж «ICC off» |
| Шрифты | авто, `Image Magic/Warn Fonts` (analyze); считаются **уникальные** шрифты: `font_id` из hits, тип/имя/`embedded` из `resources/fonts`; повтор после принятого фикса (`recheck_pdf_revision`) | строка «Шрифты: N: список» в таблице параметров; при 0 после кривых только иконка «Шрифты переведены в кривые» |
| Шрифты в кривые | кнопка «Перевести шрифты в кривые» (план `fonts_to_outlines`, `Image Magic/Convert fonts to outlines`), просмотр оператором и accept; после фикса сразу повторяется Warn Fonts; невстроенные: `fixups_failed>0`, код 8, сообщение «Шрифты не встроены, перевод в кривые невозможен, нужна проверка макета.»; частичный успех показывается предупреждением | карточка |
| Плашка 100% K | авто, `separations` (`sep_process`, PNG, 50 ppi): доля пикселей Black<30 при C,M,Y>=225 от площади страницы; порог `IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD` (0.25); «Плашка 100% K: N% площади, стр. …» | карточка; кнопка «Перевести плашку в составной чёрный» (план `black_to_rich`) |
| Составной чёрный | перед запуском проверка, что RGB уже нет (иначе отказ 409 `rgb_not_converted`); после фикса сравнение с оригиналом (`changed_percent`), повтор детектора, предупреждение про мелкий текст и штрихкоды (профиль меняет все K100). Чёрное поле, которое не является чистым K100 (составной/растровый чёрный), даёт предупреждение «скорректируйте вручную» | карточка |
| Обтравка | «Clipping objects found» создаёт Enfocus PitStop (ActionID 2079); `issue_groups` собирает её в одну группу с числом и страницами (`fix_plan=remove_clipping`); кнопка «Удалить обтравку» (план `remove_clipping`), подсказка про Fixup `RemoveClipPathes`; профиль срабатывает почти всегда (служебный клип), поэтому после фикса сравнение копии с оригиналом (`core/fix_compare.py`, страница 1) и подтверждение оператора; предупреждение про скрытое содержимое | карточка |
| Растры выше 500 ppi | стандартный профиль «List color and grayscale images not within 250 to 450 ppi» (XML), фильтр `max(v_ppi,h_ppi) > 500 + 0.5`; одно сгруппированное предупреждение «Растры выше 500 ppi: N объектов, стр. …»; однобитные (`bits_per_color_component=1` или маска) отдельной пометкой, не блокируют; снижения разрешения нет | карточка |

Новые настройки: `IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED` (по умолчанию `true`),
`IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD` (`0.25`), `IMAGE_MAGIC_HIGH_PPI_LIMIT` (`500`).
Для плашки K100 на службе callas должен быть включён тип задания `separations`
(`CALLAS_ENABLED_TYPES`; на тесте 192.168.88.109 включён). Проверка сбоя не роняет заказ: блок
получает `status: failed` и текст ошибки. Ограничения: сравнение «до/после» строится по первой
странице; детектор плашки видит только то, что попало в сепарации Black при белых C,M,Y.

## Таблица профилей

| Файл | Имя для приложения | Назначение | Режим |
|---|---|---|---|
| `NEW_Convert color to 4c for digital printing.kfpx` | `Image Magic/NEW_Convert color to 4c for digital printing` | RGB, ICC-RGB и Lab в CMYK (PSO Coated v3), без встраивания OutputIntent | авто, вместо `Convert colors/Convert to CMYK, keep spot colors (ISO Coated v2 (ECI))` |
| `Warn Fonts.kfpx` | `Image Magic/Warn Fonts` | определение шрифтов (живой текст) | analyze, «по аналогии с полями» на карточке |
| `Pure CMYK black to rich black (C60 M40 Y40 K100).kfpx` | `Image Magic/Pure CMYK black to rich black (C60 M40 Y40 K100)` | чистый K100 в составной чёрный C60 M40 Y40 K100 | fix, кнопка |
| `Convert fonts to outlines.kfpx` | `Image Magic/Convert fonts to outlines` | шрифты в кривые | fix, кнопка |
| `Remove all clipping paths.kfpx` | `Image Magic/Remove all clipping paths` | удаление всех обтравок | fix, кнопка |
| `Black to rich black.kfpx` | `Image Magic/Black to rich black` | любой «только чёрный» объект в C40 M40 Y40 K100 (не использовать, см. ниже) | fix |
| `List potential overprint problems.kfpx` | `Image Magic/List potential overprint problems` | 32 проверки наложения (overprint/knockout) | analyze |
| `chek and fix bleed to js script.kfpx`, `new_size_correction.kfpx` | `Image Magic/chek and fix bleed to js script`, `Image Magic/new_size_correction` | коррекция размера/вылетов (JS), дубликаты рабочего `size_correction` | fix |

Профили развёрнуты на сервере 10.20.2.109 в `/opt/pdfToolbox-17.0.682/var/Profiles/Image Magic/` (root:root 644, как у соседних).

## Подробно по профилям

### NEW_Convert color to 4c for digital printing

- Внутри: один Fixup (имя профиля и fixup в отчёте: `NEW_Convert color to 4c for digital printing (Copy 1)`), Checks нет. Настройки: цель PSO Coated v3, `EmbdNone` (OutputIntent не встраивается); DeviceRGB/ICC-RGB/CalRGB/Lab конвертируются в цель; DeviceCMYK и Gray, Spot не трогаются; ICC-CMYK переводится в DeviceCMYK без пересчёта (`Decalibrate`); политики сохранения чистого K.
- Переменных нет. Нужен ICC `PSOcoated_v3.icc` на сервере (есть в `/opt/pdfToolbox-17.0.682/etc/ICC profiles/`).
- Отчёт: `<results><fixup fixup_id="FIX1" count="1" severity="SUCCESS">`, в режиме `-o` код выхода 5. На CMYK/Gray без RGB: код 0, `fixups_applied=0`.
- Особенность: на CMYK-макете с прозрачностью вторым fixup срабатывает «Set transparency blend color space» (`Summary Corrections 2`, `soft_07_90x50.pdf`); поэтому `fixups_applied>0` не доказывает наличие RGB. «Был ли RGB» определять `core.pdf_inspector.find_rgb_pages` до и после, как сейчас в `services/callas/color_conversion.py`.
- Отличие от текущего профиля `Convert to CMYK ... ISO Coated v2 (ECI)`: другой профиль-цель (PSO Coated v3 вместо ISO Coated v2) и **не** встраивает OutputIntent (старый встраивает; проверено: `OutputIntent` есть в результате старого, нет в новом). Серый RGB (R=G=B) превращается в чистый K (канал Black), цвета немного светлее по сумме красок (real_np50x50: C 17.3, 15.6 против старого 16.6).
- Страницы и размеры: число страниц и размер страницы сохраняются. У PDF с отрицательным началом MediaBox (real 50x50: `-8.39,-8.39`) callas нормализует начало координат в (0,0), размер тот же.
- Остаточный RGB: ImageMagick-миниатюра `/Thumb` (1x1, ICCBased RGB) остаётся нетронутой, на странице не видна.
- Время: около 1 с на файл (в том числе 6 МБ).

### Warn Fonts

- Внутри: одна проверка (правило `Fonts found in PDF`, условие `CSFONT::FontType` is_regex `.+`), важность Warning, fixup нет. Имя профиля в отчёте `Warn if PDF uses fonts`. Переменных нет.
- Как определять: режим `analyze`. Есть шрифты/живой текст: код 2, `warnings >= 1`. Нет шрифтов: код 0.
- Разбор XML: `<hits rule_id="RUL1" severity="Warning">` содержит по `<hit type="TextFill|TextInvisible|TextStroke...">` на каждый текстовый объект; `<trigger><value>` тип шрифта (`Type1`, `TrueType`, `Type3`), `<detail>` имя шрифта (может быть пустым у Type3). Список шрифтов с флагами `embedded`/`subset`: `<resources><fonts><font id type embedded subset><name>`. Одно срабатывание на объект, а не на шрифт: считать по `font_id`/`detail`.
- Не различает «встроен/не встроен» в самих hit: смотреть `embedded` в `<resources><fonts>`.
- Срабатывает также на невидимый текст (`TextInvisible`) и стандартные шрифты (Helvetica, Courier).

### Pure CMYK black to rich black (C60 M40 Y40 K100)

- Внутри: правило `R_17_Isvectorobjectnoregistrationcolornotwhite` (вспомогательное, скрытое), один Fixup `MapColors` дважды: для изображений (`Images`) и для векторных объектов (`PRCCUSTOMCHECK`), исходный цвет C0 M0 Y0 K100, допуск 0, результат C60 M40 Y40 K100. Переменных нет.
- Действие: только точный K100. Тоны K (например K40) и RGB не трогаются; K100 в растровых CMYK-картинках пересчитывается по пикселям.
- Отчёт: `Fix ... Pure CMYK black to rich black (C60 M40 Y40 K100)`, `fixups_applied` = число сработавших прогонов (1-2), код 5. Нет K100: код 0, `fixups_applied=0`. Это и есть признак «найден чистый K100» (для кнопки: применить и посмотреть счётчик; отдельной проверки нет).
- Размеры страниц и их число сохраняются. Сработало на: `black_vector.pdf` (K 59.45, C 6.5 -> 36.5, M 5.9 -> 25.9), `black_raster_cmyk.pdf` (картинка: C 7.5 -> 63, M 0 -> 37), `real_np50x50.pdf` (2 исправления, текст K100 -> rich, C +0.8 п.п.), `soft_04/07/12` (малые изменения). Не сработало: `black_tint40_half.pdf` (K40), RGB-чёрные, `fonts.pdf`, `scan.pdf`, `soft_09`, `soft_11`.
- Ограничения: мелкий текст в составном чёрном риск приводки при печати; выполнять после RGB->CMYK (иначе RGB-чёрный не станет K100). Проверена цепочка 4c -> K100 -> outlines на `real_np50x50.pdf`: все три шага отработали (коды 5).

### Convert fonts to outlines

- Внутри: один Fixup `ConvertFontsToOutlines` (`F_7_Convertfontstooutlines`), без проверок и переменных.
- Отчёт: `<results><fixup count=N severity="SUCCESS"><details><detail>ИмяШрифта`. Не удалось: `<fixup count_fail=N severity="ERROR">` и в консоли `FixFailure ... <имя>`; код 8.
- **Не конвертирует невстроенные шрифты** (Helvetica, Courier, Helvetica-Bold из `fonts.pdf`, `black_vector.pdf`): `fixups_failed>0`, статус `errors` (код 8), PDF остаётся с живым текстом. Встроенные TrueType/Type1 (BirchCTT, DejaVuSans) и Type3 конвертируются.
- После конвертации `Warn Fonts` на результате даёт 0 предупреждений (`real_np50x50` цепочка). Невидимый текст (`TextInvisible`) не конвертируется и остаётся: в `fonts_outl.pdf` (Helvetica, 3 невидимых) `Warn Fonts` всё ещё показывает 3 предупреждения.
- На PDF без текста (`scan.pdf`, `soft_09`, `soft_04`): код 0, `fixups_applied=0`. Размер/страницы сохраняются. Внешний вид: разница при растеризации минимальна (средняя 0.1-0.15 из 255).

### Remove all clipping paths

- Внутри: один Fixup `RemoveClipPathes`. Переменных нет.
- Отчёт: `<fixup count=N severity="SUCCESS">`, код 5. Нет обтравок: код 0.
- **Срабатывает практически на любом PDF** из Illustrator/Photoshop, потому что в них есть служебная обтравка по границе страницы: `fixups_applied` > 0 на `soft_04/07/09/11/12`, `real_np50x50`. Поэтому счётчик не означает «макет с вырубкой»; на `fonts.pdf`, `scan.pdf`, `black_vector.pdf` (нет клипа) 0. Определять «обтравка найдена» только отдельной проверкой (например по анализу объектов), не по этому профилю.
- Внешний вид меняется там, где обтравка реально скрывала содержимое: `clip_real_B_40x40.pdf` (круглый макет, средняя разница 34 из 255, максимум 230) и `clipping.pdf` (41.5, максимум 255) показывают то, что было за обтравкой. На обычных макетах отличие 0. Для заказа с вырубкой, где важна форма, кнопку применять только после просмотра результата.
- Страницы, размер и боксы (Crop/Trim/Bleed) сохраняются.

### Black to rich black (не использовать для автоматики)

- Правило `R_18_Objectusesblackonly` (`CSCOLOR::ObjectUsesBlackOnly`) + Fixup `SetColor` в C40 M40 Y40 K100 (комментарий «C40 K100») для заливок и обводок.
- Затрагивает **любой «только чёрный»** объект, включая тона и серые: `black_tint40_half.pdf` (K40, в сумме K 20% покрытия) превратился в сплошной K100 с C40 M40 Y40 (покрытие K 20 -> 50, средняя разница 84 из 255). Это меняет внешний вид, поэтому профиль только справочный; вместо него использовать `Pure CMYK black to rich black`.

### List potential overprint problems

- 32 правила (все важность Warning, `analysis=17`): чёрный/белый/серый/RGB/спот/Lab/CMYK объекты с неверным knockout/overprint, белый и мелкий чёрный текст (до 12 pt) и др. Fixup нет, переменных нет.
- Отчёт: `<hits rule_id=... severity="Warning">`, имя правила в `<rules><rule><display_name>` (например `Black object is set to knock out`). Код 2 при срабатывании, 0 если нет.
- На реальных макетах чисто (`real_np50x50`, `soft_*`: код 0). Срабатывает на `black_vector.pdf` (4 предупреждения «Black object is set to knock out»).

### chek and fix bleed to js script / new_size_correction

- Содержимое **идентично** рабочему `size_correction.kfpx` на сервере (`new_size_correction.kfpx` и `size_correction.kfpx` совпадают побайтово; `chek and fix bleed...` отличается только именем профиля, датой, id). Внутри JS-переменные (`V_Extract_Size` берёт размер `(ШxВ)` из **имени файла**, `V_Compare_Geometry`, `V_Check_Outer`, `QC_*`), Fixup `Set CropBox to MediaBox`, `F_Generate_Fields` (`GenerateBleed`, 2 мм со всех сторон), `QF_*` через `quickfix set_page_box`. Переменная `pad_pages` задаётся самими скриптами.
- Имя входного файла должно содержать ровно одну пару `(ШxВ)`; служба сохраняет имя входа именно для этого.
- Это существующий механизм «Коррекция размера макета» (`config/callas_plans.py`); отдельно регистрировать дубликаты не нужно. Прогон на `13_NP(K)..._(50x50)...` дал код 8 (`Corrections 1`, ошибка размера из JS: размер страницы не равен trim+4 мм); логика та же, что и у `size_correction`.

## Сводка тестов на реальных PDF

Входы (сервер, копии в `/tmp/callas-lab/in/`, отчёты `/tmp/callas-lab/rep2/`, результаты `/tmp/callas-lab/out2/`, локальные копии лабораторных: `/home/ubuntu/orca/workspaces/v2-web-platform-FastApi/lab-artifacts/pdf/`, `.../real/`):

- `/mnt/shared/inputFolders/13_NP(K)_Bezlam_350_mel_(50x50)_4-0_L1_T1_(31696-26202209)_1-face.pdf` (RGB-картинка + живой текст BirchCTT, K100 текст)
- `/mnt/shared/soft/callas_checks_fixups/09_NP_Bezlam_130_mel_gl_(99x210)_4-4_T1000_(34250-26146648)_offset-face.pdf`, `11_NP(K)_..._(110x85)_...pdf`, `07_NP_..._(90x50)_...pdf` (спот, прозрачность), `04_NP(P)_..._curve4_offset-face.pdf`, `12_NP(P)_Mlam10_..._(100x100)_...pdf`
- лабораторные: `black_vector`, `black_k100_quarter`, `black_small`, `black_tint40_half`, `black_rgb_vector`, `black_raster_cmyk`, `black_raster_rgb`, `rgb_501`, `fonts`, `type3`, `scan`, `clipping`, `clip_real_A_90x50`, `clip_real_B_40x40`, `fonts_outl`, `gray_exact500`; созданный ImageMagick `icc_rgb` (ICCBased sRGB).

Результаты (код выхода / `fixups_applied`):

| Вход | 4c | Warn Fonts | K100 | Outlines | Clip |
|---|---|---|---|---|---|
| real 50x50 (RGB, текст) | 5 / 1, RGB -> CMYK | 2 / 1 warning (BirchCTT) | 5 / 2 | 5 / 1 | 5 / 1 |
| soft_09 99x210 (CMYK) | 0 / 0 | 0 | 0 / 0 | 0 / 0 | 5 / 2 |
| soft_11 110x85 (CMYK) | 0 / 0 | 0 | 0 / 0 | - | 5 / 1 |
| soft_07 90x50 (спот) | 5 / 2 (blend CS) | 0 | 5 / 1 | - | 5 / 2 |
| soft_04, soft_12 | 0 / 0 | 0 | 5 / 2, 5 / 1 | 0 / 0 (04) | 5 / 2, 5 / 1 |
| fonts.pdf (нет встроенных) | 0 | 2 / 5 warnings | 0 | 8 / 1 + 2 failed | 0 |
| type3.pdf | 0 | 2 / 1 | - | 5 / 1 | - |
| scan.pdf (без текста) | 0 | 0 | 0 | 0 | 0 |
| black_vector (K100) | 0 | 2 / 1 | 5 / 1 | 8 (Helvetica-Bold failed) | 0 |
| black_raster_rgb | 5 / 1 | - | 0 | - | - |
| rgb_501 (серый RGB -> K) | 5 / 1 | 0 | 0 | - | - |
| icc_rgb (ICC sRGB) | 5 / 1 | - | - | - | - |
| clip_real_B_40x40 | 0 | - | - | - | 5 / 1 (внешний вид меняется) |

Во всех 68 проверенных результатах число страниц и размер страницы совпали со входом (сверка PyMuPDF: `mediabox` и размер). У трёх входов (real 50x50, soft_04, soft_12) pymupdf показывает смещение начала координат MediaBox после пересохранения callas, размер не меняется.

## Развёртывание на сервере

1. Взять файлы из `callas_profiles/` репозитория (или с шары).
2. Скопировать в `/opt/pdfToolbox-17.0.682/var/Profiles/Image Magic/`. Только добавлять новые файлы. Если имя занято, сначала сделать копию `<имя>.bak-<ГГГГММДД>`, затем заменить. Существующие `size_correction.kfpx` и `.bak` не перезаписывать.
3. Владелец и права как у соседей: `root:root`, `chmod 644` (пользователь `callas` читает).
4. Перезапуск службы не нужен: каталог читается при каждом запросе. Проверка: `GET /profiles` службы или `list_profiles()` в `/opt/callas-service`.
5. Запуск вручную для проверки: `runuser -u callas -- env HOME=/home/callas /opt/pdfToolbox-17.0.682/pdfToolbox --noprogress -r=XML,PATH=/tmp/x.xml -o=/tmp/out.pdf "/opt/pdfToolbox-17.0.682/var/Profiles/Image Magic/<имя>.kfpx" <вход.pdf>` (лицензия привязана к пользователю `callas`; каталог результата должен быть доступен на запись пользователю `callas`, иначе код 106).
6. На прод-сервер выкладывать так же и теми же файлами; перед этим сверить, что `PSOcoated_v3.icc` есть в `etc/ICC profiles`.

## Правила изменения профилей

Профили `.kfpx` создаются и редактируются только в desktop pdfToolbox (формат с id, отпечатками и версией схемы `23.0`). Ручная правка XML или генерация кодом не поддерживается и может сломать загрузку (код 103). Для изменения: правка в desktop, экспорт, копия на шару, затем копия в `callas_profiles/` и развёртывание по списку выше. Имя файла определяет имя для приложения, при переименовании менять и `config/callas_plans.py`.
