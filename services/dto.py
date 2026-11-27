"""JSON-safe DTO conversion for processing models.

The service layer deliberately returns plain dictionaries.  They can be passed
directly to FastAPI/Pydantic without leaking ``Path`` objects or mutable
``OrderCheck`` instances into the HTTP layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from processing.models import FileCheck, OrderCheck, ParsedFilename

from processing.side_rotation import side_rotation_message

from .issue_groups import attach_issue_groups
from .postpress import with_postpress_operations


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


def _size_fix_to_dto(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    return {
        # Дополнительные ключи сохраняются (пути — строками), основные приводятся к контракту.
        **{key: str(item) if isinstance(item, Path) else item for key, item in value.items()},
        "method": str(value.get("method") or ""),
        "auto": value.get("auto") is True,
        "from_mm": [float(item) for item in value.get("from_mm") or ()],
        "to_mm": [float(item) for item in value.get("to_mm") or ()],
        "detail": str(value.get("detail") or ""),
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
        "tiff_layer_count": value.tiff_layer_count,
        "tiff_flattened": value.tiff_flattened,
        "tiff_visible_layer_count": value.tiff_visible_layer_count,
        "tiff_hidden_layer_count": value.tiff_hidden_layer_count,
        "tiff_flatten_strategy": value.tiff_flatten_strategy,
        "transparency_flattened": value.transparency_flattened,
        "page_count": value.page_count,
        "pdf_colorspaces": list(value.pdf_colorspaces),
        "pdf_min_dpi": value.pdf_min_dpi,
        "pdf_content_type": value.pdf_content_type,
        "size_mb": value.size_mb,
        "errors": list(value.errors),
        "warnings": list(value.warnings),
        "processing_actions": [dict(action) for action in value.processing_actions],
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
        # Пометка коррекции размера вместо строки предупреждения (контракт m001).
        "size_fix": _size_fix_to_dto(value.size_fix),
        "expected_mm": list(value.expected_mm) if value.expected_mm else None,
        "downsampled_from_dpi": value.downsampled_from_dpi,
        "rotation_degrees": value.rotation_degrees,
        # Поворот стороны для совмещения лица и оборота (±90°, 0 — нет).
        "side_rotation_degrees": value.side_rotation_degrees,
        "pdf_page_rotations": {
            str(page): degrees for page, degrees in sorted(value.pdf_page_rotations.items())
        },
        "orientation_verified": value.orientation_verified,
        "passed": value.passed,
        "source_folder": value.source_folder,
    }
    if value.callas_source_fix is not None:
        # Файл собирается из принятого исправления Callas (``pdf_path``), а не из исходника.
        # ``bleed_only`` / ``bleed_only_reasons`` / ``baseline_pdf`` — контракт PitStop и
        # интерфейса (``services/callas_source_fix.mark_applied``); есть всегда.
        fix = dict(value.callas_source_fix)
        dto["callas_source_fix"] = {
            **fix,
            "bleed_only": fix.get("bleed_only") is True,
            "bleed_only_reasons": [str(reason) for reason in fix.get("bleed_only_reasons") or ()],
            "baseline_pdf": str(fix["baseline_pdf"]) if fix.get("baseline_pdf") else None,
            "pdf_path": str(value.content_path) if value.content_path else None,
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
    ink_coverage: dict[str, object] | None = None,
) -> dict[str, Any]:
    pending = sum(
        item.resample_decision == "ask_confirmation" for item in value.files
    )
    if status is None:
        if pending:
            status = "waiting_confirmation"
        elif not value.passed or value.side_alignment:
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
        result["postpress"] = with_postpress_operations(value.postpress)
    if value.customer_comment:
        result["customer_comment"] = value.customer_comment
    if value.customer_preview:
        # ``url`` добавляет координатор (нужен run_id): public_customer_preview.
        result["customer_preview"] = dict(value.customer_preview)
    if value.kit is not None:
        result["kit"] = value.kit
    if value.sborka_cancelled is not None:
        result["sborka_cancelled"] = dict(value.sborka_cancelled)
    if value.color_conversion is not None:
        result["color_conversion"] = value.color_conversion
    if value.prepress is not None:
        result["prepress"] = value.prepress
    if value.check_policy is not None:
        result["check_policy"] = dict(value.check_policy)
    if value.side_alignment:
        # Заказ собран с повёрнутой стороной: ошибка заказа для оператора (не в
        # ``OrderCheck.errors``, иначе заказ не собирался бы).
        result["side_alignment"] = dict(value.side_alignment)
        result["errors"].append(side_rotation_message(value.side_alignment))
    if current_pdf_revision is not None:
        result["current_pdf_revision"] = current_pdf_revision
    if current_pdf_sha256 is not None:
        result["current_pdf_sha256"] = current_pdf_sha256
    if pitstop is not None:
        result["pitstop"] = pitstop
    if ink_coverage is not None:
        result["ink_coverage"] = ink_coverage
    return attach_issue_groups(result)
