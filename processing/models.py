from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


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
    size_mb: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_resample: bool = False
    resample_target_mm: Optional[tuple[float, float]] = None
    resample_decision: str = "accept"
    resample_reason: Optional[str] = None
    resample_scale: float = 1.0
    resample_crop_mm: tuple[float, float] = (0.0, 0.0)
    resample_effective_dpi: Optional[tuple[float, float]] = None
    resample_confirmed: Optional[bool] = None
    rotation_degrees: int = 0
    orientation_verified: bool = False
    # PDF-specific facts.  These are optional so existing image workflows and
    # direct FileCheck callers stay compatible.
    page_count: Optional[int] = None
    pdf_pages: tuple[object, ...] = ()
    pdf_colorspaces: tuple[str, ...] = ()
    pdf_min_dpi: Optional[float] = None
    pdf_content_type: Optional[str] = None

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

    @property
    def aggregate_id(self) -> str:
        return f"{self.customer_id}:{self.order_id}"

    @property
    def passed(self) -> bool:
        return not self.errors and all(item.passed for item in self.files)
