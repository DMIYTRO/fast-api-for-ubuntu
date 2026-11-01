# Команды callas pdfToolbox CLI

Полный перечень **команд, которые показывает установленный pdfToolbox CLI 17.0.682
для Linux**. Основан на `pdfToolbox --help` и сверен с [официальным разделом
CLI](https://help.callassoftware.com/m/pdftoolbox/c/192208). Это именно команды
`--command`, а не полный перечень их параметров: последние различаются по
командам и версии. Точный синтаксис смотрите через
`pdfToolbox --help <command>` ([инструкция Callas](https://help.callassoftware.com/m/pdftoolbox/l/1366743-introduction-to-pdftoolbox-cli)).

Пример: `pdfToolbox --help saveasimg`. Некоторые команды создают или меняют
файлы; всегда задавайте отдельную папку результата и проверяйте помощь для
конкретной команды. Некоторые функции доступны **только через профиль**
Checks/Fixups, а не через отдельную команду.

## Информация, лицензия и кэш

| Команда | Назначение |
| --- | --- |
| `--help` (`-h`) | Общая справка или справка по одной команде |
| `--version` | Версия CLI |
| `--status` | Лицензия, коды ошибок и результатов; вывод может содержать данные владельца и ключ активации — не публиковать целиком |
| `--keycode` (`-k`) | Запрос кода для активации |
| `--activate` | Активация лицензии |
| `--deactivate` | Деактивация лицензии |
| `--emptyfontcache` | Очистка кэша шрифтов |
| `--emptyprofilecache` | Очистка кэша профилей |

См. [сведения о программе и статусе](https://help.callassoftware.com/m/pdftoolbox/l/657639-hints-and-troubleshooting-displaying-program-information).

## Проверка PDF, профили и отчёты

| Команда | Назначение |
| --- | --- |
| `--pdfa` | Проверка или преобразование PDF/A |
| `--quickpdfinfo` | Краткая информация о PDF |
| `--quickcheck` | QuickCheck по свойствам PDF |
| `--quickfix` | QuickFix PDF |
| `--enumprofiles` | Список профилей в `.kfpx` и их сводка |
| `--listvariables` | Переменные профиля `.kfpx` |
| `--visualizer` | Визуальный отчёт по свойствам печати |
| `--createseparations` | Цветоделения |
| `--compare` | Сравнение двух PDF |
| `--createcustomdict` | XML-шаблон словаря переводов |

Запуск Check/Fixup-профиля не имеет отдельного флага `--check` или `--fixup`:
`pdfToolbox [общие параметры] профиль.kfpx вход.pdf`. Для профиля доступен
`--report=...`, например JSON. См. [профили](https://help.callassoftware.com/m/pdftoolbox/l/660026-using-profiles),
[общие параметры](https://help.callassoftware.com/m/pdftoolbox/l/660027-general-command-line-options)
и [отчёты](https://help.callassoftware.com/m/pdftoolbox/l/660035-commands-related-to-reports).

## Страницы, спуск и компоновка

| Команда | Назначение |
| --- | --- |
| `--booklet` | Брошюра |
| `--nup` | Несколько страниц на листе |
| `--fillpage` | Заполнение листа повторениями |
| `--impose` | Спуск по runlist и sheet setup |
| `--mergeimpose` | Спуск по шаблону листа и runlist |
| `--slice` | Разделение по типу объектов |
| `--readerspreads` | Объединение страниц в читательский разворот |
| `--splithalf` | Разделение разворота на страницы |
| `--steprepeat` | Повторение макета на листе |
| `--splitpdf` | Разделение многостраничного PDF |
| `--mergepdf` | Объединение PDF |
| `--splitmergepdf` | Разделение на пакеты и последующее объединение |
| `--duplicatepage` | Дублирование страниц |
| `--splitatmark` | Разделение по метке/проверке |
| `--list` | Список конфигураций runlist и sheetconfig |
| `--listfonts` | Шрифты, доступные для runlist |
| `--tiling` | Разбиение большого PDF на тайлы |

См. [команды Arrange](https://help.callassoftware.com/m/pdftoolbox/l/660040-commands-related-to-arrange)
и [широкоформатную печать](https://help.callassoftware.com/m/pdftoolbox/l/660028-commands-related-to-large-format-printing).

## Представление и вывод страниц

| Команда | Назначение |
| --- | --- |
| `--presentation` | Подготовка презентации для Acrobat |
| `--handout` | Раздаточный материал |
| `--passepartout` | Паспарту |
| `--lighttable` | Размещение страниц на виртуальном световом столе |
| `--overlay` | Наложение содержимого на входной PDF |

См. [команды Present](https://help.callassoftware.com/m/pdftoolbox/l/660038-commands-related-to-present).

## Преобразование и извлечение содержимого

| Команда | Назначение |
| --- | --- |
| `--topdf` | Поддерживаемый не-PDF → PDF |
| `--createeps` | PDF → EPS |
| `--createps` | PDF → PostScript |
| `--saveasimg` | Рендер страниц в изображения |
| `--redistill` | Повторное создание PDF через PostScript |
| `--extracttext` | Извлечение текста |
| `--extractcontent` | Извлечение структуры текста/объектов |
| `--extractimages` | Извлечение изображений |
| `--collection` | PDF-коллекция из файлов папки |
| `--securepdf` | Защита PDF паролем |
| `--listembeddedfiles` | Список вложенных файлов PDF |
| `--extractembeddedfiles` | Извлечение вложений PDF |
| `--uncertify` | Удаление сертификата Preflight |
| `--modifyimages` | Извлечение и замена изображений по JSON |

См. [команды Document](https://help.callassoftware.com/m/pdftoolbox/l/660039-actions-document).

## Цвета, слои и структуры PDF

| Команда | Назначение |
| --- | --- |
| `--extracticcprofiles` | Извлечение ICC-профилей |
| `--cxf` | Извлечение/встраивание CxF-данных |
| `--spotify` | Создание DeviceN-изображения с подходящими Spot Color |
| `--spotifyremap` | Переназначение цветов Spotify blob в PDF/PNG |
| `--enumeratelayers` | Создание слоёв по шрифтам, ICC или плашечным цветам |
| `--importaslayer` | Импорт PDF как слоя |
| `--splitlayers` | Выгрузка слоёв/видов слоёв в отдельные PDF |
| `--extractxmpmetadata` | Извлечение XMP-метаданных |
| `--addbookmark` | Добавление закладок |
| `--modifystructures` | Работа с закладками, DPart и XMP |
| `--extractdieline` | Извлечение контуров вырубки |
| `--einvoice` | Создание, проверка и обработка электронных счетов |

См. [цветовые команды](https://help.callassoftware.com/m/pdftoolbox/l/660029-commands-related-to-colors)
и [слои](https://help.callassoftware.com/a/660036-commands-related-to-layers).

## Серверный и распределённый режимы

| Команда | Назначение |
| --- | --- |
| `--dispatcher` | Диспетчер распределённой обработки |
| `--satellite` | Узел распределённой обработки |
| `--monitor` | Состояние узлов у диспетчера |
| `--server` | Серверный режим |

Это отдельные режимы работы, **не требуются** для локального теста профилей
и не должны запускаться на production VM без отдельного плана. См.
[официальный раздел CLI](https://help.callassoftware.com/m/pdftoolbox/c/192208).

## Важные отличия от старых страниц документации

В документации могут встречаться устаревшие действия, например
`--convertcolors` как legacy-вариант. Установленная Linux CLI 17.0.682
**не показывает его в `--help`**; для новых задач используйте Fixup
`Convert colors` в профиле, как рекомендует [Callas](https://help.callassoftware.com/a/660029-commands-related-to-colors).
И наоборот, у CLI 17 есть новые команды, которых может не быть в старых
страницах руководства. При расхождении приоритет имеют помощь установленного
бинарника и проверка на тестовом файле.

Fixup `Generate bleed at page edges`, проверки тонких линий, цвета чёрного,
прозрачности и масок — **не самостоятельные CLI-команды**. Они настраиваются
как Checks/Fixups в Desktop, экспортируются в `.kfpx` и запускаются как профиль.
