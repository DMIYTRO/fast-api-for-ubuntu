"""Reapply local artwork rules to the PDF produced by a manual correction."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from config.profiles import get_profile
from core.pdf_inspector import PdfInspection, inspect_pdf


def validate_pdf_revision(path: Path, order: dict[str, Any], direction: str) -> PdfInspection:
    inspection = inspect_pdf(path)
    profile = get_profile(direction)
    errors = list(inspection.errors)
    warnings = list(inspection.warnings)
    files = sorted(order.get("files") or [], key=lambda f: 0 if (f.get("parsed") or {}).get("side") == "face" else 1)
    page_files = [file for file in files for _ in range(int(file.get("page_count") or 1))]
    if page_files and inspection.page_count != len(page_files):
        errors.append(f"PDF содержит {inspection.page_count} страниц; ожидается {len(page_files)}")
    for index, page in enumerate(inspection.pages):
        parsed = (page_files[index].get("parsed") or {}) if index < len(page_files) else {}
        if parsed.get("width_mm") and parsed.get("height_mm"):
            expected = (float(parsed["width_mm"]) + profile.size_extra_mm, float(parsed["height_mm"]) + profile.size_extra_mm)
            actual = (page.width_mm, page.height_mm)
            if not any(all(abs(a - b) <= profile.size_tolerance_mm for a, b in zip(actual, target)) for target in (expected, expected[::-1])):
                errors.append(f"PDF страница {page.page_number} имеет размер {actual[0]:.1f}x{actual[1]:.1f} мм; ожидается {expected[0]:.1f}x{expected[1]:.1f} мм")
        for image in page.images:
            dpi = [value for value in (image.effective_dpi_x, image.effective_dpi_y) if value is not None]
            if dpi and min(dpi) + 1e-6 < profile.min_dpi:
                errors.append(f"PDF страница {page.page_number}: effective DPI {min(dpi):.1f}; требуется не менее {profile.min_dpi:.0f}")
            if "RGB" in (image.colorspace_name or "") and "ICCBased" not in (image.colorspace_name or ""):
                warnings.append(f"PDF страница {page.page_number}: растровое изображение имеет RGB colorspace")
            elif not image.colorspace_name:
                errors.append(f"PDF страница {page.page_number}: неизвестная цветовая модель")
    return replace(inspection, errors=tuple(errors), warnings=tuple(warnings))


def readable_pdf(inspection: PdfInspection) -> bool:
    return not inspection.encrypted and inspection.page_count > 0 and len(inspection.pages) == inspection.page_count
