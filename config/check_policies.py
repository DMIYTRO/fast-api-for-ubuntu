"""Политики проверки: какие находки PitStop влияют на статус заказа.

Модуль — только данные (без ввода-вывода). Логика применения — ``services/check_policy.py``.

Откуда берётся политика заказа
------------------------------
* файл в папке из ``IMAGE_MAGIC_STRICT_SOURCE_DIRS`` (по умолчанию ``CorelDRAW``) —
  политика ``full`` (как раньше: любая находка PitStop влияет на статус);
* файл в корне входной папки — политика ``IMAGE_MAGIC_INPUT_CHECK_POLICY``
  (по умолчанию ``basic``; ``full`` выключает облегчённую проверку целиком);
* заказ из нескольких файлов, хотя бы один из строгой папки, — ``full``;
* при запуске проверки можно выбрать «полную» для всего запуска.

Как это устроено
----------------
Находка PitStop сопоставляется с КАТЕГОРИЕЙ (``CheckCategory``) по ``action_id`` и,
если нужно, по тексту сообщения (одно действие 2078 описывает и RGB, и ICC, и ICC
прозрачности). Политика (``CheckPolicy``) для каждой категории говорит:

* ``"status"`` — находка влияет на статус как обычно;
* ``"info"``   — находка понижается до «информации»: показывается, но статус не меняет.

Находка без категории («новая», неизвестная) получает правило ``default`` политики.
В ``basic`` оно ``"status"``: неизвестная проверка не пропадает молча, а помечается
``policy_unclassified``.

Как добавить новую проверку
---------------------------
1. Добавьте одну строку ``CheckCategory(...)`` в ``CATEGORIES`` (порядок важен: первое
   совпадение выигрывает, поэтому узкие шаблоны ставьте раньше общих).
2. Добавьте по одному правилу ``"status"`` или ``"info"`` в каждую политику в
   ``POLICIES`` (без правила действует ``default`` политики).

Как добавить новую политику: новая запись в ``POLICIES``; её ключ можно указать в
``IMAGE_MAGIC_INPUT_CHECK_POLICY``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

STATUS = "status"
INFO = "info"
RULES = (STATUS, INFO)

POLICY_FULL = "full"
POLICY_BASIC = "basic"
#: Значение опции запуска: политика по папке (по умолчанию).
RUN_OPTION_AUTO = "auto"
RUN_OPTION_FULL = "full"
RUN_OPTIONS = (RUN_OPTION_AUTO, RUN_OPTION_FULL)

#: Папки внутри входной папки, файлы из которых проверяются полностью.
DEFAULT_STRICT_SOURCE_DIRS: tuple[str, ...] = ("CorelDRAW",)
DEFAULT_INPUT_CHECK_POLICY = POLICY_BASIC


#: Поля, которые политика добавляет в DTO PitStop (services/check_policy.py); при
#: повторном применении снимаются, при сохранении запуска кладутся в конверт.
ISSUE_POLICY_FIELDS = (
    "policy_category",
    "policy_category_label",
    "policy_demoted",
    "policy_original_severity",
    "policy_unclassified",
)
CHECK_POLICY_FIELDS = ("check_policy", "verdict_before_policy", "counts_before_policy")


@dataclass(frozen=True)
class PitStopMatch:
    """Находка PitStop: ``action_id`` и, при необходимости, регулярное выражение сообщения."""

    action_id: str
    message_pattern: str | None = None


@dataclass(frozen=True)
class CheckCategory:
    """Категория проверки: набор находок PitStop, которые политика решает вместе."""

    key: str
    label: str
    matches: tuple[PitStopMatch, ...]

    @property
    def pitstop_actions(self) -> frozenset[str]:
        return frozenset(item.action_id for item in self.matches)


def _actions(*action_ids: str) -> tuple[PitStopMatch, ...]:
    return tuple(PitStopMatch(action_id) for action_id in action_ids)


# Порядок значим: первое совпадение выигрывает.
CATEGORIES: tuple[CheckCategory, ...] = (
    CheckCategory("resolution", "Разрешение", _actions("1150", "1148", "1149", "1151")),
    CheckCategory(
        "color_model",
        "Цветовая модель",
        (
            PitStopMatch("2078", r"^(RGB|Lab) color"),
            PitStopMatch("3202"),
            PitStopMatch("3157"),
        ),
    ),
    # ICC прозрачной группы (2078) — часть «Прозрачности», а не цветового профиля.
    CheckCategory(
        "transparency",
        "Прозрачность",
        (PitStopMatch("3004"), PitStopMatch("2078", r"for transparency group")),
    ),
    CheckCategory(
        "icc_profile",
        "Цветовой профиль (ICC)",
        (PitStopMatch("2078", r"^ICC based is used(?! for transparency)"),),
    ),
    CheckCategory("clipping", "Обтравка", _actions("2079")),
    CheckCategory("page_size", "Размер и поворот страниц", _actions("1164", "4501")),
    CheckCategory("ink_coverage", "Суммарное покрытие краски", _actions("1153")),
    CheckCategory(
        "spot_colors", "Цветность / плашечные цвета", _actions("3159", "3156", "3184")
    ),
    CheckCategory(
        "fonts_missing", "Шрифты не встроены или неполные", _actions("1180", "1185")
    ),
    CheckCategory("font_types", "Типы шрифтов", _actions("1128", "1129", "1131")),
    CheckCategory("annotations", "Печатаемые аннотации", _actions("2022", "3180")),
    CheckCategory(
        "overprint", "Белый оверпринт / нет выворотки", _actions("3136")
    ),
    CheckCategory("invisible_objects", "Невидимые объекты", _actions("3138")),
    CheckCategory("thin_lines", "Тонкие линии", _actions("3140")),
    CheckCategory("pdf_actions", "Действия PDF", _actions("3211")),
)

CATEGORIES_BY_KEY: Mapping[str, CheckCategory] = MappingProxyType(
    {category.key: category for category in CATEGORIES}
)


@dataclass(frozen=True)
class CheckPolicy:
    """Что делать с находками каждой категории; ``default`` — для категорий без правила."""

    key: str
    label: str
    rules: Mapping[str, str] = field(default_factory=dict)
    default: str = STATUS

    def rule_for(self, category_key: str | None) -> str:
        if category_key is None:
            return self.default
        return self.rules.get(category_key, self.default)


POLICIES: Mapping[str, CheckPolicy] = MappingProxyType(
    {
        # Как раньше: любая находка PitStop влияет на статус.
        POLICY_FULL: CheckPolicy(POLICY_FULL, "Полная проверка", {}, STATUS),
        # Входная папка: статус задают только разрешение, цвет, ICC, размер, красочность,
        # шрифты, печатаемые аннотации и белый оверпринт; остальное — справочно.
        POLICY_BASIC: CheckPolicy(
            POLICY_BASIC,
            "Облегчённая проверка (Input)",
            {
                "resolution": STATUS,
                "color_model": STATUS,
                "icc_profile": STATUS,
                "page_size": STATUS,
                "ink_coverage": STATUS,
                "spot_colors": STATUS,
                "fonts_missing": STATUS,
                "annotations": STATUS,
                "overprint": STATUS,
                "transparency": INFO,
                "clipping": INFO,
                "font_types": INFO,
                "invisible_objects": INFO,
                "thin_lines": INFO,
                "pdf_actions": INFO,
            },
            STATUS,
        ),
    }
)

assert all(
    rule in RULES
    for policy in POLICIES.values()
    for rule in (*policy.rules.values(), policy.default)
), "правило политики должно быть 'status' или 'info'"
