"""Автоматический поворот стороны, если лицо и оборот разной ориентации.

Заказ 26288404: имя 148x105, лицо 109×152 мм (вертикальное), оборот 152×109 мм
(горизонтальный) — раньше «ориентация сторон не совпадает» и диагностический PDF. Теперь одна
сторона поворачивается на ±90° (лицо на +D, оборот на −D), итоговый PDF настоящий, а заказ
красный: оператор сверяет превью и печатает с подтверждением.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck, ParsedFilename
from processing.side_rotation import (
    format_degrees,
    normalize_degrees,
    plan_side_rotation,
    side_rotation_message,
)
from services.batch_adapter import OrderArtifacts, completed_order_status
from services.callas.page_rotate import (
    CallasPdfRotator,
    PdfRotateError,
    build_rotate_quickfix,
)
from services.dto import order_check_to_dto
from services.order_journal import build_order_journal
from services.print_readiness import print_blockers, print_mode
from tests.fake_callas import FakeCallasMerger

MM = 72 / 25.4
needs_magick = pytest.mark.skipif(not shutil.which("magick"), reason="нужен ImageMagick")


# --------------------------------------------------------------------------- правило


@pytest.mark.parametrize(
    "face, back, name, side, degrees",
    [
        ((109, 152), (152, 109), (148, 105), "face", 90),   # заказ 26288404
        ((152, 109), (109, 152), (148, 105), "back", -90),
        ((109, 152), (152, 109), (105, 148), "back", -90),
        ((54, 54.2), (54, 54.2), (50, 50), None, None),     # совпадают
        ((91, 90), (90, 91), (50, 50), "back", -90),        # квадратное имя → оборот
        ((91, 90), (90, 91), (87, 86), "back", -90),        # почти квадрат: 91×90 и 90×91
        ((90, 91), (91, 90), (87, 86), "face", 90),
        ((90.3, 90), (90, 91), (87, 86), None, None),       # квадратная сторона — не поворачиваем
    ],
)
def test_plan_side_rotation(face, back, name, side, degrees):
    plan = plan_side_rotation(face, back, name, 90)
    if side is None:
        assert plan is None
        return
    assert plan["side"] == side and plan["degrees"] == degrees


def test_configured_direction_is_reversed_for_both_sides():
    assert plan_side_rotation((109, 152), (152, 109), (148, 105), -90)["degrees"] == -90
    assert plan_side_rotation((152, 109), (109, 152), (148, 105), -90)["degrees"] == 90


def test_degrees_normalisation_and_message():
    assert normalize_degrees("270") == -90
    assert normalize_degrees(-270) == 90
    assert normalize_degrees("45") == 90
    assert format_degrees(-90) == "−90"
    assert side_rotation_message({"side": "back", "degrees": -90}) == (
        "Сторона back автоматически повёрнута на −90°: проверьте ориентацию и "
        "совмещение лица и оборота"
    )


def test_settings_read_rotation_env(monkeypatch):
    from server.settings import Settings

    monkeypatch.setenv("IMAGE_MAGIC_SIDE_ROTATE_DEGREES", "270")
    monkeypatch.setenv("IMAGE_MAGIC_SIDE_AUTO_ROTATE_ENABLED", "0")
    settings = Settings.from_env()
    assert settings.side_rotate_degrees == -90 and settings.side_auto_rotate_enabled is False
    monkeypatch.setenv("IMAGE_MAGIC_SIDE_ROTATE_DEGREES", "33")
    monkeypatch.delenv("IMAGE_MAGIC_SIDE_AUTO_ROTATE_ENABLED")
    settings = Settings.from_env()
    assert settings.side_rotate_degrees == 90 and settings.side_auto_rotate_enabled is True


# --------------------------------------------------------------------------- _validate_order


def _parsed(side: str, size=(148.0, 105.0), back_colors: int = 4) -> ParsedFilename:
    return ParsedFilename(
        customer_id="22044", order_id="26288404", width_mm=size[0], height_mm=size[1],
        front_colors=4, back_colors=back_colors, side=side,
    )


def _file(side: str, actual, *, suffix=".jpg", size=(148.0, 105.0), name=None, **fields) -> FileCheck:
    check = FileCheck(
        path=Path(name or f"30_KS(K)_({size[0]:g}x{size[1]:g})_4-4_(22044-26288404)_1-{side}{suffix}"),
        parsed=_parsed(side, size),
    )
    check.actual_width_mm, check.actual_height_mm = actual
    for key, value in fields.items():
        setattr(check, key, value)
    return check


def _pdf_page(number: int, width: float, height: float, rotation: int = 0):
    return SimpleNamespace(page_number=number, width_mm=width, height_mm=height, rotation=rotation)


def _pdf_file(side, pages, **fields) -> FileCheck:
    first = pages[0]
    check = _file(side, (first.width_mm, first.height_mm), suffix=".pdf", **fields)
    check.page_count = len(pages)
    check.pdf_pages = tuple(pages)
    check.rotation_degrees = first.rotation
    return check


def _order(*files: FileCheck) -> OrderCheck:
    return OrderCheck(order_id="26288404", customer_id="22044", files=list(files))


def _validate(order: OrderCheck, **options) -> OrderCheck:
    options.setdefault("pdf_rotator", object())
    BatchProcessor(Path("in"), Path("out"), **options)._validate_order(order)
    return order


ORIENTATION_ERROR = "ориентация сторон не совпадает"


def test_face_disagreeing_with_name_is_rotated_clockwise():
    face, back = _file("face", (113, 156)), _file("back", (156, 113))
    order = _validate(_order(face, back))
    assert order.errors == [] and order.passed
    assert order.side_alignment["side"] == "face"
    assert order.side_alignment["degrees"] == 90
    assert order.side_alignment["from"] == "вертикальная" and order.side_alignment["to"] == "горизонтальная"
    assert face.side_rotation_degrees == 90 and back.side_rotation_degrees == 0


def test_back_is_rotated_counterclockwise():
    face, back = _file("face", (156, 113)), _file("back", (113, 156))
    order = _validate(_order(face, back))
    assert order.side_alignment["side"] == "back" and back.side_rotation_degrees == -90


def test_orientation_uses_planned_correction_target():
    # Почти квадрат: фактически оборот 91×90, но после коррекции он 90×91 (как лицо) — поворота нет.
    face = _file("face", (90, 91), size=(86.0, 87.0))
    back = _file("back", (91, 90), size=(86.0, 87.0), resample_target_mm=(90.0, 91.0), needs_resample=True)
    order = _validate(_order(face, back))
    assert order.side_alignment is None and order.errors == []


@pytest.mark.parametrize(
    "case",
    ["disabled", "file_error", "order_error", "no_scale", "square_side", "pdf_without_rotator"],
)
def test_old_error_is_kept_when_rotation_is_excluded(case):
    face, back = _file("face", (113, 156)), _file("back", (156, 113))
    options = {}
    if case == "disabled":
        options["side_auto_rotate"] = False
    elif case == "file_error":
        face.errors.append("разрешение 150 DPI ниже минимального 250 DPI")
    elif case == "order_error":
        back.parsed = ParsedFilename("99999", "26288404", 148.0, 105.0, 4, 4, "back")
    elif case == "no_scale":
        face.no_scale = True
    elif case == "square_side":
        back.actual_width_mm, back.actual_height_mm = 113.2, 113.0
    elif case == "pdf_without_rotator":
        face = _pdf_file("face", [_pdf_page(1, 113, 156)])
        options["pdf_rotator"] = None
    order = _validate(_order(face, back), **options)
    assert order.side_alignment is None
    assert any(error.startswith(ORIENTATION_ERROR) for error in order.errors), order.errors
    assert face.side_rotation_degrees == 0 and back.side_rotation_degrees == 0


def test_one_sided_order_is_never_rotated():
    face = _file("face", (113, 156))
    face.parsed = _parsed("face", back_colors=0)
    order = _validate(_order(face))
    assert order.side_alignment is None and order.errors == []


def test_pdf_side_and_page_with_rotate_attribute():
    # /Rotate 90 у страницы 156×113 — отображается 113×156 (вертикально), как в имени нет.
    face = _pdf_file("face", [_pdf_page(1, 156, 113, rotation=90)])
    back = _file("back", (156, 113))
    order = _validate(_order(face, back))
    assert order.side_alignment["side"] == "face" and order.side_alignment["page"] == 1
    assert face.pdf_page_rotations == {1: 90}


def test_two_page_pdf_rotates_only_one_page():
    item = _pdf_file("", [_pdf_page(1, 156, 113), _pdf_page(2, 113, 156)])
    item.parsed = _parsed(None)
    order = _validate(_order(item))
    assert order.side_alignment["side"] == "back" and order.side_alignment["page"] == 2
    assert item.pdf_page_rotations == {2: -90}


def test_settle_restores_old_error_after_rejected_correction():
    face, back = _file("face", (113, 156)), _file("back", (156, 113))
    processor = BatchProcessor(Path("in"), Path("out"))
    order = _order(face, back)
    processor._validate_order(order)
    assert order.side_alignment is not None
    back.errors.append("пользователь отказался от предложенной коррекции размера")
    processor.settle_side_alignment(order)
    assert order.side_alignment is None
    assert any(error.startswith(ORIENTATION_ERROR) for error in order.errors)


# --------------------------------------------------------------------------- сборка растров


def _marked_jpeg(path: Path, width_mm: float, height_mm: float, dpi: int = 300) -> None:
    """Белый CMYK JPEG с красной меткой в левом верхнем углу."""
    width, height = round(width_mm * dpi / 25.4), round(height_mm * dpi / 25.4)
    subprocess.run(
        [
            "magick", "-size", f"{width}x{height}", "xc:white",
            "-fill", "red", "-draw", "rectangle 0,0 40,40",
            "-colorspace", "CMYK", "-units", "PixelsPerInch", "-density", str(dpi), str(path),
        ],
        check=True,
    )


def _red_corner(page: pymupdf.Page) -> str:
    pixmap = page.get_pixmap(dpi=30)
    width, height = pixmap.width, pixmap.height
    corners = {
        "top-left": (1, 1), "top-right": (width - 2, 1),
        "bottom-left": (1, height - 2), "bottom-right": (width - 2, height - 2),
    }
    reds = [name for name, (x, y) in corners.items() if pixmap.pixel(x, y)[0] > 200 and pixmap.pixel(x, y)[1] < 80]
    assert len(reds) == 1, reds
    return reds[0]


@needs_magick
def test_raster_face_is_rotated_clockwise_into_the_final_pdf(tmp_path):
    stem = "30_KS(K)_(50x30)_4-4_(22044-26288404)_1"
    _marked_jpeg(tmp_path / f"{stem}-face.jpg", 34, 54)  # вертикальное лицо
    _marked_jpeg(tmp_path / f"{stem}-back.jpg", 54, 34)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    order = processor.inspect_orders()[0]
    assert order.passed, (order.errors, [item.errors for item in order.files])
    assert order.side_alignment["side"] == "face" and order.side_alignment["degrees"] == 90

    (_order_check, pdf_path, error), = processor.create_pdfs([order])
    assert error is None, error
    with pymupdf.open(pdf_path) as document:
        sizes = [(page.rect.width / MM, page.rect.height / MM) for page in document]
        assert all(width > height for width, height in sizes), sizes
        assert all(page.rotation == 0 for page in document)
        # По часовой: левый верхний угол лица стал правым верхним; оборот не тронут.
        assert _red_corner(document[0]) == "top-right"
        assert _red_corner(document[1]) == "top-left"
        # Без ресэмпла: те же пиксели, только переставлены (DPI 300 сохранён).
        image = document.extract_image(document[0].get_images()[0][0])
        assert (image["width"], image["height"]) == (638, 402)
    face = next(item for item in order.files if item.parsed.side == "face")
    assert {"kind": "side_rotation", "degrees": 90, "side": "face", "method": "imagemagick"} in face.processing_actions


@needs_magick
def test_back_rotated_counterclockwise_when_face_matches_the_name(tmp_path):
    stem = "30_KS(K)_(50x30)_4-4_(22044-26288405)_1"
    _marked_jpeg(tmp_path / f"{stem}-face.jpg", 54, 34)
    _marked_jpeg(tmp_path / f"{stem}-back.jpg", 34, 54)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    order = processor.inspect_orders()[0]
    (_order_check, pdf_path, error), = processor.create_pdfs([order])
    assert error is None, error
    with pymupdf.open(pdf_path) as document:
        assert _red_corner(document[0]) == "top-left"
        # Против часовой: левый верхний угол оборота стал левым нижним.
        assert _red_corner(document[1]) == "bottom-left"


# --------------------------------------------------------------------------- PDF через callas


class FakeRotateClient:
    """Имитирует ``CallasClient.process`` с QuickFix ``rotate_pages`` (как pdfToolbox 17:
    поворот от отображаемой страницы, 90 — по часовой, результат без /Rotate)."""

    def __init__(self, root: Path, *, broken: bool = False) -> None:
        self.root = root
        self.broken = broken
        self.configs: list[dict] = []

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def delete(self, job_id: str) -> None:
        pass

    def process(self, source, profile, mode, *, variables, job_id, **_kwargs):
        config = json.loads(variables["quickfix_config"])
        self.configs.append(config)
        output = self.job_dir(job_id) / "out" / "fixed.pdf"
        output.parent.mkdir(parents=True, exist_ok=True)
        if self.broken:
            shutil.copyfile(source, output)
            return SimpleNamespace(output_pdf=output)
        angles: dict[int, int] = {}
        with pymupdf.open(source) as src:
            for instruction in config["quickfixes"][0]["instructions"]:
                selector = instruction["page_selector"]
                pages = range(1, src.page_count + 1) if selector == "all" else map(int, selector.split(","))
                for page in pages:
                    angles[page] = instruction["rotate_by"]
            result = pymupdf.open()
            for index, page in enumerate(src, start=1):
                rect = page.rect  # отображаемый размер (с /Rotate)
                angle = angles.get(index, 0)
                width, height = (rect.height, rect.width) if angle else (rect.width, rect.height)
                new = result.new_page(width=width, height=height)
                new.show_pdf_page(new.rect, src, index - 1, rotate=-angle)
                bleed = 2 * MM
                new.set_trimbox(pymupdf.Rect(bleed, bleed, width - bleed, height - bleed))
            result.save(output)
            result.close()
        return SimpleNamespace(output_pdf=output)


def _marked_pdf(path: Path, pages: list[tuple[float, float]]) -> None:
    document = pymupdf.open()
    for width_mm, height_mm in pages:
        page = document.new_page(width=width_mm * MM, height=height_mm * MM)
        page.draw_rect(pymupdf.Rect(0, 0, 30, 30), color=(1, 0, 0), fill=(1, 0, 0))
        page.insert_text((40, 60), "artwork", fontsize=10)
        bleed = 2 * MM
        page.set_trimbox(pymupdf.Rect(bleed, bleed, page.rect.width - bleed, page.rect.height - bleed))
    document.save(path)
    document.close()


def test_rotate_quickfix_json():
    assert build_rotate_quickfix({2: -90}, 2) == {
        "quickfixes": [{
            "quickfix": "rotate_pages", "version": "1.0",
            "instructions": [{"page_selector": "2", "rotate_by": -90}],
        }],
    }
    with pytest.raises(ValueError):
        build_rotate_quickfix({1: 180}, 1)


def test_rotator_rejects_result_that_did_not_rotate(tmp_path):
    source = tmp_path / "face.pdf"
    _marked_pdf(source, [(54, 34)])
    rotator = CallasPdfRotator(FakeRotateClient(tmp_path / "jobs", broken=True))
    with pytest.raises(PdfRotateError, match="не повернул страницу 1"):
        rotator(source, tmp_path / "rotated.pdf", {1: 90}, "face")
    assert not (tmp_path / "rotated.pdf").exists()


def test_two_page_pdf_gets_only_its_back_rotated(tmp_path):
    name = "30_KS(K)_(50x30)_4-4_(22044-26288406)_1.pdf"
    _marked_pdf(tmp_path / name, [(54, 34), (34, 54)])
    client = FakeRotateClient(tmp_path / "jobs")
    processor = BatchProcessor(
        tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger(), pdf_rotator=CallasPdfRotator(client)
    )
    order = processor.inspect_orders()[0]
    assert order.passed, order.errors
    assert order.side_alignment["side"] == "back" and order.side_alignment["page"] == 2
    (_order_check, pdf_path, error), = processor.create_pdfs([order])
    assert error is None, error
    assert client.configs[0]["quickfixes"][0]["instructions"] == [{"page_selector": "2", "rotate_by": -90}]
    with pymupdf.open(pdf_path) as document:
        assert [page.rotation for page in document] == [0, 0]
        assert all(page.rect.width > page.rect.height for page in document)
        assert _red_corner(document[0]) == "top-left"
        assert _red_corner(document[1]) == "bottom-left"
    item = order.files[0]
    assert {"kind": "side_rotation", "degrees": -90, "page": 2, "side": "back", "method": "callas"} in item.processing_actions
    # Исходник не изменён.
    with pymupdf.open(tmp_path / name) as source:
        assert source[1].rect.width < source[1].rect.height


# --------------------------------------------------------------------------- статус, DTO, печать, журнал


def _rotated_order() -> OrderCheck:
    face, back = _file("face", (113, 156)), _file("back", (156, 113))
    order = _validate(_order(face, back))
    face.processing_actions.append({"kind": "side_rotation", "degrees": 90, "side": "face", "method": "imagemagick"})
    return order


GREEN_PITSTOP = {"execution_status": "completed", "verdict": "passed", "checked_revision": 1, "issues": []}


def test_rotated_order_is_red_even_with_green_pitstop():
    order = _rotated_order()
    artifacts = OrderArtifacts(pdf_kind="production", pitstop=GREEN_PITSTOP)
    assert completed_order_status(order, artifacts) == "error"


def test_dto_print_confirmation_and_journal(tmp_path):
    order = _rotated_order()
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    dto = order_check_to_dto(
        order, status="error", pdf_path=str(pdf), pitstop=GREEN_PITSTOP, current_pdf_revision=1
    )
    dto["pdf_kind"] = "production"
    message = side_rotation_message(order.side_alignment)
    assert dto["errors"] == [message]
    assert dto["side_alignment"]["side"] == "face"
    face_dto = next(item for item in dto["files"] if item["parsed"]["side"] == "face")
    assert face_dto["side_rotation_degrees"] == 90
    assert any(group["code"] == "side_rotated" for group in dto["issue_groups"])

    blockers = print_blockers(dto)
    assert print_mode(blockers) == "forced"
    assert [item["code"] for item in blockers] == ["forced_print_confirmation"]
    assert "повёрнута на +90°" in blockers[0]["message"]

    codes = {entry["code"]: entry for entry in build_order_journal(dto)["entries"]}
    assert codes["local:side_rotated"]["severity"] == "error"
    assert codes["auto:side_rotated"]["data"]["degrees"] == 90


def test_return_suggestion_for_rotated_side():
    from services.return_suggestions import suggest_return_reasons
    from tests.test_return_suggestions import TEMPLATE

    if not TEMPLATE.is_file():
        pytest.skip("нет справочника причин возврата")
    from core.return_reasons import load_return_reasons

    dto = order_check_to_dto(_rotated_order(), status="error")
    items = suggest_return_reasons(dto, load_return_reasons(TEMPLATE))
    assert [item["signal"] for item in items] == ["side_orientation"]
    assert items[0]["preselect"] is False


# --------------------------------------------------------------------------- новая ревизия PDF


def _recheck(tmp_path: Path, **kwargs) -> dict:
    from services.order_workflow import OrderWorkflowService

    captured: dict = {}

    def apply_pdf_revision(*_args, **values):
        captured.update(values)
        return {}

    service = OrderWorkflowService(SimpleNamespace(apply_pdf_revision=apply_pdf_revision), None, None)
    candidate = tmp_path / "candidate.pdf"
    _marked_pdf(candidate, [(54, 34), (54, 34)])
    alignment = {"side": "face", "degrees": 90}
    order = {
        "order_id": "26288404", "aggregate_id": "22044:26288404", "files": [],
        "side_alignment": alignment, "errors": [side_rotation_message(alignment)],
    }
    run = {"id": "run-1", "options": {"input_path": str(tmp_path), "direction": "digital"}}
    service.recheck_pdf_revision(run, order, candidate, **kwargs)
    return captured


def test_callas_fix_of_rotated_pdf_keeps_the_order_red(tmp_path):
    captured = _recheck(tmp_path, callas_fix=True)
    assert captured["status"] == "error"
    assert "clear_side_alignment" not in captured


def test_manual_pdf_replacement_drops_the_rotation(tmp_path):
    captured = _recheck(tmp_path)
    assert captured["status"] != "error"
    assert captured["clear_side_alignment"] is True


def test_apply_pdf_revision_clears_rotation_error():
    from services.coordinator import RunCoordinator
    from services.repository import InMemoryRunRepository

    alignment = {"side": "face", "degrees": 90}
    message = side_rotation_message(alignment)
    repository = InMemoryRunRepository()
    coordinator = RunCoordinator(repository, autostart=False)
    repository.create_run({
        "id": "run-1", "status": "completed", "stage": "completed", "progress": 100,
        "total_orders": 1, "processed_orders": 0,
        "options": {"input_path": "/tmp", "direction": "digital"},
        "orders": {"1001": {"order_id": "1001", "status": "error", "files": [],
                            "errors": [message, "другая ошибка"], "side_alignment": alignment,
                            "pdf_path": "/tmp/a.pdf", "current_pdf_revision": 1}},
    })
    coordinator.apply_pdf_revision(
        "run-1", "1001", status="passed", pdf_path="/tmp/a.pdf", pitstop=None,
        current_pdf_revision=2, current_pdf_sha256="a", clear_side_alignment=True,
    )
    stored = repository.get_run("run-1")["orders"]["1001"]
    assert "side_alignment" not in stored
    assert stored["errors"] == ["другая ошибка"]


@needs_magick
def test_adapter_routes_rotated_order_to_failed_with_a_production_pdf(tmp_path):
    from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions

    stem = "30_KS(K)_(50x30)_4-4_(22044-26288407)_1"
    _marked_jpeg(tmp_path / f"{stem}-face.jpg", 34, 54)
    _marked_jpeg(tmp_path / f"{stem}-back.jpg", 54, 34)
    adapter = BatchProcessorAdapter(
        ProcessingOptions(
            input_path=str(tmp_path), direction="digital", approve_corrections=True,
            generate_previews=False, copy_failures=False,
        ),
        processor_factory=lambda *a, **k: BatchProcessor(*a, pdf_merger=FakeCallasMerger(), **k),
    )
    order = adapter.scan_and_inspect()[0]
    artifacts = adapter.process_order(order)
    assert artifacts.errors == [] and artifacts.pdf_kind == "production"
    assert artifacts.pdf_path.parent.name == "Failed"
    assert completed_order_status(order, artifacts) == "error"


def test_rejected_correction_turns_rotation_back_into_the_old_error():
    from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path="/nonexistent", direction="digital"),
        processor_factory=lambda *a, **k: BatchProcessor(*a, **k),
    )
    face = _file("face", (113, 156))
    back = _file("back", (160, 113), resample_target_mm=(156.0, 113.0), resample_decision="ask_confirmation")
    order = _order(face, back)
    adapter.processor._validate_order(order)
    assert order.side_alignment is not None
    adapter.decide(order, approved=False)
    assert order.side_alignment is None
    assert any(error.startswith(ORIENTATION_ERROR) for error in order.errors)
