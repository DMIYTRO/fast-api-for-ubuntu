"""Подсказки причин возврата на доработку: какой сигнал проверки → какой текст справочника.

Модуль — только данные (без ввода-вывода). Логика распознавания сигналов —
``services/return_suggestions.py``; справочник — ``core/return_reasons.load_return_reasons``
(HTML «Цифра - Загальне.html», тексты без завершающих «.;»).

* ``signal``     — что нашла проверка (распознаётся в ``services/return_suggestions.py``);
* ``text``       — ДОСЛОВНЫЙ текст пункта справочника (оператору, по-украински);
* ``confidence`` — ``high`` (интерфейс отмечает пункт сам), ``medium`` / ``low`` — только
  предлагается. Ставьте ``high`` лишь когда сигнал однозначно означает этот пункт;
* ``alternatives`` — другие ДОСЛОВНЫЕ формулировки того же смысла из справочника (операторы
  пишут и их): выводятся сразу после основного текста с тем же сигналом, уверенностью
  не выше ``medium`` и без ``preselect``.

Порядок кортежа ``RULES`` — важность: подсказки выводятся в этом порядке.

Правило ``size_exact_catalogue`` динамическое (``dynamic=True``): его ``text`` — только
описание, а пункт выбирается при работе среди «Розмір макета з урахуванням доливок повинен
бути AхBмм» по ожидаемому размеру файла.

Как добавить подсказку: распознавание сигнала в ``services/return_suggestions.py`` и одна
строка ``ReturnReasonRule`` здесь; ``tests/test_return_suggestions.py`` проверяет, что текст
есть в справочнике дословно.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
CONFIDENCES = (HIGH, MEDIUM, LOW)

Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class ReturnReasonRule:
    """Сигнал проверки → пункт справочника причин возврата."""

    signal: str
    text: str
    confidence: Confidence
    #: Текст выбирается при работе (``text`` — описание, а не пункт справочника).
    dynamic: bool = False
    #: Другие пункты справочника того же смысла: предлагаются после ``text``, без отметки.
    alternatives: tuple[str, ...] = ()


SIZE_EXACT_CATALOGUE = "size_exact_catalogue"
SIZE_MISMATCH = "size_mismatch"

#: Метка в имени файла → сигнал эталонного размера (``PrePressProfile.reference_sizes_mm``,
#: ``no_scale_name_markers``). Скобки входят в метку, поэтому «(SRA3P)» не содержит «(SRA3)».
SIZE_REFERENCE_MARKERS = MappingProxyType(
    {
        "size_reference_sra3p": "(SRA3P)",
        "size_reference_sra3": "(SRA3)",
        "size_reference_ban": "(BAN)",
    }
)

#: Технические сепарации для PitStop 3159 сверх ``DEFAULT_OVERPRINT_IGNORE_SPOTS``: лак и маска
#: выборочного лака («Clear», в справочнике и кириллицей «Сlear») — не «Pantone» клиента.
#: В production-отчётах: «Those 1 separations are: LAK / lak / Clear / White».
EXTRA_TECHNICAL_SEPARATIONS: tuple[str, ...] = ("Lak", "Clear", "\u0421lear")

#: Пункт «Брошури на скобі»: о RGB операторы пишут именно его (возвраты 2026-11, а не
#: «Макет має бути виконаний в кольоровій моделі CMYK»).
RGB_TEXT = "Всі елементи повинні бути в CMYK, присутні RGB об'єкти"

RULES: tuple[ReturnReasonRule, ...] = (
    ReturnReasonRule(
        "low_dpi",
        "Низька роздільна здатність, має бути 300 dpi (точок на Inch/Дюйм)",
        HIGH,
    ),
    ReturnReasonRule("page_count", "Кількість сторінок не збігається з оформленним", HIGH),
    ReturnReasonRule(
        "extra_back_one_sided",
        "Оформили односторонній друк, а завантажили 2 сторони. Переоформіть замовлення як "
        "двосторонній друк або завантажте правильний макет",
        HIGH,
    ),
    ReturnReasonRule(
        "missing_back",
        "Немає однієї зі сторін, оформили двосторонній друк, завантажили тільки 1 сторону",
        HIGH,
    ),
    ReturnReasonRule(
        "missing_face",
        "Неправильно завантажені сторони. У поле «Лице» необхідно завантажувати лицевий "
        "розворот, а в поле «Зворот» - внутрішню частину макета",
        MEDIUM,
    ),
    # Лицо и оборот разной ориентации (сторона повёрнута автоматически или прежняя ошибка):
    # только предлагается — после проверки превью заказ обычно печатают.
    ReturnReasonRule(
        "side_orientation",
        "Щоб уникнути непорозумінь, макет необхідно подавати в одній орієнтації "
        "(горизонтально або вертикально)",
        MEDIUM,
    ),
    ReturnReasonRule(
        "size_reference_sra3",
        "Розмір макета для друку на форматі SRA3 повинен бути 314х434 мм",
        HIGH,
    ),
    ReturnReasonRule(
        "size_reference_sra3p",
        "Розмір макета для друку на форматі SRA3P повинен бути 324х471 мм",
        HIGH,
    ),
    ReturnReasonRule(
        "size_reference_ban",
        "Розмір макета для друку на форматі BAN (700х320 мм) повинен бути 314x680 мм",
        HIGH,
    ),
    ReturnReasonRule(
        SIZE_EXACT_CATALOGUE,
        "Розмір макета з урахуванням доливок повинен бути AхBмм (по ожидаемому размеру файла)",
        HIGH,
        dynamic=True,
    ),
    ReturnReasonRule(
        SIZE_MISMATCH,
        "Розмір макета не збігається з оформленим, переоформіть замовлення або завантажте "
        "правильний макет",
        MEDIUM,
    ),
    ReturnReasonRule("tiff_layers", "Шари потрібно зводити в Background/Фон", HIGH),
    ReturnReasonRule("tiff_alpha", "Альфа канали неприпустимі", HIGH),
    ReturnReasonRule(
        "file_unreadable",
        "Битий файл. Макет не відкривається. Збережіть макет ще раз і перезавантажте його",
        HIGH,
    ),
    ReturnReasonRule(
        "icc",
        "Вбудовані колірні профілі неприпустимі. При друці колір може спотворитися",
        HIGH,
    ),
    ReturnReasonRule("rgb_left", RGB_TEXT, HIGH),
    # Цветовая модель файла не из профиля и не RGB (Lab…): общий пункт о CMYK.
    ReturnReasonRule("color_model", "Макет має бути виконаний в кольоровій моделі CMYK", HIGH),
    # RGB был, но переведён в CMYK: операторы всё равно пишут о нём клиенту — только предложить.
    ReturnReasonRule("rgb_converted", RGB_TEXT, MEDIUM),
    ReturnReasonRule(
        "spot_colors",
        "Всі елементи мають бути в CMYK, Panton'и / Плашкові кольори недопустимі. На друці "
        "може вийти зовсім інший колір",
        HIGH,
        alternatives=(
            "Всі елементи повинні бути в CMYK, Panton'и неприпустимі. При друці може вийти "
            "абсолютно інший колір",
        ),
    ),
    ReturnReasonRule(
        "transparency",
        "Прозорості і растрові зображення з прозорим фоном неприпустимі",
        MEDIUM,
        alternatives=(
            "Складні спецефекти (тіні, лінзи, текстури, градієнтні заливки і ін.), растрові "
            "зображення на прозорому тлі неприпустимі - раструються разом з фоном без прозорості",
        ),
    ),
    ReturnReasonRule(
        "fonts",
        "Текст не переведений в криві. Шрифти вбудовані некоректно , при друці текст може злетіти",
        HIGH,
    ),
    ReturnReasonRule("ink_max", "Максимальна барвистість - 300%", HIGH),
    # Светлые участки callas (порог — IMAGE_MAGIC_INK_MIN_PERCENT, в production 20): светлый
    # фон бывает задуман клиентом, поэтому только предложить.
    ReturnReasonRule("ink_min", "Мінімальна барвистість - 20%", MEDIUM),
    ReturnReasonRule(
        "white_overprint",
        "Помітили випадково, OVERPRINT на білому кольорі неприпустимий, це повністю "
        "відсутній елемент",
        HIGH,
    ),
    ReturnReasonRule(
        "thin_lines",
        "Дуже тонкі елементи , можуть заплисти, допустимо несуміщення до 0,2мм",
        LOW,
    ),
)

RULES_BY_SIGNAL = MappingProxyType({rule.signal: rule for rule in RULES})
#: Сигналы размера, при которых общий ``size_mismatch`` не нужен.
SPECIFIC_SIZE_SIGNALS = frozenset((*SIZE_REFERENCE_MARKERS, SIZE_EXACT_CATALOGUE))

assert all(rule.confidence in CONFIDENCES for rule in RULES), "неизвестная уверенность"
assert len(RULES_BY_SIGNAL) == len(RULES), "сигнал правила должен быть уникальным"
assert not any(rule.dynamic and rule.alternatives for rule in RULES), "у динамического правила нет вариантов"
assert set(SIZE_REFERENCE_MARKERS) <= set(RULES_BY_SIGNAL), "метке размера нужно правило"
