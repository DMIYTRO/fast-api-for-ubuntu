"""Build the reviewed layered-TIFF PDF used by an explicit print action."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from typing import Any

import pymupdf


class ReviewedLayeredPdfError(ValueError):
    """A reviewed PDF set cannot safely become the order's print PDF."""


def _review_pdf_path(source_path: Path) -> Path:
    return source_path.parent / "PDF" / f"{source_path.stem}_layered-composite.pdf"


def _page_index(file: dict[str, Any], ordered_files: list[dict[str, Any]]) -> int:
    side = str(file.get("side") or (file.get("parsed") or {}).get("side") or "").lower()
    if len(ordered_files) == 2 and side in {"face", "back"}:
        return 0 if side == "face" else 1
    return ordered_files.index(file)


def build_reviewed_layered_print_pdf(order: dict[str, Any], input_root: Path) -> Path | None:
    """Create a separate print PDF with reviewed per-TIFF pages substituted.

    Returns ``None`` when the operator has not generated any layered-TIFF
    exports, preserving the existing production-print path in that case.
    """
    files = list(order.get("files") or [])
    layered_files = [
        file for file in files
        if file.get("has_unflattened_layers")
        and str(file.get("path") or "").lower().endswith((".tif", ".tiff"))
    ]
    if not layered_files:
        return None

    input_root = input_root.resolve()
    reviewed: dict[int, Path] = {}
    ordered_files = sorted(
        files,
        key=lambda file: 0 if str(file.get("side") or (file.get("parsed") or {}).get("side") or "").lower() == "face" else 1,
    )
    for file in layered_files:
        source_value = file.get("path")
        if not source_value:
            raise ReviewedLayeredPdfError("Не найден исходный TIFF для проверенного PDF.")
        source = Path(str(source_value)).resolve()
        if not source.is_relative_to(input_root):
            raise ReviewedLayeredPdfError("TIFF находится за пределами папки выбранного заказа.")
        artifact = _review_pdf_path(source).resolve()
        if not artifact.is_relative_to(input_root):
            raise ReviewedLayeredPdfError("Просмотренный PDF находится за пределами папки заказа.")
        if not artifact.is_file():
            raise ReviewedLayeredPdfError(
                f"Сначала создайте и проверьте PDF для {source.name}."
            )
        page_index = _page_index(file, ordered_files)
        if page_index in reviewed:
            raise ReviewedLayeredPdfError("Для одной стороны заказа найдено несколько layered TIFF.")
        reviewed[page_index] = artifact

    production_pdf = Path(str(order.get("pdf_path") or "")).resolve()
    if production_pdf.is_file() and not production_pdf.is_relative_to(input_root):
        raise ReviewedLayeredPdfError(
            "Производственный PDF находится за пределами папки выбранного заказа."
        )
    if production_pdf.is_file():
        base = pymupdf.open(production_pdf)
        page_count = base.page_count
        if any(index >= page_count for index in reviewed):
            base.close()
            raise ReviewedLayeredPdfError(
                "Число страниц производственного PDF не совпадает со сторонами layered TIFF."
            )
    elif len(layered_files) == len(files):
        base = None
        page_count = len(files)
    else:
        raise ReviewedLayeredPdfError(
            "Нет производственного PDF для объединения с просмотренными PDF сторон заказа."
        )

    output_dir = input_root / "PDF"
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.resolve().is_relative_to(input_root):
        raise ReviewedLayeredPdfError(
            "Папка сохранения итогового PDF находится за пределами заказа."
        )
    production_name = production_pdf.stem if production_pdf.is_file() else str(order.get("order_id") or "order")
    output = output_dir / f"{production_name}_reviewed-layered.pdf"
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp.pdf")
    assembled = pymupdf.open()
    opened_sources: list[pymupdf.Document] = []
    try:
        for index in range(page_count):
            source_path = reviewed.get(index, production_pdf)
            document = pymupdf.open(source_path)
            opened_sources.append(document)
            source_page_index = 0 if index in reviewed else index
            if source_page_index >= document.page_count:
                raise ReviewedLayeredPdfError(
                    f"В просмотренном PDF для страницы {index + 1} нет страницы."
                )
            if base is not None:
                expected = base.load_page(index).rect
                actual = document.load_page(source_page_index).rect
                if abs(expected.width - actual.width) > 0.5 or abs(expected.height - actual.height) > 0.5:
                    raise ReviewedLayeredPdfError(
                        f"Размер страницы {index + 1} просмотренного PDF отличается от производственного."
                    )
            assembled.insert_pdf(
                document,
                from_page=source_page_index,
                to_page=source_page_index,
            )
        if assembled.page_count != page_count:
            raise ReviewedLayeredPdfError("Не удалось собрать все страницы итогового PDF.")
        assembled.save(temporary, garbage=3, deflate=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        assembled.close()
        for document in opened_sources:
            document.close()
        if base is not None:
            base.close()
    try:
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output
