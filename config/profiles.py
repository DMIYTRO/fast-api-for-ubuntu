"""Профили направлений допечатной подготовки."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping


#: Предел автоматической коррекции размера по умолчанию, мм обрезки.
DEFAULT_AUTO_CORRECT_MAX_MM = 4.0
#: Жёсткий максимум коррекции с подтверждением оператора, мм обрезки.
DEFAULT_CORRECTION_CONFIRM_MAX_MM = 10.0
#: Предел автоматического пропорционального уменьшения, мм: насколько макет может быть
#: больше формата с вылетами (по большей из сторон), чтобы уменьшение прошло само.
DEFAULT_SHRINK_AUTO_MAX_MM = 2.0
#: Разрешение растра, выше которого файл с подходящим размером уменьшается до целевого DPI.
DEFAULT_MAX_DPI = 500.0
#: Целевое разрешение профилей по умолчанию.
DEFAULT_TARGET_DPI = 300.0
#: Метки в имени файла, при которых макет никогда не масштабируется: размер приводится
#: только белыми полями (холст больше) или центральной обрезкой (холст меньше).
#: Сравнение — подстрока без учёта регистра, скобки входят в метку: «(SRA3)» не
#: совпадает с «(SRA3P)», поэтому обе перечислены.
DEFAULT_NO_SCALE_NAME_MARKERS = ("(SRA3P)", "(SRA3)", "(BAN)")
#: Эталонный размер файла по метке в имени, мм, — норма УЖЕ с вылетами (вылеты к нему не
#: прибавляются, размер из имени не используется); ориентация любая (314×434 == 434×314).
#: Метка сравнивается как в ``DEFAULT_NO_SCALE_NAME_MARKERS``.
DEFAULT_REFERENCE_SIZES_MM = MappingProxyType({
    "(SRA3)": (314.0, 434.0),
    "(SRA3P)": (324.0, 471.0),
})


@dataclass(frozen=True)
class PrePressProfile:
    """Набор правил одного производственного направления."""

    direction: str
    name: str
    target_dpi: float = 300.0
    min_dpi: float = 250.0
    allowed_colorspaces: tuple[str, ...] = ("CMYK", "COLORSEPARATION")
    max_file_size_mb: float = 2000.0
    safe_zone_mm: float = 4.0
    bleed_mm: float = 2.0
    size_tolerance_mm: float = 0.5
    metadata_tolerance_mm: float = 0.1
    # Предел автоматической коррекции: максимальная обрезка (мм) при
    # пропорциональном масштабировании «с покрытием» и центральной обрезке.
    # Обрезка между ``auto_crop_mm`` и ``confirm_crop_mm`` требует подтверждения
    # оператора; выше ``confirm_crop_mm`` — отклонение. Значения переопределяются
    # через ``with_correction_limit`` (IMAGE_MAGIC_AUTO_CORRECT_MAX_MM и
    # IMAGE_MAGIC_CORRECTION_CONFIRM_MAX_MM).
    auto_crop_mm: float = DEFAULT_AUTO_CORRECT_MAX_MM
    confirm_crop_mm: float = DEFAULT_CORRECTION_CONFIRM_MAX_MM
    # Пропорциональное уменьшение (масштаб < 1) проходит автоматически, только если
    # макет больше формата не более чем на ``shrink_auto_mm`` по каждой стороне или
    # уменьшение поднимает низкое разрешение растра до ``min_dpi``; иначе — с
    # подтверждением оператора (пределы обрезки и отказа не меняются).
    shrink_auto_mm: float = DEFAULT_SHRINK_AUTO_MAX_MM
    # Вытягивание PDF (callas ``stretch_to_fill``) — неравномерный масштаб по ширине и
    # высоте, допустимый только для PDF и только если пропорциональное заполнение не
    # проходит автоматически. Искажение пропорций, %: до ``auto`` — само, до ``confirm`` —
    # с подтверждением оператора, выше — не применяется. 0 — вытягивание выключено.
    pdf_stretch_auto_pct: float = 1.0
    pdf_stretch_confirm_pct: float = 3.0
    # Растры с подходящим размером, но разрешением выше ``max_dpi``, уменьшаются
    # до ``target_dpi`` (без обрезки и без смены размера). Переопределяется через
    # ``with_downsample`` (IMAGE_MAGIC_MAX_DPI и IMAGE_MAGIC_DOWNSAMPLE_HIGH_DPI_ENABLED).
    max_dpi: float = DEFAULT_MAX_DPI
    downsample_high_dpi: bool = True
    # Пороги красочности (callas). Минимум — сумма красок В ТОЧКЕ (TAC), ниже которой
    # участок считается светлым: «Светлые участки» ищутся по цветоделениям, не по
    # среднему (services/callas/ink_coverage.py). Максимум сравнивается с
    # ``total_sum_percent`` страницы (сумма средних покрытий по рамке): выше — «Перелив».
    # Пороги задаются производственным профилем или настройками;
    # демонстрационные 20/250 не являются производственными нормативами.
    ink_min_percent: float | None = None
    ink_max_percent: float | None = None
    allowed_color_modes: tuple[tuple[int, int], ...] = (
        (1, 0),
        (1, 1),
        (4, 0),
        (4, 4),
        (5, 0),
        (5, 5),
        (6, 0),
        (6, 6),
    )
    sheet_trim: Mapping[str, str] | None = None
    cut_lines: Mapping[str, str] | None = None
    spots: tuple[str, ...] = field(default_factory=tuple)
    # Файлы с этими метками в имени не масштабируются никаким способом лестницы
    # (reveal, Callas-поля, stretch, shrink, fill): только белые поля или обрезка
    # по центру, способ ``canvas``. Пустой кортеж выключает правило.
    no_scale_name_markers: tuple[str, ...] = DEFAULT_NO_SCALE_NAME_MARKERS
    # Пределы ``canvas`` для таких файлов считаются ПО КАЖДОМУ КРАЮ (обрезка или поля
    # по центру делятся поровну): до ``canvas_auto_edge_mm`` — автоматически, до
    # ``canvas_confirm_edge_mm`` — с подтверждением оператора, больше — отказ. Заказ
    # 262422481: лист 330×487 при (SRA3P) 324×471 — по 3 и по 8 мм с краёв → подтверждение.
    canvas_auto_edge_mm: float = 4.0
    canvas_confirm_edge_mm: float = 8.0

    # Эталонный размер файла (с вылетами) по метке в имени — см. ``expected_size_mm``.
    reference_sizes_mm: Mapping[str, tuple[float, float]] = DEFAULT_REFERENCE_SIZES_MM

    def reference_size_mm(self, name: str) -> tuple[float, float] | None:
        """Эталонный размер с вылетами по метке в имени файла или ``None``.

        Если подходят несколько меток, берётся самая длинная («(SRA3P)» точнее «(SRA3)»).
        """
        lowered = name.casefold()
        matches = [
            marker for marker in self.reference_sizes_mm
            if marker and marker.casefold() in lowered
        ]
        if not matches:
            return None
        width, height = self.reference_sizes_mm[max(matches, key=len)]
        return (float(width), float(height))

    def expected_size_mm(
        self, name: str, width_mm: float, height_mm: float, extra_mm: float | None = None
    ) -> tuple[float, float]:
        """Ожидаемый размер файла с вылетами: эталон по метке или формат из имени + вылеты."""
        reference = self.reference_size_mm(name)
        if reference is not None:
            return reference
        extra = self.size_extra_mm if extra_mm is None else float(extra_mm)
        return (float(width_mm) + extra, float(height_mm) + extra)

    def is_no_scale_name(self, name: str) -> bool:
        """Имя файла содержит метку «без масштабирования» (без учёта регистра)."""
        lowered = name.casefold()
        return any(marker and marker.casefold() in lowered for marker in self.no_scale_name_markers)

    def with_correction_limit(
        self, limit_mm: float, confirm_max_mm: float | None = None
    ) -> "PrePressProfile":
        """Профиль с пределом автокоррекции и максимумом коррекции с подтверждением."""
        limit = max(0.0, float(limit_mm))
        cap = self.confirm_crop_mm if confirm_max_mm is None else float(confirm_max_mm)
        return replace(self, auto_crop_mm=limit, confirm_crop_mm=max(limit, cap))

    def with_downsample(
        self, max_dpi: float | None = None, enabled: bool | None = None
    ) -> "PrePressProfile":
        """Профиль с порогом DPI для автоматического уменьшения растров."""
        return replace(
            self,
            max_dpi=self.max_dpi if max_dpi is None else max(float(max_dpi), self.target_dpi),
            downsample_high_dpi=(
                self.downsample_high_dpi if enabled is None else bool(enabled)
            ),
        )

    def with_no_scale_markers(self, markers: "tuple[str, ...] | list[str]") -> "PrePressProfile":
        """Профиль с метками «без масштабирования» (IMAGE_MAGIC_NO_SCALE_NAME_MARKERS).

        Пустые и повторяющиеся метки отбрасываются; пустой список выключает правило.
        """
        cleaned: list[str] = []
        for marker in markers:
            value = str(marker).strip()
            if value and value.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(value)
        return replace(self, no_scale_name_markers=tuple(cleaned))

    @property
    def size_extra_mm(self) -> float:
        """Общий припуск по размеру: вылет с обеих сторон."""
        return self.bleed_mm * 2.0


PROFILES: Mapping[str, PrePressProfile] = MappingProxyType(
    {
        "digital": PrePressProfile(
            direction="digital",
            name="Цифровая печать",
            sheet_trim=MappingProxyType(
                {
                    "320x450": "314x434",
                    "330x487": "324x471",
                }
            ),
            cut_lines=MappingProxyType(
                {
                    "рез": "green 100/0/100/0",
                    "биг": "red 0/100/100/0",
                }
            ),
            spots=("White", "Clear"),
        ),
        "offset": PrePressProfile(
            direction="offset",
            name="Офсетная печать",
            sheet_trim=None,
            cut_lines=None,
            spots=(),
        ),
    }
)

DEFAULT_DIRECTION = "digital"
DEFAULT_PROFILE = PROFILES[DEFAULT_DIRECTION]


def get_profile(direction: str = DEFAULT_DIRECTION) -> PrePressProfile:
    """Возвращает профиль или понятную ошибку со списком направлений."""
    try:
        return PROFILES[direction]
    except KeyError as exc:
        available = ", ".join(PROFILES)
        raise ValueError(
            f"неизвестное направление {direction!r}; доступно: {available}"
        ) from exc
