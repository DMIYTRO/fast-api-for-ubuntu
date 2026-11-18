from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid

import pymupdf
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Collection, Mapping, Optional, Sequence

if TYPE_CHECKING:  # services.__init__ imports this module; avoid a cycle
    from services.callas import CallasClient

from core.inspector import inspect_file, inspect_raster
from core.pdf_inspector import inspect_pdf
from core.pdf_exporter import convert_image_to_pdf
from core.preview_cache import preview_extension
from core.preview_generator import generate_preview
from core.preview_cache import full_preview_cache_path, preview_work_dir
from core.resampler import canvas_image, resample_image
from core.tool_runner import run_command
from config.profiles import DEFAULT_PROFILE, PrePressProfile
from .resample_policy import (
    METHOD_CANVAS,
    ResampleDecision,
    analyze_canvas,
    analyze_resample,
    canvas_change_text,
    is_shrink,
)
from .pdf_resize_policy import (
    METHOD_FILL,
    METHOD_REVEAL,
    METHOD_SHRINK,
    METHOD_STRETCH,
    PdfResizeLimits,
    page_min_dpi,
    plan_pdf_page_resize,
    worst_plan,
)
from .profile_rules import evaluate_metadata_rules

from .filename_parser import parse_filename
from .models import FileCheck, OrderCheck, make_size_fix


logger = logging.getLogger("image_magic.callas")
# Сколько заказов одновременно ждут полей Callas при осмотре (см. iter_inspect_orders).
AUTO_BLEED_ORDER_WINDOW = 4

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}
REJECTED_EXTENSIONS = {".psd", ".bmp", ".heic", ".heif"}
# PDF point / pixel arithmetic can undershoot an exact boundary by several
# millionths (e.g. 249.9999957 for a nominal 250 DPI image).
DPI_EPSILON = 0.01


# --- Классификация проверок файла: геометрия или содержимое --------------------------
#
# После принятого исправления Callas, которое только дорисовало вылеты (``bleed_only``,
# ``services/callas_bleed.py``), файл проверяется дважды: исправленный PDF даёт
# ГЕОМЕТРИЮ (то, что меняет исправление: размер страницы, боксы, число страниц,
# поворот, решение по размеру, целостность печатаемого файла), исходник клиента —
# СОДЕРЖИМОЕ (то, что принадлежит макету: разрешение, цвет, альфа/слои/страницы
# растра, вес файла, цветность в имени). Так однопиксельные полосы вылета с ~13 dpi
# не дают ошибку разрешения, а настоящая проблема макета клиента — даёт.
# Каждая проверка ниже обязана иметь здесь запись (``_report`` падает на неизвестной).
SCOPE_GEOMETRY = "geometry"
SCOPE_CONTENT = "content"
ALL_SCOPES = frozenset({SCOPE_GEOMETRY, SCOPE_CONTENT})
CHECK_SCOPES: Mapping[str, str] = MappingProxyType({
    # PDF
    "pdf_structure": SCOPE_GEOMETRY,  # не открывается/зашифрован/нет страниц: печатается исправленный файл
    "pdf_page_size": SCOPE_GEOMETRY,  # размер страницы не совпадает с форматом + вылеты
    "pdf_page_size_tolerance": SCOPE_GEOMETRY,  # отклонение размера в пределах допуска
    "pdf_rotation": SCOPE_GEOMETRY,  # недопустимый /Rotate
    "pdf_size_correction": SCOPE_GEOMETRY,  # решение по размеру QuickFix (_record_pdf_resize)
    "pdf_effective_dpi": SCOPE_CONTENT,  # effective DPI растров на странице
    "pdf_rgb_image": SCOPE_CONTENT,  # RGB-растр без ICC
    "pdf_unknown_colorspace": SCOPE_CONTENT,  # неизвестная цветовая модель растра
    # Растр (TIFF/JPEG/PNG)
    "raster_alpha": SCOPE_CONTENT,  # альфа-канал TIFF
    "raster_layers": SCOPE_CONTENT,  # несведённые слои без известного числа
    "raster_pages": SCOPE_CONTENT,  # многостраничный TIFF / несколько кадров
    "raster_size": SCOPE_GEOMETRY,  # решение по размеру (коррекция, поворот, уменьшение DPI)
    "raster_metadata": SCOPE_CONTENT,  # вес файла, цветовая модель, DPI (profile_rules)
    "color_mode": SCOPE_CONTENT,  # цветность в имени файла
})
# Поля FileCheck, которые описывают содержимое: при ``bleed_only`` берутся из исходника.
CONTENT_FIELDS = (
    "dpi", "dpi_x", "dpi_y", "width_px", "height_px", "colorspace", "actual_format",
    "has_alpha", "has_unflattened_layers", "channels", "tiff_page_count", "tiff_layer_count",
    "pdf_colorspaces", "pdf_min_dpi", "pdf_content_type",
)


