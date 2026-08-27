from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile
from typing import Mapping, Optional

from core.inspector import count_frames, inspect_file, inspect_tiff_structure
from core.pdf_inspector import inspect_pdf
from core.pdf_exporter import (
    convert_image_to_pdf,
    convert_tiff_to_pdf_preserve_cmyk,
    merge_pdfs_with_pymupdf,
)
from core.callas_toolbox import CallasToolbox
from core.callas_toolbox import CallasToolboxError
from core.preview_generator import generate_preview
from core.resampler import resample_image
from core.tool_runner import run_command
from config.profiles import DEFAULT_PROFILE, PrePressProfile
from .resample_policy import ResampleDecision, analyze_resample
from .profile_rules import evaluate_metadata_rules

from .filename_parser import parse_filename
from .models import FileCheck, OrderCheck


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}
REJECTED_EXTENSIONS = {".psd", ".bmp", ".heic", ".heif"}
# PDF point / pixel arithmetic can undershoot an exact boundary by several
# millionths (e.g. 299.9999957 for a nominal 300 DPI image).
DPI_EPSILON = 0.01


@dataclass(frozen=True)
class _LogicalPageRef:
    """A page in the final PDF and the source page it must represent."""

    source: FileCheck
    source_page_index: int
    expected_side: str
    expected_width_mm: float
    expected_height_mm: float


def _logical_side(item: FileCheck) -> str:
    """Return the production side, including a side-less 4-0 PDF."""
    return item.parsed.side or "face"


