"""JSON-safe DTO conversion for processing models.

The service layer deliberately returns plain dictionaries.  They can be passed
directly to FastAPI/Pydantic without leaking ``Path`` objects or mutable
``OrderCheck`` instances into the HTTP layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from processing.models import FileCheck, OrderCheck, ParsedFilename


def parsed_filename_to_dto(value: ParsedFilename | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "customer_id": value.customer_id,
        "order_id": value.order_id,
        "width_mm": value.width_mm,
        "height_mm": value.height_mm,
        "front_colors": value.front_colors,
        "back_colors": value.back_colors,
        "side": value.side,
    }


def file_check_to_dto(
    value: FileCheck, *, preview_paths: list[str] | None = None
) -> dict[str, Any]:
    dto = {
        "path": str(value.path),
        "name": value.path.name,
        "parsed": parsed_filename_to_dto(value.parsed),
        "actual_width_mm": value.actual_width_mm,
        "actual_height_mm": value.actual_height_mm,
        "dpi": value.dpi,
        "dpi_x": value.dpi_x,
        "dpi_y": value.dpi_y,
        "width_px": value.width_px,
        "height_px": value.height_px,
        "actual_format": value.actual_format,
        "colorspace": value.colorspace,
        "has_alpha": value.has_alpha,
        "has_unflattened_layers": value.has_unflattened_layers,
        "channels": value.channels,
        "tiff_page_count": value.tiff_page_count,
        "page_count": value.page_count,
        "pdf_colorspaces": list(value.pdf_colorspaces),
        "pdf_min_dpi": value.pdf_min_dpi,
        "pdf_content_type": value.pdf_content_type,
        "size_mb": value.size_mb,
        "errors": list(value.errors),
        "warnings": list(value.warnings),
        "needs_resample": value.needs_resample,
        "resample_target_mm": (
            list(value.resample_target_mm) if value.resample_target_mm else None
        ),
        "resample_decision": value.resample_decision,
        "resample_reason": value.resample_reason,
        "resample_scale": value.resample_scale,
        "resample_crop_mm": list(value.resample_crop_mm),
        "resample_effective_dpi": (
            list(value.resample_effective_dpi)
            if value.resample_effective_dpi
            else None
        ),
        "resample_confirmed": value.resample_confirmed,
        "rotation_degrees": value.rotation_degrees,
        "orientation_verified": value.orientation_verified,
        "passed": value.passed,
    }
    matching_paths = [
        path
        for path in preview_paths or []
        if Path(path).name.startswith(f"{value.path.stem}_")
    ]
    if matching_paths:
        # The first path is retained for existing single-preview API clients;
        # the full list also preserves previews of all PDF pages.
        dto["preview_path"] = matching_paths[0]
        dto["preview_paths"] = matching_paths
    return dto


def order_check_to_dto(
    value: OrderCheck,
    *,
    status: str | None = None,
    pdf_path: str | None = None,
    preview_paths: list[str] | None = None,
    processing_errors: list[str] | None = None,
    source_status: str | None = None,
    pitstop_status: str | None = None,
    workflow_status: str = "active",
    pitstop: dict[str, object] | None = None,
    current_pdf_revision: int | None = None,
    current_pdf_sha256: str | None = None,
) -> dict[str, Any]:
    pending = sum(
        item.resample_decision == "ask_confirmation" for item in value.files
    )
    if status is None:
        if pending:
            status = "waiting_confirmation"
        elif not value.passed:
            status = "error"
        elif value.warnings or any(item.warnings for item in value.files):
            status = "warning"
        else:
            status = "passed"
    source_status = source_status or status
    effective_passed = status in {"passed", "warning"}
    result = {
        "aggregate_id": value.aggregate_id,
        "order_id": value.order_id,
        "customer_id": value.customer_id,
        "status": status,
        "passed": effective_passed,
        "source_status": source_status,
        "pitstop_status": pitstop_status or "not_checked",
        "workflow_status": workflow_status,
        "pending_confirmations": pending,
        "errors": list(value.errors),
        "warnings": list(value.warnings),
        "files": [
            file_check_to_dto(item, preview_paths=preview_paths)
            for item in value.files
        ],
        "pdf_path": pdf_path,
        "preview_paths": list(preview_paths or []),
        "processing_errors": list(processing_errors or []),
    }
    if value.postpress is not None:
        result["postpress"] = value.postpress
    if current_pdf_revision is not None:
        result["current_pdf_revision"] = current_pdf_revision
    if current_pdf_sha256 is not None:
        result["current_pdf_sha256"] = current_pdf_sha256
    if pitstop is not None:
        result["pitstop"] = pitstop
    return result
