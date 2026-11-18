from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

#: Способы коррекции размера в пометке ``FileCheck.size_fix`` (лестница по порядку).
#: ``canvas`` — без масштабирования: белые поля и/или центральная обрезка (файлы с меткой
#: «без масштабирования» в имени, ``PrePressProfile.no_scale_name_markers``).
SIZE_FIX_METHODS = ("reveal", "bleed", "stretch", "shrink", "fill", "canvas")


def make_size_fix(
    method: str,
    auto: bool,
    from_mm: Sequence[float],
    to_mm: Sequence[float],
    detail: str,
) -> dict[str, object]:
    """Пометка коррекции размера для ``FileCheck.size_fix`` (JSON-совместимая).

    ``auto`` — коррекция применена автоматически; ``False`` — подтверждена оператором
    или принята вручную.
    """
    if method not in SIZE_FIX_METHODS:
        raise ValueError(f"неизвестный способ коррекции размера: {method!r}")
    return {
        "method": method,
        "auto": bool(auto),
        "from_mm": [round(float(value), 2) for value in from_mm],
        "to_mm": [round(float(value), 2) for value in to_mm],
        "detail": str(detail),
    }


@dataclass(frozen=True)
class ParsedFilename:
    customer_id: str
    order_id: str
    width_mm: float
    height_mm: float
    front_colors: int
    back_colors: int
    side: str


@dataclass
class FileCheck:
    path: Path
    parsed: Optional[ParsedFilename] = None
    actual_width_mm: Optional[float] = None
    actual_height_mm: Optional[float] = None
    dpi: Optional[float] = None
    dpi_x: Optional[float] = None
    dpi_y: Optional[float] = None
    width_px: Optional[int] = None
    height_px: Optional[int] = None
    actual_format: Optional[str] = None
    colorspace: Optional[str] = None
    has_alpha: Optional[bool] = None
    has_unflattened_layers: Optional[bool] = None
    channels: Optional[str] = None
    tiff_page_count: Optional[int] = None
    tiff_layer_count: Optional[int] = None
    tiff_flattened: bool = False
    tiff_visible_layer_count: Optional[int] = None
    tiff_hidden_layer_count: Optional[int] = None
    tiff_flatten_strategy: Optional[str] = None
    size_mb: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    processing_actions: list[dict[str, object]] = field(default_factory=list)
    needs_resample: bool = False
    resample_target_mm: Optional[tuple[float, float]] = None
    resample_decision: str = "accept"
    resample_reason: Optional[str] = None
    resample_scale: float = 1.0
    resample_crop_mm: tuple[float, float] = (0.0, 0.0)
    resample_effective_dpi: Optional[tuple[float, float]] = None
    resample_confirmed: Optional[bool] = None
    # Исходное DPI растра, который будет уменьшен до целевого (размер был верным).
    downsampled_from_dpi: Optional[float] = None
    # Имя файла содержит метку «без масштабирования» (``PrePressProfile.is_no_scale_name``):
    # коррекция размера — только белые поля и/или центральная обрезка (способ ``canvas``).
    no_scale: bool = False
    # Ожидаемый размер файла с вылетами, мм (``PrePressProfile.expected_size_mm``): формат из
    # имени + вылеты или эталон по метке ((SRA3) 314×434, (SRA3P) 324×471).
    expected_mm: Optional[tuple[float, float]] = None
    rotation_degrees: int = 0
    orientation_verified: bool = False
    # PDF-specific facts.  These are optional so existing image workflows and
    # direct FileCheck callers stay compatible.
    page_count: Optional[int] = None
    pdf_pages: tuple[object, ...] = ()
    pdf_colorspaces: tuple[str, ...] = ()
    pdf_min_dpi: Optional[float] = None
    pdf_content_type: Optional[str] = None
    # Коррекция размера PDF (callas QuickFix): номер страницы → PdfPagePlan
    # (способ, формат с вылетами в ориентации страницы). Пусто — PDF не меняется.
    pdf_resize_plans: dict[int, object] = field(default_factory=dict)
    # Принятое оператором исправление Callas исходного PDF: содержимое читается из
    # ``content_path`` (копия ``fixed.pdf`` в ``PDF/CallasFix/<имя>.source/``), а имя,
    # сторона, перемещения и идентификатор файла остаются за исходным ``path``.
    # ``callas_source_fix`` — сведения о принятом исправлении (план, подпись исходника).
    content_path: Optional[Path] = None
    callas_source_fix: Optional[dict[str, object]] = None
    # Пометка коррекции размера (``make_size_fix``): способ лестницы, авто/подтверждено,
    # размер до и после, пояснение. Заменяет строки предупреждений о коррекции.
    size_fix: Optional[dict[str, object]] = None

    @property
    def input_path(self) -> Path:
        """Файл, из которого читается содержимое: исправленный PDF или сам исходник."""
        return self.content_path or self.path

    @property
    def passed(self) -> bool:
        return not self.errors and self.resample_decision != "ask_confirmation"


@dataclass
class OrderCheck:
    order_id: str
    customer_id: str
    files: list[FileCheck] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Enriched from Sborka orderinfo when that optional integration is enabled.
    postpress: dict[str, object] | None = None
    # Итог автоматического перевода RGB → CMYK итогового PDF (None — не требовался).
    color_conversion: dict[str, object] | None = None
    # Допечатные проверки callas итогового PDF: шрифты, ppi, плашка K100 (services/callas/prepress.py).
    prepress: dict[str, object] | None = None

    @property
    def aggregate_id(self) -> str:
        return f"{self.customer_id}:{self.order_id}"

    @property
    def passed(self) -> bool:
        return not self.errors and all(item.passed for item in self.files)
