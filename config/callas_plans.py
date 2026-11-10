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


CALLAS_PLANS: dict[str, CallasPlan] = {
    "size_correction": CallasPlan(
        key="size_correction",
        title="Коррекция размера макета",
        profile="Image Magic/size_correction",
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
