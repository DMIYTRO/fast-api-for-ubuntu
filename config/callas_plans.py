"""Каталог процесс-планов Callas, доступных оператору на карточке заказа.

Сервер callas принимает профиль только по имени из своего каталога профилей
(``/opt/pdfToolbox-17.0.682/var/Profiles``). Оператору показывается короткий список
из этого файла, а не все штатные профили. Новый план добавляется одной записью.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallasPlan:
    key: str
    title: str
    #: Имя профиля на сервере callas (папка/файл без расширения ``.kfpx``).
    profile: str
    mode: str = "fix"
    directions: tuple[str, ...] = ("digital", "offset")
    #: Результат всегда проходит просмотр оператором; поле зарезервировано для
    #: будущих «безопасных» планов, которые можно будет принимать без просмотра.
    requires_confirmation: bool = True
    #: Подсказка кнопки: какой Fixup применится и что изменится.
    description: str = ""
    #: Предупреждение оператору (ограничения профиля), показывается рядом с результатом.
    warning: str = ""
    #: Перед запуском в PDF не должно остаться RGB (иначе RGB-чёрный не станет K100).
    requires_cmyk: bool = False
    #: Сообщение, если профиль сообщил об ошибках исправления и ничего не исправил.
    unfixable_message: str = ""
    #: Сообщение, если профиль ничего не изменил (``fixups_applied == 0``): результат не нужен.
    no_change_message: str = ""


CALLAS_PLANS: dict[str, CallasPlan] = {
    "size_correction": CallasPlan(
        key="size_correction",
        title="Коррекция размера макета",
        profile="Image Magic/size_correction",
    ),
    "fonts_to_outlines": CallasPlan(
        key="fonts_to_outlines",
        title="Перевод шрифтов в кривые",
        profile="Image Magic/Convert fonts to outlines",
        description=(
            "Fixup «Convert fonts to outlines» (callas): встроенные шрифты (TrueType, Type1, Type3) "
            "становятся векторными контурами. Исходный PDF сохраняется; после кривых текст нельзя "
            "скопировать или найти поиском."
        ),
        warning=(
            "Невстроенные шрифты (например Helvetica, Courier) профиль не переводит. "
            "Невидимый текст остаётся."
        ),
        unfixable_message="Шрифты не встроены, перевод в кривые невозможен, нужна проверка макета.",
        no_change_message="Живых шрифтов для перевода в кривые не найдено.",
    ),
    "black_to_rich": CallasPlan(
        key="black_to_rich",
        title="Плашка 100% K в составной чёрный",
        profile="Image Magic/Pure CMYK black to rich black (C60 M40 Y40 K100)",
        description=(
            "Fixup «Map colors» (callas): чистый K100 (векторы и CMYK-растры) становится "
            "C60 M40 Y40 K100. Тоны K (например K40) и RGB не меняются."
        ),
        warning=(
            "Профиль меняет ВСЕ объекты с чистым K100, не только плашку: мелкий текст и штрихкоды "
            "в составном чёрном рискуют потерять резкость из-за совмещения красок. "
            "Проверьте наложение перед принятием."
        ),
        requires_cmyk=True,
        no_change_message="Чистого K100 в PDF не найдено: профиль ничего не изменил.",
    ),
    "remove_clipping": CallasPlan(
        key="remove_clipping",
        title="Удаление обтравки",
        profile="Image Magic/Remove all clipping paths",
        description=(
            "Fixup «RemoveClipPathes» (callas): удаляет все обтравки (clipping paths), "
            "в том числе служебную обтравку по границе страницы. Замечание «Clipping objects found» "
            "приходит из Enfocus PitStop (ActionID 2079), а не из callas."
        ),
        warning=(
            "Снятие масок может проявить скрытое содержимое, которое сейчас обрезано "
            "(например круглая вырубка). Сравните «до» и «после» и принимайте, только если вид не испортился."
        ),
        no_change_message="Обтравок в PDF не найдено: профиль ничего не изменил.",
    ),
}


def get_plan(key: str) -> CallasPlan | None:
    return CALLAS_PLANS.get(key)


def plans_for_direction(direction: str | None) -> list[CallasPlan]:
    return [
        plan
        for plan in CALLAS_PLANS.values()
        if not direction or direction in plan.directions
    ]