class BatchProcessor:
    def __init__(
        self,
        input_dir: Path,
        output_dir: Path,
        size_extra_mm: float | None = None,
        tolerance_mm: float | None = None,
        min_dpi: float | None = None,
        *,
        profile: PrePressProfile = DEFAULT_PROFILE,
        callas_toolbox: CallasToolbox | None = None,
        callas_enabled: bool = False,
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.profile = profile
        # Необязательные аргументы оставлены для совместимости и точечных тестов.
        self.size_extra_mm = profile.size_extra_mm if size_extra_mm is None else size_extra_mm
        self.tolerance_mm = profile.size_tolerance_mm if tolerance_mm is None else tolerance_mm
        self.min_dpi = profile.min_dpi if min_dpi is None else min_dpi
        # Keep the legacy backend as the default for unit tests and callers
        # that do not explicitly opt into the external CLI. Production wiring
        # can inject a configured CallasToolbox and enable it from Settings.
        self.callas_toolbox = callas_toolbox
        self.callas_enabled = callas_enabled and callas_toolbox is not None
        self.unparsed: list[FileCheck] = []
        self.unsupported: list[FileCheck] = []
        self.scanned_order_count = 0

    @staticmethod
    def _preview_worker_count(item_count: int) -> int:
        """Return a bounded, operator-configurable preview concurrency level."""
        configured = os.environ.get("IMAGE_MAGIC_PREVIEW_WORKERS", "2")
        try:
            requested = int(configured)
        except ValueError:
            requested = 2
        return max(1, min(item_count, requested, os.cpu_count() or 1))

    def scan(self) -> list[Path]:
        if not self.input_dir.is_dir():
            raise FileNotFoundError(f"Входная папка не найдена: {self.input_dir}")
        return sorted(
            path for path in self.input_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS | REJECTED_EXTENSIONS
        )

    def inspect_orders(self) -> list[OrderCheck]:
        """Inspect all orders, preserving the legacy list-returning API."""
        return list(self.iter_inspect_orders())

    def iter_inspect_orders(self):
        """Yield validated orders as soon as each order has been inspected.

        Folder discovery and filename parsing are intentionally completed
        first: this preserves deterministic grouping, sorting, and the legacy
        ``unparsed``/``unsupported`` collections.  Expensive image inspection
        then happens one order at a time, so callers can publish partial
        results without waiting for the entire folder.
        """
        grouped: dict[tuple[str, str], list[FileCheck]] = defaultdict(list)
        self.unparsed = []
        self.unsupported = []
        self.scanned_order_count = 0

        for path in self.scan():
            check = FileCheck(path=path)
            if path.suffix.lower() in REJECTED_EXTENSIONS:
                check.errors.append(
                    f"формат {path.suffix.lower().lstrip('.').upper()} исключён из обработки"
                )
                self.unsupported.append(check)
                continue
            try:
                check.parsed = parse_filename(path)
            except ValueError as exc:
                check.errors.append(str(exc))
                self.unparsed.append(check)
                continue
            grouped[(check.parsed.customer_id, check.parsed.order_id)].append(check)

        sorted_groups = sorted(grouped.items())
        self.scanned_order_count = len(sorted_groups)
        for (_customer_id, order_id), files in sorted_groups:
            for check in files:
                try:
                    path = check.path
                    if path.suffix.lower() == ".pdf":
                        self._inspect_pdf_file(check)
                        continue
                    if path.suffix.lower() in {".tif", ".tiff"}:
                        structure = inspect_tiff_structure(str(path))
                        check.tiff_page_count = structure.page_count
                        check.has_unflattened_layers = structure.has_unflattened_layers
                        check.has_alpha = structure.has_alpha
                        check.channels = structure.channels
                        if structure.has_alpha:
                            check.errors.append(
                                "TIFF содержит альфа-канал; перед отправкой удалите прозрачность"
                            )
                        if structure.has_unflattened_layers:
                            check.errors.append(
                                "TIFF содержит несведённые слои; перед отправкой сведите изображение"
                            )
                        if structure.page_count > 1:
                            check.errors.append(
                                f"TIFF содержит {structure.page_count} страниц; "
                                "разрешён только одностраничный файл"
                            )
                    else:
                        frame_count = count_frames(str(path))
                        if frame_count != 1:
                            check.errors.append(
                                f"файл содержит {frame_count} страниц/изображений; разрешён только одностраничный файл"
                            )
                            continue
                    meta = inspect_file(str(path))
                    check.actual_width_mm = meta.width_mm
                    check.actual_height_mm = meta.height_mm
                    check.width_px = meta.width_px
                    check.height_px = meta.height_px
                    check.dpi = meta.dpi
                    check.dpi_x = meta.dpi_x
                    check.dpi_y = meta.dpi_y
                    check.actual_format = meta.format.upper()
                    check.colorspace = meta.colorspace
                    check.icc_profile = meta.icc_profile
                    check.icc_profile_present = (
                        bool(meta.icc_profile) and meta.icc_profile != "Не внедрен"
                    )
                    check.size_mb = meta.size_mb
                    self._validate_file(check)
                except Exception as exc:
                    check.errors.append(f"не удалось прочитать файл: {exc}")

            order = OrderCheck(
                order_id=order_id,
                customer_id=files[0].parsed.customer_id,
                files=files,
            )
            self._validate_order(order)
            yield order

    def _inspect_pdf_file(self, check: FileCheck) -> None:
        """Read and validate a PDF without routing it through ImageMagick."""
        inspection = inspect_pdf(check.path)
        check.actual_format = "PDF"
        check.page_count = inspection.page_count
        check.pdf_pages = inspection.pages
        check.pdf_content_type = (
            "mixed" if any(page.content_type == "mixed" for page in inspection.pages)
            else "raster" if any(page.content_type == "raster" for page in inspection.pages)
            else "vector" if inspection.pages else None
        )
        image_infos = [image for page in inspection.pages for image in page.images]
        check.pdf_colorspaces = tuple(sorted({
            image.colorspace_name or "unknown" for image in image_infos
        }))
        dpi_x_values = [
            image.effective_dpi_x
            for image in image_infos
            if image.effective_dpi_x is not None
        ]
        dpi_y_values = [
            image.effective_dpi_y
            for image in image_infos
            if image.effective_dpi_y is not None
        ]
        check.dpi_x = min(dpi_x_values) if dpi_x_values else None
        check.dpi_y = min(dpi_y_values) if dpi_y_values else None
        check.dpi = (
            min(check.dpi_x, check.dpi_y)
            if check.dpi_x is not None and check.dpi_y is not None
            else check.dpi_x or check.dpi_y
        )
        check.pdf_min_dpi = check.dpi
        check.colorspace = ", ".join(check.pdf_colorspaces) or None
        check.size_mb = check.path.stat().st_size / (1024 * 1024)
        check.errors.extend(inspection.errors)
        check.warnings.extend(inspection.warnings)

        if not inspection.pages:
            return

        first_page = inspection.pages[0]
        check.actual_width_mm = first_page.width_mm
        check.actual_height_mm = first_page.height_mm
        check.rotation_degrees = first_page.rotation

        parsed = check.parsed
        expected = (
            parsed.width_mm + self.size_extra_mm,
            parsed.height_mm + self.size_extra_mm,
        )
        for page in inspection.pages:
            actual = (page.width_mm, page.height_mm)
            if not self._dimensions_match(actual, expected) and not self._dimensions_match(
                actual, (expected[1], expected[0])
            ):
                check.errors.append(
                    f"PDF страница {page.page_number} имеет размер "
                    f"{actual[0]:.1f}x{actual[1]:.1f} мм; ожидается "
                    f"{expected[0]:.1f}x{expected[1]:.1f} мм"
                )
            if page.rotation not in {0, 90, 180, 270}:
                check.errors.append(
                    f"PDF страница {page.page_number} имеет недопустимый поворот "
                    f"{page.rotation}°"
                )

            for image in page.images:
                dpi_values = [
                    dpi for dpi in (image.effective_dpi_x, image.effective_dpi_y)
                    if dpi is not None
                ]
                # Effective DPI is calculated from PDF points and therefore
                # can be 299.99999999999994 for an exact 300 DPI placement.
                # Treat that floating-point noise as the configured boundary,
                # while still rejecting a materially lower resolution.
                if dpi_values and min(dpi_values) + DPI_EPSILON < self.min_dpi:
                    check.errors.append(
                        f"PDF страница {page.page_number}: effective DPI "
                        f"{min(dpi_values):.1f}; требуется не менее {self.min_dpi:.0f}"
                    )
                colorspace = image.colorspace_name or "unknown"
                if "RGB" in colorspace and "ICCBased" not in colorspace:
                    check.warnings.append(
                        f"PDF страница {page.page_number}: растровое изображение "
                        "имеет RGB colorspace"
                    )
                elif colorspace == "unknown":
                    check.errors.append(
                        f"PDF страница {page.page_number}: неизвестная цветовая модель"
                    )

    @staticmethod
    def _pdf_pages(item: FileCheck) -> tuple[object, ...]:
        return item.pdf_pages if item.page_count is not None else ()

    def _validate_file(self, check: FileCheck) -> None:
        parsed = check.parsed
        expected = (parsed.width_mm + self.size_extra_mm, parsed.height_mm + self.size_extra_mm)
        actual = (check.actual_width_mm, check.actual_height_mm)
        # The order of dimensions in the filename defines the format, not the
        # visual top of the artwork. Accept both orientations without rotating.
        # A possible face/back mismatch is handled later at order level.
        plans = [
            analyze_resample(
                actual,
                target,
                (check.dpi_x or 0.0, check.dpi_y or 0.0),
                min_dpi=self.min_dpi,
                metadata_tolerance_mm=self.profile.metadata_tolerance_mm,
                auto_crop_mm=self.profile.auto_crop_mm,
                confirm_crop_mm=self.profile.confirm_crop_mm,
                allow_rotation=False,
            )
            for target in (expected, (expected[1], expected[0]))
        ]
        decision_rank = {
            ResampleDecision.ACCEPT: 0,
            ResampleDecision.AUTO_CORRECT: 1,
            ResampleDecision.ASK_CONFIRMATION: 2,
            ResampleDecision.REJECT: 3,
        }
        plan = min(plans, key=lambda value: (decision_rank[value.decision], max(value.crop_mm)))
        expected = plan.target_mm
        check.resample_decision = plan.decision.value
        check.resample_reason = plan.reason
        check.resample_scale = plan.scale
        check.resample_crop_mm = plan.crop_mm
        check.resample_effective_dpi = plan.effective_dpi
        check.rotation_degrees = plan.rotation_degrees

        crop_text = f"обрезка {plan.crop_mm[0]:.2f}x{plan.crop_mm[1]:.2f} мм"
        rotation_text = f", поворот на {plan.rotation_degrees}°" if plan.rotation_degrees else ""
        if plan.decision == ResampleDecision.AUTO_CORRECT:
            check.needs_resample = True
            check.resample_target_mm = expected
            check.warnings.append(
                f"размер автоматически скорректирован: {actual[0]:.1f}x{actual[1]:.1f} → "
                f"{expected[0]:.1f}x{expected[1]:.1f} мм; пропорции сохранены, {crop_text}{rotation_text}"
            )
        elif plan.decision == ResampleDecision.ASK_CONFIRMATION:
            check.resample_target_mm = expected
            check.warnings.append(
                f"требуется подтверждение коррекции {actual[0]:.1f}x{actual[1]:.1f} → "
                f"{expected[0]:.1f}x{expected[1]:.1f} мм; {crop_text}{rotation_text}"
            )
        elif plan.decision == ResampleDecision.REJECT:
            check.errors.append(
                f"размер {actual[0]:.1f}x{actual[1]:.1f} мм; ожидается "
                f"{expected[0]:.1f}x{expected[1]:.1f} мм: {plan.reason}"
            )

        if plan.rotation_degrees and plan.decision != ResampleDecision.REJECT:
            check.warnings.append(
                f"{parsed.side} будет автоматически повёрнут на {plan.rotation_degrees}°; "
                "проверьте ориентацию и совмещение лица и оборота"
            )

        correction_has_enough_dpi = (
            plan.decision in {ResampleDecision.AUTO_CORRECT, ResampleDecision.ASK_CONFIRMATION}
            and min(plan.effective_dpi) >= self.min_dpi
        )
        metadata_rules = evaluate_metadata_rules(
            profile=self.profile,
            size_mb=check.size_mb,
            colorspace=check.colorspace,
            dpi_x=check.dpi_x,
            dpi_y=check.dpi_y,
            correction_has_enough_dpi=correction_has_enough_dpi,
        )
        check.errors.extend(metadata_rules.errors)
        check.warnings.extend(metadata_rules.warnings)

        color_mode = (parsed.front_colors, parsed.back_colors)
        if color_mode not in self.profile.allowed_color_modes:
            allowed = ", ".join(
                f"{front}-{back}" for front, back in sorted(self.profile.allowed_color_modes)
            )
            check.errors.append(
                f"цветность {color_mode[0]}-{color_mode[1]} не поддерживается; разрешены: {allowed}"
            )

    def _dimensions_match(self, actual: tuple[float, float], expected: tuple[float, float]) -> bool:
        return all(abs(a - e) <= self.tolerance_mm for a, e in zip(actual, expected))

    @staticmethod
    def confirm_resample(check: FileCheck, approved: bool) -> None:
        if check.resample_decision != ResampleDecision.ASK_CONFIRMATION.value:
            return
        check.resample_confirmed = approved
        if approved:
            check.resample_decision = ResampleDecision.AUTO_CORRECT.value
            check.needs_resample = True
            check.warnings.append("коррекция размера подтверждена пользователем")
        else:
            check.resample_decision = ResampleDecision.REJECT.value
            check.errors.append("пользователь отказался от предложенной коррекции размера")

    @staticmethod
    def _validate_order(order: OrderCheck) -> None:
        sides: dict[str, list[FileCheck]] = defaultdict(list)
        for item in order.files:
            # A two-page PDF is a complete duplex order.  Its page indices
            # define face/back; filename side suffixes are not needed.
            if item.page_count == 2:
                sides["complete_pdf"].append(item)
            else:
                sides[item.parsed.side].append(item)

        duplex_files = [item for item in order.files if item.page_count == 2]
        if len(duplex_files) > 1:
            order.errors.append("найдено несколько двухстраничных PDF в одном заказе")
        if duplex_files and len(order.files) > 1:
            order.errors.append(
                "двухстраничный PDF является полным заказом; дополнительные стороны запрещены"
            )

        color_modes = {(item.parsed.front_colors, item.parsed.back_colors) for item in order.files}
        if len(color_modes) != 1:
            order.errors.append("у файлов заказа не совпадает цветность в имени")
            return

        _, back_colors = next(iter(color_modes))
        if duplex_files:
            expected_pages = 2 if back_colors > 0 else 1
            if back_colors == 0:
                order.errors.append("двухстраничный PDF недопустим для односторонней печати")
            if duplex_files[0].page_count != expected_pages:
                order.errors.append(
                    f"для цветности с оборотом требуется {expected_pages} логические страницы"
                )
            # A complete PDF does not need a filename side and must not be
            # forced through the legacy face/back checks below.
            if len(order.files) == 1:
                return

        if back_colors == 0 and len(order.files) > 1:
            order.errors.append(
                "для односторонней печати разрешён только один входной файл"
            )

        if back_colors > 0 and not duplex_files and not sides["face"]:
            order.errors.append("не найден обязательный файл face")
        if len(sides["face"]) > 1:
            order.errors.append("найдено несколько файлов face")
        if len(sides["back"]) > 1:
            order.errors.append("найдено несколько файлов back")

        readable_files = [
            item for item in order.files
            if item.actual_width_mm is not None and item.actual_height_mm is not None
        ]
        orientations = {
            BatchProcessor._orientation(
                item.actual_height_mm if item.rotation_degrees in {90, 270} else item.actual_width_mm,
                item.actual_width_mm if item.rotation_degrees in {90, 270} else item.actual_height_mm,
            ) for item in readable_files
        }
        if len(orientations) > 1:
            details = ", ".join(
                f"{item.parsed.side}={BatchProcessor._orientation(item.actual_width_mm, item.actual_height_mm)}"
                for item in readable_files
            )
            order.errors.append(f"ориентация сторон не совпадает: {details}")

        declared_sizes = {(item.parsed.width_mm, item.parsed.height_mm) for item in order.files}
        if len(declared_sizes) != 1:
            order.errors.append("у файлов заказа не совпадает размер в имени")

        customer_ids = {item.parsed.customer_id for item in order.files}
        if len(customer_ids) != 1:
            order.errors.append("у файлов заказа не совпадает номер клиента")

        actual_formats = {item.actual_format for item in order.files if item.actual_format}
        if len(actual_formats) > 1:
            order.warnings.append(
                "стороны заказа имеют разные форматы: " + ", ".join(sorted(actual_formats))
            )

        if back_colors > 0 and not sides["back"] and not duplex_files:
            order.errors.append("для двусторонней печати не найден файл back")
        if back_colors == 0 and sides["back"]:
            order.errors.append("для односторонней печати найден лишний файл back")

    @staticmethod
    def _orientation(width_mm: float, height_mm: float) -> str:
        if abs(width_mm - height_mm) <= 0.5:
            return "квадратная"
        return "горизонтальная" if width_mm > height_mm else "вертикальная"

    def create_pdfs(self, orders: list[OrderCheck]) -> list[tuple[OrderCheck, Path, str | None]]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for order in orders:
            if not order.passed:
                continue
            ordered_files = self._ordered_files_for_creation(order)
            output_path = self.output_dir / f"{self._output_stem(order, ordered_files)}.pdf"
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f".{order.order_id}_",
                    dir=self.output_dir,
                ) as temporary_dir:
                    page_refs = self._build_page_refs(ordered_files)
                    page_pdfs = self._prepare_page_pdfs(
                        page_refs,
                        Path(temporary_dir),
                    )

                    temporary_output = Path(temporary_dir) / "combined.pdf"
                    # A complete PDF is already a production artifact.  Keep
                    # its bytes intact and only validate a copied temporary
                    # candidate; re-saving it through PyMuPDF is needlessly
                    # expensive and may alter metadata/content streams.
                    if (
                        len(ordered_files) == 1
                        and ordered_files[0].path.suffix.lower() == ".pdf"
                    ):
                        shutil.copy2(ordered_files[0].path, temporary_output)
                    else:
                        self._merge_page_pdfs(page_pdfs, temporary_output)
                    self._validate_created_pdf(
                        temporary_output,
                        page_refs,
                        Path(temporary_dir) / "validation",
                    )
                    temporary_output.replace(output_path)
                results.append((order, output_path, None))
            except Exception as exc:
                results.append((order, output_path, str(exc)))
        return results

    def _merge_page_pdfs(self, page_pdfs: list[str], output_path: Path) -> None:
        """Merge PDFs using callas only for an explicitly opted-in PDF-only batch."""
        pdf_only = all(Path(path).suffix.lower() == ".pdf" for path in page_pdfs)
        if self.callas_enabled and self.callas_toolbox is not None and pdf_only:
            self.callas_toolbox.merge_pdfs(
                [Path(path) for path in page_pdfs],
                output_path,
            )
            return
        merge_pdfs_with_pymupdf(page_pdfs, str(output_path))

    @staticmethod
    def _ordered_files_for_creation(order: OrderCheck) -> list[FileCheck]:
        """Order inputs by production side, while supporting complete PDFs."""
        if len(order.files) == 1 and order.files[0].page_count == 2:
            return list(order.files)
        return sorted(order.files, key=lambda item: 0 if _logical_side(item) == "face" else 1)

    @staticmethod
    def _output_stem(order: OrderCheck, ordered_files: list[FileCheck]) -> str:
        """Choose a stable name even when a one-page PDF has no side suffix."""
        if len(ordered_files) == 1:
            return ordered_files[0].path.stem
        face = next((item for item in ordered_files if _logical_side(item) == "face"), None)
        return (face or ordered_files[0]).path.stem

    @staticmethod
    def _build_page_refs(ordered_files: list[FileCheck]) -> list[_LogicalPageRef]:
        refs: list[_LogicalPageRef] = []
        if len(ordered_files) == 1 and ordered_files[0].page_count == 2:
            item = ordered_files[0]
            for index, page in enumerate(item.pdf_pages):
                refs.append(
                    _LogicalPageRef(
                        source=item,
                        source_page_index=index,
                        expected_side="face" if index == 0 else "back",
                        expected_width_mm=page.width_mm,
                        expected_height_mm=page.height_mm,
                    )
                )
            return refs

        for item in ordered_files:
            if item.page_count == 1 and item.pdf_pages:
                page = item.pdf_pages[0]
                width_mm, height_mm = page.width_mm, page.height_mm
                page_index = 0
            else:
                width_mm, height_mm = item.actual_width_mm, item.actual_height_mm
                page_index = 0
            if width_mm is None or height_mm is None:
                raise ValueError(f"не удалось определить размер страницы: {item.path.name}")
            refs.append(
                _LogicalPageRef(
                    source=item,
                    source_page_index=page_index,
                    expected_side=_logical_side(item),
                    expected_width_mm=width_mm,
                    expected_height_mm=height_mm,
                )
            )
        return refs

    def _prepare_page_pdfs(self, page_refs: list[_LogicalPageRef], temporary_dir: Path) -> list[str]:
        """Create page PDFs for rasters and keep incoming PDFs untouched."""
        page_pdfs: list[str] = []
        converted: dict[Path, str] = {}
        for page_number, ref in enumerate(page_refs, start=1):
            item = ref.source
            if item.path.suffix.lower() == ".pdf":
                if str(item.path) not in page_pdfs:
                    page_pdfs.append(str(item.path))
                continue

            source_image_path = str(item.path)
            dpi_arg = f"{item.dpi_x}x{item.dpi_y}"
            if item.needs_resample and item.resample_target_mm:
                resampled_path = temporary_dir / (
                    f"resampled_{page_number}_{ref.expected_side}{item.path.suffix}"
                )
                resample_image(
                    str(item.path),
                    str(resampled_path),
                    target_width_mm=item.resample_target_mm[0],
                    target_height_mm=item.resample_target_mm[1],
                    target_dpi=self.min_dpi,
                    rotation_degrees=item.rotation_degrees,
                )
                source_image_path = str(resampled_path)
                dpi_arg = str(self.min_dpi)

            page_path = temporary_dir / f"{page_number}_{ref.expected_side}.pdf"
            if item.path.suffix.lower() in {".tif", ".tiff"}:
                convert_tiff_to_pdf_preserve_cmyk(
                    source_image_path, str(page_path), dpi=dpi_arg
                )
            else:
                convert_image_to_pdf(
                    source_image_path, str(page_path), dpi=dpi_arg, compression="none"
                )
            converted[item.path] = str(page_path)
            page_pdfs.append(str(page_path))
        return page_pdfs

    def _validate_created_pdf(
        self,
        pdf_path: Path,
        page_refs: list[_LogicalPageRef],
        validation_dir: Path,
    ) -> None:
        """Render the final PDF and verify readability, page count, order and page sizes."""
        gs_cmd = shutil.which("gs")
        if not gs_cmd:
            raise FileNotFoundError("Ghostscript (`gs`) не найден для проверки PDF.")
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise ValueError("Ghostscript не создал итоговый PDF или файл пуст.")

        validation_dir.mkdir(parents=True, exist_ok=True)
        page_pattern = validation_dir / "page-%03d.png"
        command = [
            gs_cmd,
            "-q",
            "-dSAFER",
            "-dBATCH",
            "-dNOPAUSE",
            "-sDEVICE=png16m",
            "-r72",
            f"-sOutputFile={page_pattern}",
            str(pdf_path),
        ]
        result = run_command(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise ValueError(f"итоговый PDF повреждён или не открывается: {details}")

        rendered_pages = sorted(validation_dir.glob("page-*.png"))
        if len(rendered_pages) != len(page_refs):
            raise ValueError(
                f"в итоговом PDF {len(rendered_pages)} страниц; ожидалось {len(page_refs)}"
            )

        for page_number, (rendered_page, ref) in enumerate(zip(rendered_pages, page_refs), start=1):
            page_meta = inspect_file(str(rendered_page))
            source = ref.source
            expected = (ref.expected_width_mm, ref.expected_height_mm)
            if source.needs_resample and source.resample_target_mm and source.path.suffix.lower() != ".pdf":
                expected = source.resample_target_mm
            actual = (page_meta.width_mm, page_meta.height_mm)
            # A 72-DPI control render has a rounding step of about 0.35 mm.
            validation_tolerance = max(self.tolerance_mm, 0.6)
            if any(abs(a - e) > validation_tolerance for a, e in zip(actual, expected)):
                raise ValueError(
                    f"страница {page_number} ({ref.expected_side}) имеет размер "
                    f"{actual[0]:.1f}x{actual[1]:.1f} мм; ожидалось "
                    f"{expected[0]:.1f}x{expected[1]:.1f} мм"
                )

            expected_side = "face" if page_number == 1 else "back"
            if ref.expected_side != expected_side:
                raise ValueError(
                    f"нарушен порядок страниц: страница {page_number} должна быть {expected_side}"
                )

    def copy_failed_to_troubles(
        self,
        orders: list[OrderCheck],
        troubles_dir: Path,
    ) -> list[tuple[Path, Path, str | None]]:
        """Copy rejected inputs and a readable reason file without changing originals."""
        rejected: list[tuple[FileCheck, list[str], str]] = []
        for order in orders:
            if order.passed:
                continue
            order_reasons = [f"Ошибка заказа: {message}" for message in order.errors]
            for item in order.files:
                rejected.append((item, item.errors + order_reasons, order.order_id))

        for item in self.unparsed:
            rejected.append((item, item.errors, "UNPARSED"))

        for item in self.unsupported:
            rejected.append((item, item.errors, "UNSUPPORTED"))

        results = []
        for item, reasons, group_name in rejected:
            target_dir = troubles_dir / group_name
            target_path = target_dir / item.path.name
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.path, target_path)
                reason_path = target_dir / f"{item.path.name}.error.txt"
                reason_text = "\n".join(f"- {reason}" for reason in reasons)
                reason_path.write_text(
                    f"Файл: {item.path.name}\nСтатус: НЕ ОБРАБОТАН\nПричины:\n{reason_text}\n",
                    encoding="utf-8",
                )
                results.append((item.path, target_path, None))
            except Exception as exc:
                results.append((item.path, target_path, str(exc)))
        return results

    def copy_pdf_failure_to_troubles(
        self,
        order: OrderCheck,
        troubles_dir: Path,
        reason: str,
    ) -> list[tuple[Path, Path, str | None]]:
        """Route an order that passed input checks but failed PDF creation."""
        results = []
        target_dir = troubles_dir / order.order_id
        for item in order.files:
            target_path = target_dir / item.path.name
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.path, target_path)
                reason_path = target_dir / f"{item.path.name}.error.txt"
                reason_path.write_text(
                    f"Файл: {item.path.name}\nСтатус: PDF НЕ СОЗДАН\nПричина:\n- {reason}\n",
                    encoding="utf-8",
                )
                results.append((item.path, target_path, None))
            except Exception as exc:
                results.append((item.path, target_path, str(exc)))
        return results

    def generate_pdf_previews(
        self,
        pdf_path: Path,
        preview_dir: Path,
        render_dpi: float = 150.0,
        safe_zone_mm: float | None = None,
        bleed_mm: float | None = None,
        page_names: Optional[list[str]] = None,
        fold_overlay: Mapping[str, object] | None = None,
    ) -> list[Path]:
        """Render PDF previews with standard frames or confirmed fold guides."""
        safe_zone_mm = self.profile.safe_zone_mm if safe_zone_mm is None else safe_zone_mm
        # Линия реза на превью исторически имеет толщину 1 мм; это не вылет.
        bleed_mm = 1.0 if bleed_mm is None else bleed_mm
        gs_cmd = shutil.which("gs")
        if not gs_cmd:
            raise FileNotFoundError("Ghostscript (`gs`) не найден для рендеринга превью.")

        preview_dir.mkdir(parents=True, exist_ok=True)
        created_previews: list[Path] = []

        with tempfile.TemporaryDirectory(prefix=f".preview_{pdf_path.stem}_") as temp_dir:
            temp_path = Path(temp_dir)
            rendered_pages: list[Path] = []
            if self.callas_enabled and self.callas_toolbox is not None:
                try:
                    self.callas_toolbox.save_as_image(pdf_path, temp_path, resolution=int(render_dpi))
                    rendered_pages = sorted(temp_path.glob("*.png"))
                    if not rendered_pages:
                        raise CallasToolboxError("callas не создал изображения preview")
                except Exception:
                    rendered_pages = []
            if not rendered_pages:
                if not gs_cmd:
                    raise FileNotFoundError("Ghostscript (`gs`) не найден для рендеринга превью.")
                page_pattern = temp_path / "page-%03d.png"
                command = [
                    gs_cmd, "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE",
                    "-sDEVICE=png16m", f"-r{int(render_dpi)}",
                    f"-sOutputFile={page_pattern}", str(pdf_path),
                ]
                result = run_command(command, capture_output=True, text=True)
                if result.returncode != 0:
                    details = (result.stderr or result.stdout).strip()
                    raise ValueError(f"ошибка рендеринга PDF для превью: {details}")
                rendered_pages = sorted(temp_path.glob("page-*.png"))
            if not rendered_pages:
                raise ValueError("не удалось извлечь страницы из PDF")

            total_pages = len(rendered_pages)
            for idx, rendered_page in enumerate(rendered_pages, start=1):
                meta = inspect_file(str(rendered_page))
                if page_names and idx <= len(page_names):
                    base_name = page_names[idx - 1]
                    preview_filename = f"{base_name}_preview.png"
                elif total_pages == 1:
                    preview_filename = f"{pdf_path.stem}_preview.png"
                else:
                    preview_filename = f"{pdf_path.stem}_page{idx}_preview.png"

                output_preview_path = preview_dir / preview_filename
                page_overlay = self._overlay_for_page(fold_overlay, idx)
                generate_preview(
                    input_path=str(rendered_page),
                    output_preview_path=str(output_preview_path),
                    dpi=meta.dpi or render_dpi,
                    w_px=meta.width_px,
                    h_px=meta.height_px,
                    safe_zone_mm=safe_zone_mm,
                    bleed_mm=bleed_mm,
                    fold_overlay=page_overlay,
                )
                created_previews.append(output_preview_path)

        return created_previews

    def generate_previews_for_all(
        self,
        pdf_paths: list[Path],
        preview_dir: Path,
        pdf_page_names_map: Optional[dict[Path, list[str]]] = None,
        fold_overlays_by_pdf: Optional[dict[Path, Mapping[str, object]]] = None,
    ) -> list[tuple[Path, list[Path], str | None]]:
        """Генерирует превью с рамками для всех передаваемых PDF файлов."""
        results = []
        pdf_map = pdf_page_names_map or {}
        overlays_map = fold_overlays_by_pdf or {}
        def create_one(pdf_path: Path) -> tuple[Path, list[Path], str | None]:
            try:
                previews = self.generate_pdf_previews(
                    pdf_path,
                    preview_dir,
                    page_names=pdf_map.get(pdf_path),
                    fold_overlay=overlays_map.get(pdf_path),
                )
                return pdf_path, previews, None
            except Exception as exc:
                return pdf_path, [], str(exc)

        with ThreadPoolExecutor(
            max_workers=self._preview_worker_count(len(pdf_paths)),
            thread_name_prefix="image-magic-preview",
        ) as executor:
            # executor.map preserves the input order, which is part of the
            # API consumed by the adapter and UI.
            results.extend(executor.map(create_one, pdf_paths))
        return results

    def generate_previews_for_files(
        self,
        files: list[FileCheck],
        preview_dir: Path,
        fold_overlays_by_file: Optional[dict[Path, Mapping[str, object]]] = None,
    ) -> list[tuple[FileCheck, list[Path], str | None]]:
        """Generate previews directly from every available source file.

        Unlike ``generate_previews_for_all``, this method deliberately does
        not depend on a successfully assembled production PDF.  A source file
        can therefore still be reviewed when it fails prepress validation.
        """
        overlays_map = fold_overlays_by_file or {}

        def create_one(file_check: FileCheck) -> tuple[FileCheck, list[Path], str | None]:
            try:
                overlay = self._overlay_for_file(overlays_map.get(file_check.path), file_check)
                if file_check.path.suffix.lower() == ".pdf":
                    page_count = file_check.page_count or 1
                    if page_count == 2 and file_check.parsed.back_colors > 0:
                        # One two-page PDF is a complete duplex layout.  Give
                        # its previews semantic names so every downstream
                        # consumer (the UI and Sborka return collage) can
                        # distinguish the face from the back.
                        page_names = [
                            f"{file_check.path.stem}_face",
                            f"{file_check.path.stem}_back",
                        ]
                        if overlay is not None:
                            overlay["page_sides"] = {1: "face", 2: "back"}
                    elif page_count == 1:
                        page_names = [file_check.path.stem]
                    else:
                        page_names = [
                            f"{file_check.path.stem}_page{page_number}"
                            for page_number in range(1, page_count + 1)
                        ]
                    previews = self.generate_pdf_previews(
                        file_check.path,
                        preview_dir,
                        page_names=page_names,
                        fold_overlay=overlay,
                    )
                else:
                    meta = inspect_file(str(file_check.path))
                    preview_path = preview_dir / f"{file_check.path.stem}_preview.png"
                    generate_preview(
                        input_path=str(file_check.path),
                        output_preview_path=str(preview_path),
                        dpi=meta.dpi or 300.0,
                        w_px=meta.width_px,
                        h_px=meta.height_px,
                        safe_zone_mm=self.profile.safe_zone_mm,
                        bleed_mm=1.0,
                        fold_overlay=overlay,
                    )
                    previews = [preview_path]
                return file_check, previews, None
            except Exception as exc:
                return file_check, [], str(exc)

        results: list[tuple[FileCheck, list[Path], str | None]] = []
        with ThreadPoolExecutor(
            max_workers=self._preview_worker_count(len(files)),
            thread_name_prefix="image-magic-preview",
        ) as executor:
            results.extend(executor.map(create_one, files))
        return results

    @staticmethod
    def _overlay_for_page(
        overlay: Mapping[str, object] | None,
        page_number: int,
    ) -> dict[str, object] | None:
        """Return a page-specific overlay without mutating caller-owned data."""
        if overlay is None:
            return None
        resolved = dict(overlay)
        page_sides = resolved.pop("page_sides", None)
        if isinstance(page_sides, Mapping):
            side = page_sides.get(page_number, page_sides.get(str(page_number)))
            if side is not None:
                resolved["side"] = side
        return resolved

    @staticmethod
    def _overlay_for_file(
        overlay: Mapping[str, object] | None,
        file_check: FileCheck,
    ) -> dict[str, object] | None:
        resolved = BatchProcessor._overlay_for_page(overlay, 1)
        if resolved is None:
            return None
        if file_check.parsed:
            resolved.setdefault("side", file_check.parsed.side or "face")
            span = (
                file_check.parsed.width_mm
                if resolved.get("axis", "width") == "width"
                else file_check.parsed.height_mm
            )
            resolved.setdefault("span_mm", span)
        return resolved