def _accepted_fix_state(content_path: Path, source: Path) -> Mapping[str, object] | None:
    """``summary`` исправления Callas, лежащего рядом с ``content_path``, для ``source``."""
    import json

    try:
        state = json.loads((Path(content_path).parent / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or (state.get("source") or {}).get("name") != Path(source).name:
        return None
    summary = state.get("summary")
    return summary if isinstance(summary, dict) else None


def _fix_is_bleed_only(fix: Mapping[str, object] | None) -> bool:
    return isinstance(fix, Mapping) and fix.get("bleed_only") is True


def _report(
    check: FileCheck, kind: str, level: str, message: str, scopes: Collection[str] = ALL_SCOPES
) -> None:
    """Записать находку ``kind`` (см. ``CHECK_SCOPES``), если её область проверяется."""
    if CHECK_SCOPES[kind] not in scopes:
        return
    (check.errors if level == "error" else check.warnings).append(message)


def _is_pdf_content(item: FileCheck) -> bool:
    """Содержимое файла — PDF (исходный PDF или принятое исправление растра/PDF)."""
    return item.input_path.suffix.lower() == ".pdf"


def _is_canvas_correction(item: FileCheck) -> bool:
    """Коррекция растра «без масштабирования» (белые поля / обрезка в исходном DPI).

    Уменьшение высокого DPI (``downsampled_from_dpi``) — не коррекция размера и идёт
    обычным ресемплингом.
    """
    return bool(
        item.no_scale
        and item.needs_resample
        and item.resample_target_mm
        and item.downsampled_from_dpi is None
        and not _is_pdf_content(item)
    )


def _read_raster_facts(check: FileCheck, scopes: Collection[str] = ALL_SCOPES) -> bool:
    """Сведения о растре (одна инспекция ImageMagick); ``False`` — дальше не проверять."""
    raster = inspect_raster(str(check.path))
    structure = raster.tiff
    if structure is not None:
        check.tiff_page_count = structure.page_count
        check.has_unflattened_layers = structure.has_unflattened_layers
        check.tiff_layer_count = structure.layer_count
        check.has_alpha = structure.has_alpha
        check.channels = structure.channels
        if structure.has_alpha:
            _report(check, "raster_alpha", "error",
                    "TIFF содержит альфа-канал; перед отправкой удалите прозрачность", scopes)
        if structure.has_unflattened_layers and not structure.layer_count:
            _report(check, "raster_layers", "error",
                    "TIFF содержит несведённые слои, но ImageMagick не смог "
                    "определить их число; требуется ручное решение", scopes)
        if structure.page_count > 1:
            _report(check, "raster_pages", "error",
                    f"TIFF содержит {structure.page_count} страниц; "
                    "разрешён только одностраничный файл", scopes)
    elif raster.frame_count != 1:
        _report(check, "raster_pages", "error",
                f"файл содержит {raster.frame_count} страниц/изображений; разрешён только одностраничный файл",
                scopes)
        return False
    meta = raster.metadata
    check.actual_width_mm = meta.width_mm
    check.actual_height_mm = meta.height_mm
    check.width_px = meta.width_px
    check.height_px = meta.height_px
    check.dpi = meta.dpi
    check.dpi_x = meta.dpi_x
    check.dpi_y = meta.dpi_y
    check.actual_format = meta.format.upper()
    check.colorspace = meta.colorspace
    check.size_mb = meta.size_mb
    return True


def _flatten_for_pdf(item: FileCheck, source_image_path: str, flat_path: Path):
    """Видимые слои TIFF, сведённые во временный файл, как при сборке заказа."""
    if not (item.has_unflattened_layers and item.tiff_layer_count):
        return source_image_path, None
    from core.tiff_flatten import flatten_visible_tiff

    flattened = flatten_visible_tiff(item.path, flat_path)
    return str(flat_path), flattened


def _record_flatten(item: FileCheck, flattened) -> None:
    if flattened is None:
        return
    item.tiff_flattened = True
    item.tiff_visible_layer_count = flattened.visible_layer_count
    item.tiff_hidden_layer_count = flattened.hidden_layer_count
    item.tiff_flatten_strategy = flattened.strategy


def _convert_raster_page(
    item: FileCheck, source_image_path: str, page_path: Path, dpi_arg: str | float, strip_icc_cmyk: bool
) -> None:
    """Растр → одностраничный PDF ImageMagick: то же DPI, без перевода цвета."""
    convert_image_to_pdf(
        source_image_path,
        str(page_path),
        dpi=dpi_arg,
        compression="none",
        ignore_tiff_layers=item.tiff_layer_count == 1,
        strip_icc=strip_icc_cmyk and (item.colorspace or "").upper() == "CMYK",
    )


def build_raster_source_pdf(source: Path | str, target: Path | str, *, strip_icc_cmyk: bool = False) -> FileCheck:
    """Одностраничный PDF из растра заказа — ровно как растр попадает в PDF заказа.

    Сведение видимых слоев TIFF, DPI файла по осям, без пересэмплирования и без
    перевода цвета (``strip_icc_cmyk`` — только снятие тега ICC у CMYK, как
    ``IMAGE_MAGIC_STRIP_ICC_CMYK``). Нужен исправлению Callas растрового исходника
    (``PDF/CallasFix/<имя>.source/source.pdf``). Исходный растр не меняется.
    """
    source_path = Path(source)
    target_path = Path(target)
    check = FileCheck(path=source_path)
    if not _read_raster_facts(check):
        raise ValueError(check.errors[-1] if check.errors else "растр не прочитан")
    if check.errors:
        raise ValueError("; ".join(check.errors))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".raster-pdf-", dir=target_path.parent) as temporary_dir:
        image_path, flattened = _flatten_for_pdf(
            check, str(source_path), Path(temporary_dir) / "flattened.tif"
        )
        _convert_raster_page(check, image_path, target_path, f"{check.dpi_x}x{check.dpi_y}", strip_icc_cmyk)
        _record_flatten(check, flattened)
    return check


CALLAS_RENDER_UNAVAILABLE_MESSAGE = (
    "Рендер PDF выполняется только через callas pdfToolbox, но callas не настроен: "
    "задайте IMAGE_MAGIC_CALLAS_URL и IMAGE_MAGIC_CALLAS_TOKEN и перезапустите сервис. "
    "Ghostscript для PDF больше не используется."
)


class CallasRenderUnavailableError(RuntimeError):
    """callas pdfToolbox не настроен, а рендер PDF без него запрещён."""

    def __init__(self, message: str = CALLAS_RENDER_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class _LogicalPageRef:
    """A page in the final PDF and the source page it must represent."""

    source: FileCheck
    source_page_index: int
    expected_side: str
    expected_width_mm: float
    expected_height_mm: float


def _resized(item: FileCheck, page) -> tuple[float, float]:
    """Ожидаемый размер страницы PDF в итоговом файле: после QuickFix — целевой."""
    plan = item.pdf_resize_plans.get(page.page_number) if item.needs_resample else None
    if plan is not None:
        return plan.target_mm
    return (page.width_mm, page.height_mm)


def _logical_side(item: FileCheck) -> str:
    """Return the production side, including a side-less 4-0 PDF."""
    return item.parsed.side or "face"


CALLAS_MERGE_UNAVAILABLE_MESSAGE = (
    "Склейка PDF невозможна: callas pdfToolbox не настроен. "
    "Задайте IMAGE_MAGIC_CALLAS_URL (и общий ресурс IMAGE_MAGIC_CALLAS_SHARE) "
    "и перезапустите службу; локальная склейка PyMuPDF отключена."
)


class BatchProcessor:
    # Тестовый шов: клиент рендера, если callas_client не передан. В production None.
    default_render_client: "CallasClient | None" = None

    def __init__(
        self,
        input_dir: Path,
        output_dir: Path,
        size_extra_mm: float | None = None,
        tolerance_mm: float | None = None,
        min_dpi: float | None = None,
        *,
        profile: PrePressProfile = DEFAULT_PROFILE,
        callas_client: "CallasClient | None" = None,
        callas_merge: bool = False,
        callas_render: bool = False,
        strip_icc_cmyk: bool = False,
        pdf_merger: "Callable[[Sequence[Path], Path, str], None] | None" = None,
        pdf_color_converter: "Callable[[Path, Path, str], object] | None" = None,
        pdf_prepress_checker: "Callable[..., dict[str, object]] | None" = None,
        correction_policy: str = "auto",
        pdf_resizer: "Callable[[Path, Path, dict[int, tuple[float, float]], float, str], object] | None" = None,
        auto_bleed: "Callable[[FileCheck, BatchProcessor], FileCheck | None] | None" = None,
        auto_bleed_max_concurrent: int = 2,
    ):
        # Поля вылета Callas при осмотре файла (services/callas_auto_bleed.py): хук
        # возвращает новую проверку файла из принятого исправления или None. Только
        # при correction_policy == "auto"; None — шаг не выполняется (CLI, тесты).
        self.auto_bleed = auto_bleed
        self.auto_bleed_max_concurrent = auto_bleed_max_concurrent
        # "auto" — малые коррекции применяются сами (CLI, тесты); "ask" и "reject" —
        # любая коррекция размера требует решения оператора (подтверждение или отказ).
        self.correction_policy = correction_policy
        # Коррекция размера PDF QuickFix callas (services/callas/page_resize.py); None —
        # PDF не масштабируется, несовпадение размера остаётся ошибкой.
        self.pdf_resizer = pdf_resizer
        # Допечатные проверки итогового PDF (шрифты, ppi, плашка K100); None — не проверяем.
        self.pdf_prepress_checker = pdf_prepress_checker
        # Перевод RGB → CMYK итогового PDF через callas; None — не конвертируем.
        self.pdf_color_converter = pdf_color_converter
        # Только CMYK-растры: убираем тег ICCBased без пересчёта значений.
        self.strip_icc_cmyk = bool(strip_icc_cmyk)
        self.callas_client = callas_client
        # Склейка PDF всегда идёт через callas pdfToolbox. Флаг ``callas_merge``
        # оставлен только для совместимости вызовов и больше ни на что не влияет.
        del callas_merge
        if pdf_merger is None and callas_client is not None:
            from services.callas.merger import make_callas_pdf_merger

            pdf_merger = make_callas_pdf_merger(callas_client)
        self.pdf_merger = pdf_merger
        # Все рендеры PDF (контрольный и превью) идут только через callas;
        # флаг callas_render оставлен для совместимости и ничего не выбирает.
        del callas_render
        self.render_client = callas_client if callas_client is not None else self.default_render_client
        self.callas_render = self.render_client is not None
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
        # Unparsed/unsupported files go to Troubles once per run and target
        # folder, however many failed orders call copy_failed_to_troubles
        # (the adapter calls it once per order from pool threads).
        self._troubles_extras_lock = threading.Lock()
        self._troubles_extras_claimed: set[Path] = set()
        # Optional hook called with the sorted order ids once filenames are
        # parsed and grouped, before the slow per-order inspection starts.
        self.on_orders_grouped: Callable[[list[str]], None] | None = None

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
        wanted = SUPPORTED_EXTENSIONS | REJECTED_EXTENSIONS
        # os.scandir entries carry the file type from the directory listing, so
        # is_file() costs no extra stat per entry (matters on a network share);
        # the cheap suffix test runs first and spares even that for junk files.
        with os.scandir(self.input_dir) as entries:
            return sorted(
                self.input_dir / entry.name
                for entry in entries
                if Path(entry.name).suffix.lower() in wanted and entry.is_file()
            )

    def inspect_orders(self) -> list[OrderCheck]:
        """Inspect all orders, preserving the legacy list-returning API."""
        return list(self.iter_inspect_orders())

    def iter_inspect_orders(self):
        """Yield validated orders as soon as each order has been inspected.

        Folder discovery and filename parsing are intentionally completed
        first: this preserves deterministic grouping, sorting, and the legacy
        ``unparsed``/``unsupported`` collections.  Expensive image inspection
        then proceeds in order, overlapping inspections with auto bleed
        corrections so callers can publish partial results.
        """
        grouped: dict[tuple[str, str], list[FileCheck]] = defaultdict(list)
        self.unparsed = []
        self.unsupported = []
        self.scanned_order_count = 0
        with self._troubles_extras_lock:
            self._troubles_extras_claimed = set()

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
        if self.on_orders_grouped is not None:
            try:
                self.on_orders_grouped(list(dict.fromkeys(oid for (_c, oid), _f in sorted_groups)))
            except Exception:
                logger.exception("orders_grouped hook failed")

        def finish(order_id: str, files: list[FileCheck]) -> OrderCheck:
            order = OrderCheck(
                order_id=order_id,
                customer_id=files[0].parsed.customer_id,
                files=files,
            )
            self._validate_order(order)
            return order

        def inspect(files: list[FileCheck]) -> None:
            for check in files:
                try:
                    if _is_pdf_content(check):
                        self._inspect_pdf_file(check)
                    else:
                        self._inspect_raster_file(check)
                except Exception as exc:
                    check.errors.append(f"не удалось прочитать файл: {exc}")

        if self.auto_bleed is None or self.correction_policy != "auto":
            for (_customer_id, order_id), files in sorted_groups:
                inspect(files)
                yield finish(order_id, files)
            return

        hook = self.auto_bleed

        def apply_auto_bleed(check: FileCheck) -> FileCheck:
            try:
                replacement = hook(check, self)
            except Exception:  # noqa: BLE001 — осмотр заказа не должен падать
                logger.exception("order.callas_auto_bleed_failed file=%s", check.path.name)
                return check
            return replacement if replacement is not None else check

        # Очередь на весь осмотр: файлы более ранних заказов попадают в пул первыми.
        # Пока выполняются исправления, основной поток осматривает следующие заказы.
        # Дополнительный поток перекрывает подготовку файлов с работой Callas;
        # общий семафор хука сохраняет настроенный лимит заданий Callas.
        window: deque[tuple[str, list[Future[FileCheck]]]] = deque()
        with ThreadPoolExecutor(
            max_workers=self.auto_bleed_max_concurrent + 1, thread_name_prefix="callas-auto-bleed"
        ) as pool:
            for (_customer_id, order_id), files in sorted_groups:
                inspect(files)
                window.append((order_id, [pool.submit(apply_auto_bleed, check) for check in files]))
                if len(window) >= AUTO_BLEED_ORDER_WINDOW:
                    ready_id, corrections = window.popleft()
                    yield finish(ready_id, [correction.result() for correction in corrections])
            for ready_id, corrections in window:
                yield finish(ready_id, [correction.result() for correction in corrections])

    def _inspect_raster_file(self, check: FileCheck, scopes: Collection[str] = ALL_SCOPES) -> None:
        """Read and validate a raster file (TIFF/JPEG/PNG).

        One cheap inspection per file (see core.inspector): TIFF structure,
        frame count and metadata share launches.
        """
        if _read_raster_facts(check, scopes):
            self._validate_file(check, scopes)

    def _inspect_pdf_file(self, check: FileCheck, scopes: Collection[str] = ALL_SCOPES) -> None:
        """Read and validate a PDF without routing it through ImageMagick.

        ``scopes`` — какие проверки записывать (``CHECK_SCOPES``); поля заполняются всегда.
        """
        inspection = inspect_pdf(check.input_path)
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
        check.size_mb = check.input_path.stat().st_size / (1024 * 1024)
        for message in inspection.errors:
            _report(check, "pdf_structure", "error", message, scopes)
        for message in inspection.warnings:
            _report(check, "pdf_structure", "warning", message, scopes)

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
        resize_plans = self._plan_pdf_resize(check, inspection.pages, expected)
        for page in inspection.pages:
            actual = (page.width_mm, page.height_mm)
            if resize_plans is not None:
                pass  # размер приводится QuickFix callas, решение записано ниже
            elif not self._dimensions_match(actual, expected) and not self._dimensions_match(
                actual, (expected[1], expected[0])
            ):
                # PDF не масштабируется и не обрезается автоматически (в отличие от
                # растровых файлов): подсказываем оператору, что делать.
                _report(
                    check, "pdf_page_size", "error",
                    f"PDF страница {page.page_number} имеет размер "
                    f"{actual[0]:.1f}x{actual[1]:.1f} мм; ожидается "
                    f"{expected[0]:.1f}x{expected[1]:.1f} мм. Автоматическая коррекция "
                    "размера для PDF не выполняется: исправьте размер макета "
                    "(на карточке заказа доступен план Callas «Коррекция размера макета») "
                    "или пришлите PDF нужного размера",
                    scopes,
                )
            elif self._pdf_page_deviation(actual, expected) > self.profile.metadata_tolerance_mm:
                _report(
                    check, "pdf_page_size_tolerance", "warning",
                    f"PDF страница {page.page_number} имеет размер "
                    f"{actual[0]:.1f}x{actual[1]:.1f} мм, ожидается "
                    f"{expected[0]:.1f}x{expected[1]:.1f} мм: отклонение в пределах допуска "
                    f"{self.tolerance_mm:.1f} мм принято без коррекции "
                    "(для PDF коррекция размера не выполняется)",
                    scopes,
                )
            if page.rotation not in {0, 90, 180, 270}:
                _report(
                    check, "pdf_rotation", "error",
                    f"PDF страница {page.page_number} имеет недопустимый поворот "
                    f"{page.rotation}°",
                    scopes,
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
                    _report(
                        check, "pdf_effective_dpi", "error",
                        f"PDF страница {page.page_number}: effective DPI "
                        f"{min(dpi_values):.1f}; требуется не менее {self.min_dpi:.0f}",
                        scopes,
                    )
                colorspace = image.colorspace_name or "unknown"
                if "RGB" in colorspace and "ICCBased" not in colorspace:
                    _report(
                        check, "pdf_rgb_image", "warning",
                        f"PDF страница {page.page_number}: растровое изображение "
                        "имеет RGB colorspace",
                        scopes,
                    )
                elif colorspace == "unknown":
                    _report(
                        check, "pdf_unknown_colorspace", "error",
                        f"PDF страница {page.page_number}: неизвестная цветовая модель",
                        scopes,
                    )
        if resize_plans is not None and CHECK_SCOPES["pdf_size_correction"] in scopes:
            self._record_pdf_resize(check, resize_plans)

    def _plan_pdf_resize(
        self, check: FileCheck, pages: Sequence[object], expected: tuple[float, float]
    ) -> "dict[int, object] | None":
        """Планы QuickFix по страницам, если PDF нужно привести к формату; иначе None.

        Способы и пороги — ``processing/pdf_resize_policy.py`` (показать скрытые вылеты,
        пропорциональное заполнение как у растра, малое вытягивание). План строится для
        всех страниц, потому что QuickFix выставляет боксы каждой; страницы с /Rotate
        не трогаем. Файл с меткой «без масштабирования» в имени
        (``PrePressProfile.is_no_scale_name``) получает только ``canvas``: белые поля и/или
        центральная обрезка без масштаба, без показа скрытых вылетов и вытягивания.
        """
        check.no_scale = self.profile.is_no_scale_name(check.path.name)
        if self.pdf_resizer is None or not pages:
            return None
        mismatched = [
            page for page in pages
            if not self._dimensions_match((page.width_mm, page.height_mm), expected)
            and not self._dimensions_match((page.width_mm, page.height_mm), (expected[1], expected[0]))
        ]
        if not mismatched or any(page.rotation for page in pages):
            return None
        limits = PdfResizeLimits(
            min_dpi=self.min_dpi,
            metadata_tolerance_mm=self.profile.metadata_tolerance_mm,
            auto_mm=self.profile.auto_crop_mm,
            confirm_mm=self.profile.confirm_crop_mm,
            stretch_auto_pct=self.profile.pdf_stretch_auto_pct,
            stretch_confirm_pct=self.profile.pdf_stretch_confirm_pct,
            shrink_auto_mm=self.profile.shrink_auto_mm,
        )
        return {
            page.page_number: plan_pdf_page_resize(
                (page.width_mm, page.height_mm),
                expected,
                page_min_dpi(page.images),
                limits,
                getattr(page, "hidden_margins_mm", ()),
                no_scale=check.no_scale,
            )
            for page in pages
        }

    def _record_pdf_resize(self, check: FileCheck, plans: "dict[int, object]") -> None:
        plan = worst_plan(plans.values())
        if plan.decision == ResampleDecision.ACCEPT:
            return
        worst_page = next(number for number, value in plans.items() if value is plan)
        decision = plan.decision
        reason = plan.reason
        if self.correction_policy in {"ask", "reject"} and decision == ResampleDecision.AUTO_CORRECT:
            decision = ResampleDecision.ASK_CONFIRMATION
            reason = "коррекция размера PDF требует подтверждения пользователя"
        check.resample_decision = decision.value
        check.resample_reason = reason
        check.resample_scale = max(plan.scale)
        check.resample_crop_mm = plan.crop_mm
        check.resample_effective_dpi = (
            None if any(value == float("inf") for value in plan.effective_dpi) else plan.effective_dpi
        )
        page = next(p for p in check.pdf_pages if p.page_number == worst_page)
        actual = (page.width_mm, page.height_mm)
        target = plan.target_mm
        if decision == ResampleDecision.REJECT:
            check.errors.append(
                f"PDF: размер {actual[0]:.1f}x{actual[1]:.1f} мм; ожидается "
                f"{target[0]:.1f}x{target[1]:.1f} мм: {plan.reason}"
            )
            return
        check.pdf_resize_plans = dict(plans)
        check.resample_target_mm = target
        if decision == ResampleDecision.AUTO_CORRECT:
            # Вместо строки предупреждения — пометка коррекции (интерфейс показывает её значком).
            check.needs_resample = True
            check.size_fix = self._pdf_size_fix(check, auto=True)
        else:
            details = self._pdf_resize_details(plans)
            change = f"{actual[0]:.1f}x{actual[1]:.1f} → {target[0]:.1f}x{target[1]:.1f} мм"
            check.warnings.append(f"PDF: требуется подтверждение коррекции {change}; {details}")

    @classmethod
    def _pdf_resize_details(cls, plans: "Mapping[int, object]") -> str:
        """Пояснения по всем страницам, которые меняются (у многостраничного — с номером)."""
        changed = [
            (number, plan) for number, plan in sorted(plans.items())
            if plan.decision != ResampleDecision.ACCEPT
        ]
        prefix = len(plans) > 1
        return "; ".join(
            (f"стр. {number}: " if prefix else "") + cls._pdf_resize_detail(plan)
            for number, plan in changed
        )

    @classmethod
    def _pdf_size_fix(cls, check: FileCheck, *, auto: bool) -> "dict[str, object] | None":
        """Пометка ``size_fix`` по планам QuickFix: способ и размеры — худшей страницы."""
        plans = check.pdf_resize_plans
        if not plans:
            return None
        plan = worst_plan(plans.values())
        number = next(key for key, value in plans.items() if value is plan)
        page = next((p for p in check.pdf_pages if p.page_number == number), None)
        actual = (
            (page.width_mm, page.height_mm) if page is not None
            else (check.actual_width_mm or 0.0, check.actual_height_mm or 0.0)
        )
        target = plan.target_mm
        detail = (
            f"PDF {actual[0]:.1f}x{actual[1]:.1f} → {target[0]:.1f}x{target[1]:.1f} мм "
            f"(callas): {cls._pdf_resize_details(plans)}"
        )
        if not auto:
            detail += "; подтверждено оператором"
        return make_size_fix(plan.method, auto, actual, target, detail)

    @staticmethod
    def _pdf_resize_detail(plan) -> str:
        if plan.method == METHOD_CANVAS:
            # enlarge_mm у canvas — сдвиг краёв MediaBox со знаком (+ поле, − обрезка).
            pad = tuple(max(value, 0.0) for value in plan.enlarge_mm)
            cut = tuple(max(-value, 0.0) for value in plan.enlarge_mm)
            parts = []
            if max(pad) > 0.005:
                parts.append(f"белые поля {pad[0]:.2f}/{pad[1]:.2f} мм с каждой стороны")
            if max(cut) > 0.005:
                parts.append(f"обрезка {cut[0]:.2f}/{cut[1]:.2f} мм с каждой стороны")
            return "без масштабирования: " + (", ".join(parts) or "размер не меняется")
        if plan.method == METHOD_REVEAL:
            return (
                f"показаны скрытые вылеты: +{plan.enlarge_mm[0]:.2f}/{plan.enlarge_mm[1]:.2f} мм "
                "с каждой стороны, без масштабирования"
            )
        if plan.method == METHOD_STRETCH:
            return (
                f"вытягивание {plan.scale[0] * 100:.1f}% × {plan.scale[1] * 100:.1f}%, "
                f"искажение пропорций {plan.distortion_pct:.2f}%"
            )
        crop = f"обрезка {plan.crop_mm[0]:.2f}x{plan.crop_mm[1]:.2f} мм"
        if plan.method == METHOD_SHRINK:
            return f"пропорциональное уменьшение до {plan.scale[0] * 100:.1f}%, {crop}"
        if plan.scale[0] > 1.0005:
            return f"пропорциональное увеличение до {plan.scale[0] * 100:.1f}%, {crop}"
        return f"пропорции сохранены, {crop}"

    @staticmethod
    def _pdf_page_deviation(
        actual: tuple[float, float], expected: tuple[float, float]
    ) -> float:
        """Наименьшее отклонение размера страницы PDF в любой из двух ориентаций."""
        return min(
            max(abs(a - e) for a, e in zip(actual, target))
            for target in (expected, (expected[1], expected[0]))
        )

    @staticmethod
    def _pdf_pages(item: FileCheck) -> tuple[object, ...]:
        return item.pdf_pages if item.page_count is not None else ()

    def _validate_file(self, check: FileCheck, scopes: Collection[str] = ALL_SCOPES) -> None:
        if CHECK_SCOPES["raster_size"] in scopes:
            correction_has_enough_dpi = self._validate_raster_size(check)
        else:
            correction_has_enough_dpi = False
        self._validate_raster_content(check, correction_has_enough_dpi, scopes)

    def _validate_raster_size(self, check: FileCheck) -> bool:
        """Решение по размеру растра (геометрия); ``True`` — у коррекции хватает DPI."""
        parsed = check.parsed
        expected = (parsed.width_mm + self.size_extra_mm, parsed.height_mm + self.size_extra_mm)
        actual = (check.actual_width_mm, check.actual_height_mm)
        # The order of dimensions in the filename defines the format, not the
        # visual top of the artwork. Accept both orientations without rotating.
        # A possible face/back mismatch is handled later at order level.
        source_dpi = (check.dpi_x or 0.0, check.dpi_y or 0.0)
        # Метка «без масштабирования» в имени ((SRA3), (SRA3P), (BAN)): только белые поля
        # или центральная обрезка в исходном разрешении, без поворота.
        check.no_scale = self.profile.is_no_scale_name(check.path.name)
        if check.no_scale:
            plans = [
                analyze_canvas(
                    actual,
                    target,
                    source_dpi,
                    min_dpi=self.min_dpi,
                    metadata_tolerance_mm=self.profile.metadata_tolerance_mm,
                    auto_crop_mm=self.profile.auto_crop_mm,
                    confirm_crop_mm=self.profile.confirm_crop_mm,
                )
                for target in (expected, (expected[1], expected[0]))
            ]
        else:
            plans = [
                analyze_resample(
                    actual,
                    target,
                    source_dpi,
                    min_dpi=self.min_dpi,
                    metadata_tolerance_mm=self.profile.metadata_tolerance_mm,
                    auto_crop_mm=self.profile.auto_crop_mm,
                    confirm_crop_mm=self.profile.confirm_crop_mm,
                    allow_rotation=False,
                    shrink_auto_mm=self.profile.shrink_auto_mm,
                )
                for target in (expected, (expected[1], expected[0]))
            ]
        decision_rank = {
            ResampleDecision.ACCEPT: 0,
            ResampleDecision.AUTO_CORRECT: 1,
            ResampleDecision.ASK_CONFIRMATION: 2,
            ResampleDecision.REJECT: 3,
        }
        if check.no_scale:
            # Холст: при равном решении — меньшее изменение размера (поля или обрезка).
            plan = min(
                plans,
                key=lambda value: (
                    decision_rank[value.decision],
                    max(abs(value.target_mm[i] - actual[i]) for i in (0, 1)),
                ),
            )
        else:
            plan = min(plans, key=lambda value: (decision_rank[value.decision], max(value.crop_mm)))
        expected = plan.target_mm
        check.resample_decision = plan.decision.value
        check.resample_reason = plan.reason
        check.resample_scale = plan.scale
        check.resample_crop_mm = plan.crop_mm
        check.resample_effective_dpi = plan.effective_dpi
        check.rotation_degrees = plan.rotation_degrees

        if (
            self.correction_policy in {"ask", "reject"}
            and plan.decision == ResampleDecision.AUTO_CORRECT
        ):
            # Режим не «автоматический»: даже малую коррекцию не применяем молча.
            plan = replace(
                plan,
                decision=ResampleDecision.ASK_CONFIRMATION,
                reason=(
                    f"коррекция {canvas_change_text(actual, expected)} требует подтверждения пользователя"
                    if check.no_scale
                    else "пропорциональная коррекция требует подтверждения пользователя"
                ),
            )
            check.resample_decision = plan.decision.value
            check.resample_reason = plan.reason

        crop_text = (
            canvas_change_text(actual, expected)
            if check.no_scale
            else f"обрезка {plan.crop_mm[0]:.2f}x{plan.crop_mm[1]:.2f} мм"
        )
        rotation_text = f", поворот на {plan.rotation_degrees}°" if plan.rotation_degrees else ""
        if plan.decision == ResampleDecision.AUTO_CORRECT:
            check.needs_resample = True
            check.resample_target_mm = expected
            # Вместо строки предупреждения — пометка коррекции (интерфейс показывает её значком).
            check.size_fix = self._raster_size_fix(check, auto=True)
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

        self._plan_high_dpi_downsample(check, plan)

        return (
            plan.decision in {ResampleDecision.AUTO_CORRECT, ResampleDecision.ASK_CONFIRMATION}
            and min(plan.effective_dpi) >= self.min_dpi
        )

    def _validate_raster_content(
        self, check: FileCheck, correction_has_enough_dpi: bool, scopes: Collection[str] = ALL_SCOPES
    ) -> None:
        """Проверки содержимого растра: вес, цветовая модель, DPI, цветность в имени."""
        parsed = check.parsed
        metadata_rules = evaluate_metadata_rules(
            profile=self.profile,
            size_mb=check.size_mb,
            colorspace=check.colorspace,
            dpi_x=check.dpi_x,
            dpi_y=check.dpi_y,
            correction_has_enough_dpi=correction_has_enough_dpi,
        )
        for message in metadata_rules.errors:
            _report(check, "raster_metadata", "error", message, scopes)
        for message in metadata_rules.warnings:
            _report(check, "raster_metadata", "warning", message, scopes)

        color_mode = (parsed.front_colors, parsed.back_colors)
        if color_mode not in self.profile.allowed_color_modes:
            allowed = ", ".join(
                f"{front}-{back}" for front, back in sorted(self.profile.allowed_color_modes)
            )
            _report(
                check, "color_mode", "error",
                f"цветность {color_mode[0]}-{color_mode[1]} не поддерживается; разрешены: {allowed}",
                scopes,
            )

    def _plan_high_dpi_downsample(self, check: FileCheck, plan) -> None:
        """Уменьшить растр с верным размером, но разрешением выше ``profile.max_dpi``.

        Это не обрезка: цель — тот же размер в мм при целевом DPI профиля, поэтому
        политика коррекции размера не применяется.
        """
        if not self.profile.downsample_high_dpi or plan.decision != ResampleDecision.ACCEPT:
            return
        if _is_pdf_content(check):
            return
        source_dpi = max(check.dpi_x or 0.0, check.dpi_y or 0.0)
        if source_dpi <= self.profile.max_dpi + DPI_EPSILON:
            return
        check.needs_resample = True
        check.resample_target_mm = plan.target_mm
        check.downsampled_from_dpi = source_dpi
        check.warnings.append(
            f"разрешение {source_dpi:.0f} DPI уменьшено до "
            f"{self.profile.target_dpi:.0f} DPI (выше {self.profile.max_dpi:.0f} DPI)"
        )

    def _dimensions_match(self, actual: tuple[float, float], expected: tuple[float, float]) -> bool:
        return all(abs(a - e) <= self.tolerance_mm for a, e in zip(actual, expected))

    @staticmethod
    def _raster_size_fix(check: FileCheck, *, auto: bool) -> "dict[str, object] | None":
        """Пометка ``size_fix`` растра: уменьшение (масштаб < 1) или заполнение с обрезкой."""
        target = check.resample_target_mm
        if target is None or check.actual_width_mm is None or check.actual_height_mm is None:
            return None
        actual = (check.actual_width_mm, check.actual_height_mm)
        if check.no_scale:
            # Без масштабирования: белые поля и/или центральная обрезка, без поворота.
            detail = (
                f"{actual[0]:.1f}x{actual[1]:.1f} → {target[0]:.1f}x{target[1]:.1f} мм: "
                f"{canvas_change_text(actual, target, separator=', ')}"
            )
            if not auto:
                detail += "; подтверждено оператором"
            return make_size_fix(METHOD_CANVAS, auto, actual, target, detail)
        scale = check.resample_scale
        method = METHOD_SHRINK if is_shrink(actual, target) else METHOD_FILL
        crop = check.resample_crop_mm
        if method == METHOD_SHRINK:
            how = f"пропорциональное уменьшение до {scale * 100:.1f}%"
        elif scale > 1.0005:
            how = f"пропорциональное увеличение до {scale * 100:.1f}%"
        else:
            how = "пропорции сохранены"
        detail = (
            f"{actual[0]:.1f}x{actual[1]:.1f} → {target[0]:.1f}x{target[1]:.1f} мм: "
            f"{how}, обрезка {crop[0]:.2f}x{crop[1]:.2f} мм"
        )
        if check.rotation_degrees:
            detail += f", поворот на {check.rotation_degrees}°"
        if not auto:
            detail += "; подтверждено оператором"
        return make_size_fix(method, auto, actual, target, detail)

    @staticmethod
    def confirm_resample(check: FileCheck, approved: bool) -> None:
        if check.resample_decision != ResampleDecision.ASK_CONFIRMATION.value:
            return
        check.resample_confirmed = approved
        if approved:
            check.resample_decision = ResampleDecision.AUTO_CORRECT.value
            check.needs_resample = True
            # Пометка вместо строки «коррекция размера подтверждена пользователем».
            confirmed = (
                BatchProcessor._pdf_size_fix(check, auto=False)
                if check.pdf_resize_plans
                else BatchProcessor._raster_size_fix(check, auto=False)
            )
            check.size_fix = confirmed or check.size_fix
        else:
            check.resample_decision = ResampleDecision.REJECT.value
            check.errors.append("пользователь отказался от предложенной коррекции размера")

    def reinspect_pdf_file(
        self,
        item: FileCheck,
        content_path: Path | None,
        *,
        fix: Mapping[str, object] | None = None,
    ) -> FileCheck:
        """Новая проверка файла заказа по содержимому ``content_path``.

        ``content_path`` — исправленный PDF (принятое исправление Callas исходного PDF
        или растра) или ``None`` (снова сам исходник, PDF или растр). Возвращается новый
        ``FileCheck`` с тем же путём и разобранным именем; к нему применяются все
        обычные правила (размер, боксы, DPI, цвет, политика коррекции). Исходный
        ``item`` не меняется.

        ``fix`` — сведения о принятом исправлении (``bleed_only``); без него читаются
        из ``state.json`` рядом с ``content_path``. Если исправление только дорисовало
        вылеты, геометрия берётся из исправленного PDF, содержимое — из исходника
        (``CHECK_SCOPES``); иначе всё проверяется по исправленному PDF.
        """
        if content_path is not None and Path(content_path).suffix.lower() != ".pdf":
            raise ValueError("исправленный файл стороны должен быть PDF")
        check = FileCheck(path=item.path, parsed=item.parsed, content_path=content_path)
        try:
            if content_path is None:
                if _is_pdf_content(check):
                    self._inspect_pdf_file(check)
                else:
                    self._inspect_raster_file(check)
            elif _fix_is_bleed_only(fix if fix is not None else _accepted_fix_state(content_path, item.path)):
                self._inspect_bleed_only_fix(check)
            else:
                self._inspect_pdf_file(check)
        except Exception as exc:
            check.errors.append(f"не удалось прочитать файл: {exc}")
        return check

    def _inspect_bleed_only_fix(self, check: FileCheck) -> None:
        """Геометрия — из исправленного PDF, содержимое — из исходника клиента.

        Исходный PDF проверяется обычной проверкой PDF, растр — обычной проверкой
        растра; из них берутся только находки ``SCOPE_CONTENT`` и поля
        ``CONTENT_FIELDS``. Полосы вылета, дорисованные callas, в содержимое не входят.
        """
        self._inspect_pdf_file(check, {SCOPE_GEOMETRY})
        source = FileCheck(path=check.path, parsed=check.parsed)
        if source.path.suffix.lower() == ".pdf":
            self._inspect_pdf_file(source, {SCOPE_CONTENT})
        else:
            self._inspect_raster_file(source, {SCOPE_CONTENT})
        for name in CONTENT_FIELDS:
            setattr(check, name, getattr(source, name))
        check.errors.extend(source.errors)
        check.warnings.extend(source.warnings)

    def revalidate_order(self, order: OrderCheck) -> None:
        """Пересчитать проверки уровня заказа после замены файла (ориентация, стороны…).

        До сборки PDF ошибки и предупреждения заказа даёт только ``_validate_order``,
        поэтому они строятся заново, а не дописываются.
        """
        order.errors.clear()
        order.warnings.clear()
        self._validate_order(order)

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
                    self._merge_page_pdfs(page_pdfs, temporary_output, order)
                    self._convert_rgb_to_cmyk(temporary_output, order)
                    self._check_prepress(temporary_output, order)
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

    def create_diagnostic_pdf(
        self, order: OrderCheck, output_dir: Path
    ) -> tuple[Path | None, str | None]:
        """Build an "as is" PDF for an order that failed local inspection.

        No resampling, no correction, no size validation: every readable
        page is included so PitStop/Callas can still report on the artwork.
        Returns ``(path, note)``; ``path`` is ``None`` when nothing could be
        read, ``note`` lists skipped files (or the reason nothing was built).
        """
        ordered_files = self._ordered_files_for_creation(order)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{self._output_stem(order, ordered_files)}.pdf"
        skipped: list[str] = []
        with tempfile.TemporaryDirectory(
            prefix=f".{order.order_id}_diag_", dir=output_dir
        ) as temporary_dir:
            page_pdfs: list[str] = []
            for page_number, item in enumerate(ordered_files, start=1):
                try:
                    if _is_pdf_content(item):
                        with pymupdf.open(item.input_path) as document:
                            if document.page_count == 0:
                                raise ValueError("в PDF нет страниц")
                        if str(item.input_path) not in page_pdfs:
                            page_pdfs.append(str(item.input_path))
                        continue
                    if item.dpi_x and item.dpi_y:
                        dpi_arg: str | float = f"{item.dpi_x}x{item.dpi_y}"
                    else:
                        dpi_arg = item.dpi or 300.0
                    page_path = Path(temporary_dir) / f"{page_number}.pdf"
                    source_path = item.path
                    flattened = None
                    if item.has_unflattened_layers and item.tiff_layer_count:
                        from core.tiff_flatten import flatten_visible_tiff
                        source_path = Path(temporary_dir) / f"flat-{page_number}.tif"
                        flattened = flatten_visible_tiff(item.path, source_path)
                    convert_image_to_pdf(
                        str(source_path),
                        str(page_path),
                        dpi=dpi_arg,
                        compression="none",
                        ignore_tiff_layers=item.tiff_layer_count == 1,
                    )
                    if flattened is not None:
                        item.tiff_flattened = True
                        item.tiff_visible_layer_count = flattened.visible_layer_count
                        item.tiff_hidden_layer_count = flattened.hidden_layer_count
                        item.tiff_flatten_strategy = flattened.strategy
                    page_pdfs.append(str(page_path))
                except Exception as exc:
                    skipped.append(f"{item.path.name}: {exc}")
            if not page_pdfs:
                return None, "ни один файл заказа не удалось прочитать: " + "; ".join(skipped)
            combined = Path(temporary_dir) / "combined.pdf"
            self._merge_page_pdfs(page_pdfs, combined, order)
            self._render_pdf_pages(combined, Path(temporary_dir) / "validation")
            combined.replace(output_path)
        note = (
            "в диагностический PDF не вошли: " + "; ".join(skipped) if skipped else None
        )
        return output_path, note

    def _cleanup_callas_job(self, job_id: str, client=None) -> None:
        client = client if client is not None else self.callas_client
        assert client is not None
        try:
            client.delete(job_id)
        except Exception:
            pass
        shutil.rmtree(client.job_dir(job_id), ignore_errors=True)

    def _merge_page_pdfs(self, page_pdfs, temporary_output: Path, order: OrderCheck) -> None:
        """Merge page PDFs via callas pdfToolbox; there is no local fallback."""
        if self.pdf_merger is None:
            raise RuntimeError(CALLAS_MERGE_UNAVAILABLE_MESSAGE)
        self.pdf_merger([Path(item) for item in page_pdfs], temporary_output, order.order_id)
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise RuntimeError(
                "callas pdfToolbox не вернул объединённый PDF для заказа "
                f"{order.order_id}. Проверьте журнал службы callas и повторите проверку."
            )

    def _check_prepress(self, pdf_path: Path, order: OrderCheck) -> None:
        """Шрифты, растры выше предела ppi, плашка K100; сбой проверки заказ не роняет."""
        if self.pdf_prepress_checker is None:
            return
        try:
            order.prepress = self.pdf_prepress_checker(pdf_path, order.order_id, order.prepress)
        except Exception as exc:  # noqa: BLE001
            logger.warning("prepress.failed order_id=%s error=%s", order.order_id, exc)
            order.prepress = {"status": "failed", "error": str(exc).strip() or exc.__class__.__name__}

    def _convert_rgb_to_cmyk(self, pdf_path: Path, order: OrderCheck) -> None:
        """RGB → CMYK итогового PDF; при успехе снимает предупреждения про RGB."""
        if self.pdf_color_converter is None:
            return
        from services.callas.color_conversion import (
            STATUS_CONVERTED,
            convert_rgb_to_cmyk,
            rgb_warning,
        )

        record = convert_rgb_to_cmyk(pdf_path, self.pdf_color_converter, order.order_id)
        if record is None:
            return
        order.color_conversion = record
        if record["status"] == STATUS_CONVERTED:
            order.warnings[:] = [text for text in order.warnings if not rgb_warning(text)]
            for item in order.files:
                item.warnings[:] = [text for text in item.warnings if not rgb_warning(text)]
        else:
            order.warnings.append(
                f"Не удалось перевести RGB в CMYK: {record['error']}. PDF оставлен в RGB."
            )

    def _render_pdf_with_callas(
        self,
        pdf_path: Path,
        render_dpi: float,
        target_dir: Path,
        *,
        expected_pages: int | None = None,
    ) -> list[Path]:
        from services.callas import deterministic_job_id

        client = self.render_client
        if client is None:
            raise CallasRenderUnavailableError()

        # Уникальный job_id на операцию: см. комментарий в _merge_page_pdfs.
        # Хеш PDF сюда не входит: из-за случайной части он ничего не определял,
        # а чтение всего файла стоило секунды на больших PDF.
        job_id = deterministic_job_id(
            f"render-{pdf_path.stem}",
            str(int(render_dpi)),
            uuid.uuid4().hex[:12],
        )
        logger.info("callas.render.start pdf=%s job_id=%s dpi=%d", pdf_path.name, job_id, int(render_dpi))
        try:
            kwargs = {"expected_pages": expected_pages} if expected_pages is not None else {}
            pages = client.render(pdf_path, int(render_dpi), job_id=job_id, **kwargs)
            logger.info("callas.render.done pdf=%s job_id=%s pages=%d", pdf_path.name, job_id, len(pages))
            target_dir.mkdir(parents=True, exist_ok=True)
            copied = []
            for number, page in enumerate(pages, start=1):
                target = target_dir / f"page-{number:03d}.png"
                shutil.copyfile(page, target)
                copied.append(target)
            return copied
        finally:
            self._cleanup_callas_job(job_id, client)

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
                        expected_width_mm=_resized(item, page)[0],
                        expected_height_mm=_resized(item, page)[1],
                    )
                )
            return refs

        for item in ordered_files:
            if item.page_count == 1 and item.pdf_pages:
                page = item.pdf_pages[0]
                width_mm, height_mm = _resized(item, page)
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
            if _is_pdf_content(item):
                # Исходный PDF или принятое исправление Callas (в т. ч. растра).
                pdf_path = converted.get(item.path)
                if pdf_path is None:
                    pdf_path = self._resize_pdf_if_needed(item, page_number, temporary_dir)
                    converted[item.path] = pdf_path
                if pdf_path not in page_pdfs:
                    page_pdfs.append(pdf_path)
                continue

            source_image_path, flattened = _flatten_for_pdf(
                item, str(item.path), temporary_dir / f"flattened_{page_number}.tif"
            )
            dpi_arg = f"{item.dpi_x}x{item.dpi_y}"
            canvas = _is_canvas_correction(item)
            if canvas:
                # Без масштабирования: белые поля / обрезка в исходном разрешении.
                resampled_path = temporary_dir / (
                    f"canvas_{page_number}_{ref.expected_side}{item.path.suffix}"
                )
                canvas_image(
                    source_image_path,
                    str(resampled_path),
                    target_width_mm=item.resample_target_mm[0],
                    target_height_mm=item.resample_target_mm[1],
                    source_dpi=(float(item.dpi_x), float(item.dpi_y)),
                    ignore_tiff_layers=item.tiff_layer_count == 1,
                )
                source_image_path = str(resampled_path)
            elif item.needs_resample and item.resample_target_mm:
                resampled_path = temporary_dir / (
                    f"resampled_{page_number}_{ref.expected_side}{item.path.suffix}"
                )
                resample_image(
                    source_image_path,
                    str(resampled_path),
                    target_width_mm=item.resample_target_mm[0],
                    target_height_mm=item.resample_target_mm[1],
                    target_dpi=self.profile.target_dpi,
                    rotation_degrees=item.rotation_degrees,
                    ignore_tiff_layers=item.tiff_layer_count == 1,
                )
                source_image_path = str(resampled_path)
                dpi_arg = str(self.profile.target_dpi)

            page_path = temporary_dir / f"{page_number}_{ref.expected_side}.pdf"
            _convert_raster_page(item, source_image_path, page_path, dpi_arg, self.strip_icc_cmyk)
            if canvas:
                item.processing_actions.append({
                    "kind": "size_correction", "method": METHOD_CANVAS,
                    "target_mm": list(item.resample_target_mm),
                    "dpi": [item.dpi_x, item.dpi_y],
                })
            elif item.needs_resample and item.resample_target_mm:
                if item.downsampled_from_dpi is not None:
                    item.processing_actions.append({
                        "kind": "dpi_downsample", "from_dpi": item.downsampled_from_dpi,
                        "target_dpi": self.profile.target_dpi,
                        "target_mm": list(item.resample_target_mm),
                    })
                else:
                    item.processing_actions.append({
                        "kind": "size_correction", "target_mm": list(item.resample_target_mm),
                        "target_dpi": self.profile.target_dpi, "rotation_degrees": item.rotation_degrees,
                    })
            if self.strip_icc_cmyk and (item.colorspace or "").upper() == "CMYK":
                item.processing_actions.append({"kind": "strip_icc"})
            _record_flatten(item, flattened)
            converted[item.path] = str(page_path)
            page_pdfs.append(str(page_path))
        return page_pdfs

    def _resize_pdf_if_needed(self, item: FileCheck, page_number: int, temporary_dir: Path) -> str:
        """Исходный PDF или его копия, приведённая к формату QuickFix callas."""
        if not (item.needs_resample and item.pdf_resize_plans):
            return str(item.input_path)
        if self.pdf_resizer is None:
            raise ValueError("коррекция размера PDF запланирована, но callas QuickFix не настроен")
        resized = temporary_dir / f"resized_{page_number}_{item.path.stem}.pdf"
        plans = dict(item.pdf_resize_plans)
        self.pdf_resizer(item.input_path, resized, plans, self.size_extra_mm / 2.0, item.path.stem)
        item.processing_actions.append({
            "kind": "size_correction",
            "method": "callas_quickfix",
            "target_mm": list(item.resample_target_mm or ()),
            "pages": [
                {
                    "page": number,
                    "method": plan.method,
                    "scale": [round(value, 5) for value in plan.scale],
                    "crop_mm": list(plan.crop_mm),
                    "enlarge_mm": list(plan.enlarge_mm),
                }
                for number, plan in sorted(plans.items())
            ],
        })
        return str(resized)

    def _render_pdf_pages(self, pdf_path: Path, validation_dir: Path) -> list[Path]:
        """Контрольный рендер callas: доказывает, что PDF открывается; возвращает PNG страниц."""
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise ValueError("итоговый PDF не создан или файл пуст.")
        try:
            return self._render_pdf_with_callas(pdf_path, 72, validation_dir)
        except CallasRenderUnavailableError:
            raise
        except Exception as exc:
            raise ValueError(f"итоговый PDF повреждён или не открывается в callas: {exc}") from exc

    def _validate_created_pdf(
        self,
        pdf_path: Path,
        page_refs: list[_LogicalPageRef],
        validation_dir: Path,
    ) -> None:
        """Render the final PDF and verify readability, page count, order and page sizes."""
        rendered_pages = self._render_pdf_pages(pdf_path, validation_dir)
        if len(rendered_pages) != len(page_refs):
            raise ValueError(
                f"в итоговом PDF {len(rendered_pages)} страниц; ожидалось {len(page_refs)}"
            )

        for page_number, (rendered_page, ref) in enumerate(zip(rendered_pages, page_refs), start=1):
            page_meta = inspect_file(str(rendered_page))
            # Контрольный рендер callas выполняется при 72 DPI: 1 px = 1 pt.
            width_px = getattr(page_meta, "width_px", None)
            height_px = getattr(page_meta, "height_px", None)
            source = ref.source
            expected = (ref.expected_width_mm, ref.expected_height_mm)
            if source.needs_resample and source.resample_target_mm and not _is_pdf_content(source):
                expected = source.resample_target_mm
            if width_px and height_px:
                actual = (width_px * 25.4 / 72.0, height_px * 25.4 / 72.0)
            else:
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
        """Copy rejected inputs and a readable reason file without changing originals.

        Files of the given failed *orders* are copied on every call.  The
        unparsed/unsupported files of the run are copied by the first call for
        a given *troubles_dir* only (thread-safe), not once per failed order.
        """
        rejected: list[tuple[FileCheck, list[str], str]] = []
        for order in orders:
            if order.passed:
                continue
            order_reasons = [f"Ошибка заказа: {message}" for message in order.errors]
            for item in order.files:
                rejected.append((item, item.errors + order_reasons, order.order_id))

        with self._troubles_extras_lock:
            claim_extras = troubles_dir not in self._troubles_extras_claimed
            if claim_extras:
                self._troubles_extras_claimed.add(troubles_dir)
        if claim_extras:
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

        preview_dir.mkdir(parents=True, exist_ok=True)
        created_previews: list[Path] = []

        with tempfile.TemporaryDirectory(
            prefix=f".preview_{pdf_path.stem}_", dir=preview_work_dir(preview_dir / ".preview")
        ) as temp_dir:
            temp_path = Path(temp_dir)
            self._render_pdf_with_callas(pdf_path, render_dpi, temp_path)

            rendered_pages = sorted(temp_path.glob("page-*.png"))
            if not rendered_pages:
                raise ValueError("не удалось извлечь страницы из PDF")

            total_pages = len(rendered_pages)
            for idx, rendered_page in enumerate(rendered_pages, start=1):
                meta = inspect_file(str(rendered_page))
                if page_names and idx <= len(page_names):
                    base_name = page_names[idx - 1]
                    preview_filename = f"{base_name}_preview{preview_extension()}"
                elif total_pages == 1:
                    preview_filename = f"{pdf_path.stem}_preview{preview_extension()}"
                else:
                    preview_filename = f"{pdf_path.stem}_page{idx}_preview{preview_extension()}"

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
                    full_preview_path=str(full_preview_cache_path(output_preview_path)),
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
                if _is_pdf_content(file_check):
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
                        file_check.input_path,
                        preview_dir,
                        page_names=page_names,
                        fold_overlay=overlay,
                    )
                else:
                    meta = inspect_file(str(file_check.path))
                    preview_path = preview_dir / f"{file_check.path.stem}_preview{preview_extension()}"
                    generate_preview(
                        input_path=str(file_check.path),
                        output_preview_path=str(preview_path),
                        dpi=meta.dpi or 300.0,
                        w_px=meta.width_px,
                        h_px=meta.height_px,
                        safe_zone_mm=self.profile.safe_zone_mm,
                        bleed_mm=1.0,
                        fold_overlay=overlay,
                        full_preview_path=str(full_preview_cache_path(preview_path)),
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
