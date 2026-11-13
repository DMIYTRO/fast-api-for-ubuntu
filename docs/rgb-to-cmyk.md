# Автоперевод RGB → CMYK через callas (2026-11-12)

## Что сделано

Итоговый PDF с RGB автоматически переводится в CMYK процесс-планом callas pdfToolbox.
Если перевод прошёл, в карточке заказа появляется значок «RGB» (буквы красная, зелёная,
синяя), а RGB-предупреждения и RGB-ошибка PitStop не показываются. Заказ без других
проблем становится зелёным. Если RGB не переводился, значка нет.

- Включение: `IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_ENABLED=1` (по умолчанию выключено; на проде
  задаётся в `/etc/default/fastapi-app-callas`).
- Профиль: `IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE`, по умолчанию (с 2026-11-14)
  `Image Magic/NEW_Convert color to 4c for digital printing` (профиль `.kfpx` из
  `callas_profiles/`, развёрнут на сервере callas в каталоге «Image Magic»). Он заменил
  `Convert colors/Convert to CMYK, keep spot colors (ISO Coated v2 (ECI))`: цель PSO Coated
  v3, OutputIntent не встраивается, RGB, ICC-RGB и Lab уходят в DeviceCMYK, ICC-CMYK
  снимается без пересчёта. Старый профиль можно вернуть этой же переменной. Подробности
  профиля и результаты тестов: [callas-profiles.md](callas-profiles.md).
- Где применяется: сборка PDF (digital и offset), ручной Callas-fix, сборка печати из
  слоёв TIFF, `recheck_pdf_revision`.
- Порядок: склейка → перевод в CMYK → проверка PDF → контрольный рендер → PitStop. PitStop
  видит уже CMYK-файл.

## Значок «ICC off»

Перед конвертацией стандартный профиль «PDF analysis/List objects using ICC or Lab or
calibrated color» ищет объекты в ICC, Lab или калиброванном цвете. Если они были и
конвертация прошла, в `color_conversion` пишутся `icc_removed: true` и `icc_pages`, а в
карточке рядом со значком RGB появляется «ICC off» (подсказка: ICC-профиль снят, цвет
переведён в DeviceCMYK). Сбой определения ICC конвертацию не блокирует (`icc_error`).

## Как работает

- «Был RGB» решает `find_rgb_pages` до и после, а не `fixups_applied`: на CMYK-макете с
  прозрачностью новый профиль даёт `fixups_applied=2` (Set transparency blend color space) без RGB.
- `core/pdf_inspector.py::find_rgb_pages` находит RGB: растры с 3 компонентами (Lab не
  считается) и операторы `rg`/`RG`/`/DeviceRGB cs` в потоке страницы. Объекты внутри Form
  XObject не просматриваются.
- `services/callas/color_conversion.py::convert_rgb_to_cmyk` конвертирует на месте, только
  если RGB найден. Результат заменяет PDF, лишь когда число страниц не изменилось и RGB не
  осталось. Итог пишется в `order["color_conversion"]` (`status`, `pages`, `profile`,
  `error`, `fixups_applied`); миграция БД не нужна.
- При любом сбое PDF остаётся в RGB, а в карточке появляется предупреждение «Не удалось
  перевести RGB в CMYK: …».
- Исходные изображения не перекрашиваются, Gray/Lab/spot не трогаются.

## Другие допечатные проверки и действия (2026-11-14)

См. [callas-profiles.md](callas-profiles.md), раздел «Что делает приложение».

## Известные особенности

- Чёрный текст после перевода получается K-only, а не rich black.
- Другие замечания PitStop (например, «Clipping objects found») перевод не исправляет и
  может не дать зелёный статус.
- Ошибка «Исходный файл combined.pdf изменился во время обработки callas»: клиент callas
  сравнивает size/mtime входа до и после; на сетевом диске mtime мог «плыть». Исправлено
  (PR #10): callas получает локальную копию PDF.
- Старые отчёты не обновляются: изменения видны только на новой проверке.
- После деплоя UI обновляется без кеша: Ctrl+F5 (Windows/Linux) или Cmd+Shift+R (Safari
  и Chrome на Mac).

## Pull requests

- #9 — перевод RGB → CMYK и значок в карточке.
- #10 — локальный снимок PDF для callas.
