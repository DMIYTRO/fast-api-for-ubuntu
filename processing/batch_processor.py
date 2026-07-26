from collections import defaultdict
from pathlib import Path
import shutil
import subprocess
import tempfile

from core.inspector import count_frames, inspect_file
from core.pdf_exporter import convert_image_to_pdf, merge_pdfs_with_ghostscript
from core.preview_generator import generate_preview
from core.resampler import resample_image
from config.profiles import DEFAULT_PROFILE, PrePressProfile
from .resample_policy import ResampleDecision, analyze_resample

from .filename_parser import parse_filename
from .models import FileCheck, OrderCheck


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
REJECTED_EXTENSIONS = {".psd", ".bmp", ".heic", ".heif"}


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
    ):
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.profile = profile
        # Необязательные аргументы оставлены для совместимости и точечных тестов.
        self.size_extra_mm = profile.size_extra_mm if size_extra_mm is None else size_extra_mm
        self.tolerance_mm = profile.size_tolerance_mm if tolerance_mm is None else tolerance_mm
        self.min_dpi = profile.min_dpi if min_dpi is None else min_dpi
        self.unparsed: list[FileCheck] = []
        self.unsupported: list[FileCheck] = []
        self.scanned_order_count = 0

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
        grouped: dict[str, list[FileCheck]] = defaultdict(list)
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
            grouped[check.parsed.order_id].append(check)

        sorted_groups = sorted(grouped.items())
        self.scanned_order_count = len(sorted_groups)
        for order_id, files in sorted_groups:
            for check in files:
                try:
                    path = check.path
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

        allowed_colorspaces = {value.upper() for value in self.profile.allowed_colorspaces}
        if (check.colorspace or "").upper() not in allowed_colorspaces:
            check.warnings.append(
                f"цветовая модель {check.colorspace or 'не определена'} не входит в профиль "
                f"{self.profile.name} ({', '.join(self.profile.allowed_colorspaces)}); "
                "файл будет сохранён без преобразования цветовой модели"
            )

        correction_has_enough_dpi = (
            plan.decision in {ResampleDecision.AUTO_CORRECT, ResampleDecision.ASK_CONFIRMATION}
            and min(plan.effective_dpi) >= self.min_dpi
        )
        if (check.dpi is None or check.dpi < self.min_dpi) and not correction_has_enough_dpi:
            actual_dpi = (
                f"{check.dpi_x:.1f}x{check.dpi_y:.1f}"
                if check.dpi_x is not None and check.dpi_y is not None
                else "не определено"
            )
            check.errors.append(f"разрешение {actual_dpi} DPI; по обеим осям требуется не меньше {self.min_dpi:.0f} DPI")

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
            sides[item.parsed.side].append(item)

        if not sides["face"]:
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

        color_modes = {(item.parsed.front_colors, item.parsed.back_colors) for item in order.files}
        if len(color_modes) != 1:
            order.errors.append("у файлов заказа не совпадает цветность в имени")
            return

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

        _, back_colors = next(iter(color_modes))
        if back_colors > 0 and not sides["back"]:
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
            ordered_files = sorted(order.files, key=lambda item: 0 if item.parsed.side == "face" else 1)
            face_file = next(item for item in ordered_files if item.parsed.side == "face")
            output_path = self.output_dir / f"{face_file.path.stem}.pdf"
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f".{order.order_id}_",
                    dir=self.output_dir,
                ) as temporary_dir:
                    page_pdfs = []
                    for page_number, item in enumerate(ordered_files, start=1):
                        source_image_path = str(item.path)
                        dpi_arg = f"{item.dpi_x}x{item.dpi_y}"

                        if item.needs_resample and item.resample_target_mm:
                            resampled_path = Path(temporary_dir) / f"resampled_{page_number}_{item.parsed.side}{item.path.suffix}"
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

                        page_path = Path(temporary_dir) / f"{page_number}_{item.parsed.side}.pdf"
                        convert_image_to_pdf(
                            source_image_path,
                            str(page_path),
                            dpi=dpi_arg,
                            compression="none",
                        )
                        page_pdfs.append(str(page_path))

                    temporary_output = Path(temporary_dir) / "combined.pdf"
                    merge_pdfs_with_ghostscript(page_pdfs, str(temporary_output))
                    self._validate_created_pdf(
                        temporary_output,
                        ordered_files,
                        Path(temporary_dir) / "validation",
                    )
                    temporary_output.replace(output_path)
                results.append((order, output_path, None))
            except Exception as exc:
                results.append((order, output_path, str(exc)))
        return results

    def _validate_created_pdf(
        self,
        pdf_path: Path,
        ordered_files: list[FileCheck],
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
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            raise ValueError(f"итоговый PDF повреждён или не открывается: {details}")

        rendered_pages = sorted(validation_dir.glob("page-*.png"))
        if len(rendered_pages) != len(ordered_files):
            raise ValueError(
                f"в итоговом PDF {len(rendered_pages)} страниц; ожидалось {len(ordered_files)}"
            )

        for page_number, (rendered_page, source) in enumerate(zip(rendered_pages, ordered_files), start=1):
            page_meta = inspect_file(str(rendered_page))
            expected = (
                source.resample_target_mm
                if (source.needs_resample and source.resample_target_mm)
                else (source.actual_width_mm, source.actual_height_mm)
            )
            actual = (page_meta.width_mm, page_meta.height_mm)
            # A 72-DPI control render has a rounding step of about 0.35 mm.
            validation_tolerance = max(self.tolerance_mm, 0.6)
            if any(abs(a - e) > validation_tolerance for a, e in zip(actual, expected)):
                raise ValueError(
                    f"страница {page_number} ({source.parsed.side}) имеет размер "
                    f"{actual[0]:.1f}x{actual[1]:.1f} мм; ожидалось "
                    f"{expected[0]:.1f}x{expected[1]:.1f} мм"
                )

            expected_side = "face" if page_number == 1 else "back"
            if source.parsed.side != expected_side:
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
    ) -> list[Path]:
        """Рендерит страницы PDF и генерирует превью с рамками (1 мм зелёная наружная, 4 мм красная внутренняя)."""
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
            page_pattern = temp_path / "page-%03d.png"
            command = [
                gs_cmd,
                "-q",
                "-dSAFER",
                "-dBATCH",
                "-dNOPAUSE",
                "-sDEVICE=png16m",
                f"-r{int(render_dpi)}",
                f"-sOutputFile={page_pattern}",
                str(pdf_path),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
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
                generate_preview(
                    input_path=str(rendered_page),
                    output_preview_path=str(output_preview_path),
                    dpi=meta.dpi or render_dpi,
                    w_px=meta.width_px,
                    h_px=meta.height_px,
                    safe_zone_mm=safe_zone_mm,
                    bleed_mm=bleed_mm,
                )
                created_previews.append(output_preview_path)

        return created_previews

    def generate_previews_for_all(
        self,
        pdf_paths: list[Path],
        preview_dir: Path,
        pdf_page_names_map: Optional[dict[Path, list[str]]] = None,
    ) -> list[tuple[Path, list[Path], str | None]]:
        """Генерирует превью с рамками для всех передаваемых PDF файлов."""
        results = []
        pdf_map = pdf_page_names_map or {}
        for pdf_path in pdf_paths:
            try:
                page_names = pdf_map.get(pdf_path)
                previews = self.generate_pdf_previews(
                    pdf_path, preview_dir, page_names=page_names
                )
                results.append((pdf_path, previews, None))
            except Exception as exc:
                results.append((pdf_path, [], str(exc)))
        return results
