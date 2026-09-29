"""Build the reviewed layered-TIFF PDF used by an explicit print action."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
from uuid import uuid4
from typing import Any

import pymupdf

from core.tool_runner import run_command


class ReviewedLayeredPdfError(ValueError):
    """A reviewed PDF set cannot safely become the order's print PDF."""

    def __init__(self, message: str, *, generated_file_ids: list[str] | None = None):
        super().__init__(message)
        self.generated_file_ids = generated_file_ids or []


def _create_layered_export(source: Path, artifact: Path, preview: Path) -> None:
    """Create an inspectable draft from a TIFF's saved composite, without editing it."""
    magick = shutil.which("magick")
    if not magick:
        raise ReviewedLayeredPdfError("ImageMagick (magick) не найден на сервере.")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    preview.parent.mkdir(parents=True, exist_ok=True)
    temporary_pdf = artifact.with_name(f".{artifact.stem}.{uuid4().hex}.tmp.pdf")
    temporary_png = preview.with_name(f".{preview.stem}.{uuid4().hex}.tmp.png")
    try:
        run_command(
            [
                magick,
                "-define", "tiff:ignore-layers=true",
                str(source),
                "-compress", "none",
                str(temporary_pdf),
            ],
            check=True,
        )
        with pymupdf.open(temporary_pdf) as document:
            if document.page_count != 1:
                raise ReviewedLayeredPdfError(
                    f"Экспорт {source.name} должен содержать одну страницу."
                )
            page = document.load_page(0)
            scale = min(1800 / max(page.rect.width, page.rect.height, 1), 4)
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(temporary_png)
        temporary_pdf.replace(artifact)
        temporary_png.replace(preview)
    except Exception:
        artifact.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
        raise
    finally:
        temporary_pdf.unlink(missing_ok=True)
        temporary_png.unlink(missing_ok=True)


def _review_pdf_path(source_path: Path) -> Path:
    return source_path.parent / "PDF" / f"{source_path.stem}_layered-composite.pdf"


def _page_index(file: dict[str, Any], ordered_files: list[dict[str, Any]]) -> int:
    side = str(file.get("side") or (file.get("parsed") or {}).get("side") or "").lower()
    if len(ordered_files) == 2 and side in {"face", "back"}:
        return 0 if side == "face" else 1
    return ordered_files.index(file)


def build_reviewed_layered_print_pdf(order: dict[str, Any], input_root: Path) -> Path | None:
    """Prepare one print part per order file and assemble the checked pages.

    Ordinary file pages are taken from the production PDF. Layered TIFF pages
    use their saved-composite exports. If an export is missing, it is generated
    as a draft and this call stops so an operator can review it before printing.
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
    ordered_files = sorted(
        files,
        key=lambda file: 0 if str(file.get("side") or (file.get("parsed") or {}).get("side") or "").lower() == "face" else 1,
    )
    for file in ordered_files:
        source_value = file.get("path")
        if not source_value or not Path(str(source_value)).resolve().is_relative_to(input_root):
            raise ReviewedLayeredPdfError(
                "Один из исходных файлов находится за пределами папки выбранного заказа."
            )
    paths_by_page: dict[int, Path] = {}
    generated_file_ids: list[str] = []
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
            preview_path = source.parent / "Previews" / f"{source.stem}_layered-composite.png"
            if not preview_path.resolve().is_relative_to(input_root):
                raise ReviewedLayeredPdfError(
                    "Папка превью находится за пределами выбранного заказа."
                )
            _create_layered_export(
                source,
                artifact,
                preview_path,
            )
            generated_file_ids.append(
                str(file.get("id") or file.get("file_result_id") or source.name)
            )
        page_index = _page_index(file, ordered_files)
        if page_index in paths_by_page:
            raise ReviewedLayeredPdfError("Для одной стороны заказа найдено несколько layered TIFF.")
        paths_by_page[page_index] = artifact

    production_pdf = Path(str(order.get("pdf_path") or "")).resolve()
    if production_pdf.is_file() and not production_pdf.is_relative_to(input_root):
        raise ReviewedLayeredPdfError(
            "Производственный PDF находится за пределами папки выбранного заказа."
        )
    if production_pdf.is_file():
        base = pymupdf.open(production_pdf)
        page_count = base.page_count
        if any(index >= page_count for index in paths_by_page):
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
    parts_dir = output_dir / "Print" / "Parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    if not parts_dir.resolve().is_relative_to(input_root):
        raise ReviewedLayeredPdfError(
            "Папка печатных версий находится за пределами заказа."
        )

    # Save a page PDF for every source file in the order. Ordinary sources use
    # the already validated production PDF; layered sources use their TIFF export.
    part_paths: dict[int, Path] = {}
    for page_index, file in enumerate(ordered_files):
        source_name = Path(str(file.get("path") or file.get("filename") or f"page-{page_index + 1}")).stem
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source_name).strip("._-") or f"page-{page_index + 1}"
        side = str(file.get("side") or (file.get("parsed") or {}).get("side") or f"page-{page_index + 1}").lower()
        part = parts_dir / f"{page_index + 1:02d}_{side}_{safe_stem}.pdf"
        source_path = paths_by_page.get(page_index, production_pdf)
        source_page_index = 0 if page_index in paths_by_page else page_index
        if page_index not in paths_by_page and base is None:
            raise ReviewedLayeredPdfError(
                "Для остальных файлов заказа нужен производственный PDF."
            )
        document = pymupdf.open(source_path)
        piece = pymupdf.open()
        try:
            if source_page_index >= document.page_count:
                raise ReviewedLayeredPdfError(
                    f"В исходном PDF для страницы {page_index + 1} нет страницы."
                )
            if base is not None:
                expected = base.load_page(page_index).rect
                actual = document.load_page(source_page_index).rect
                if (
                    abs(expected.width - actual.width) > 0.5
                    or abs(expected.height - actual.height) > 0.5
                ):
                    raise ReviewedLayeredPdfError(
                        f"Размер страницы {page_index + 1} просмотренного PDF отличается от производственного."
                    )
            piece.insert_pdf(document, from_page=source_page_index, to_page=source_page_index)
            temporary_part = part.with_name(f".{part.stem}.{uuid4().hex}.tmp.pdf")
            try:
                piece.save(temporary_part, garbage=3, deflate=True)
                temporary_part.replace(part)
            finally:
                temporary_part.unlink(missing_ok=True)
        finally:
            piece.close()
            document.close()
        part_paths[page_index] = part

    if generated_file_ids:
        if base is not None:
            base.close()
        raise ReviewedLayeredPdfError(
            "Созданы PDF для недостающих сторон. Проверьте их в карточке заказа и повторите отправку в печать.",
            generated_file_ids=generated_file_ids,
        )

    production_name = production_pdf.stem if production_pdf.is_file() else str(order.get("order_id") or "order")
    output = output_dir / f"{production_name}_reviewed-layered.pdf"
    temporary = output.with_name(f".{output.stem}.{uuid4().hex}.tmp.pdf")
    assembled = pymupdf.open()
    opened_sources: list[pymupdf.Document] = []
    try:
        for index in range(page_count):
            source_path = part_paths.get(index)
            if source_path is None:
                raise ReviewedLayeredPdfError(
                    f"Не создана печатная версия файла для страницы {index + 1}."
                )
            document = pymupdf.open(source_path)
            opened_sources.append(document)
            source_page_index = 0
            if source_page_index >= document.page_count:
                raise ReviewedLayeredPdfError(
                    f"В просмотренном PDF для страницы {index + 1} нет страницы."
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
