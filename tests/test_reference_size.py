"""Эталонный размер по метке в имени: (SRA3) — 314×434, (SRA3P) — 324×471 мм.

Норма уже с вылетами: вылеты к эталону не прибавляются, размер из имени не используется,
ориентация любая. Файл с такой меткой к тому же не масштабируется (``canvas``).
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from config.profiles import DEFAULT_PROFILE
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, ParsedFilename
from services.dto import file_check_to_dto

SRA3 = "06_NP(K)_(SRA3)_Glam10_450_mel_(434x314)_4-0_L5_T1_(3773-26162382)_1-face.tif"
SRA3P = "06_NP(SRA3P)_(PPP)_Bezlam_300_mel_Planshet_(471x324)_4-0_L7_T7_(9543-26161972)_contour1-face.tif"
PLAIN = "06_NP(K)_Glam10_450_mel_(434x314)_4-0_L5_T1_(3773-26162382)_1-face.tif"
PT = 72 / 25.4


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (SRA3, (314.0, 434.0)),
        (SRA3.lower(), (314.0, 434.0)),
        (SRA3P, (324.0, 471.0)),
        ("x_(SRA3)_(SRA3P)_(10x10).pdf", (324.0, 471.0)),  # длинная метка точнее
        (PLAIN, None),
        ("x_(BAN)_(666x302).pdf", None),
        ("x_SRA3_(434x314).pdf", None),
    ],
)
def test_reference_size_by_marker(name, expected):
    assert DEFAULT_PROFILE.reference_size_mm(name) == expected


def test_expected_size_uses_reference_without_adding_bleed():
    assert DEFAULT_PROFILE.expected_size_mm(SRA3, 434, 314) == (314.0, 434.0)
    assert DEFAULT_PROFILE.expected_size_mm(SRA3P, 471, 324) == (324.0, 471.0)
    # Эталон важнее размера из имени.
    assert DEFAULT_PROFILE.expected_size_mm("a_(SRA3)_(450x320).tif", 450, 320) == (314.0, 434.0)
    assert DEFAULT_PROFILE.expected_size_mm(PLAIN, 434, 314) == (438.0, 318.0)
    assert DEFAULT_PROFILE.expected_size_mm(PLAIN, 434, 314, 0.0) == (434.0, 314.0)


def _raster(name: str, width: float, height: float) -> FileCheck:
    return FileCheck(
        path=Path("/in") / name,
        parsed=ParsedFilename("3773", "26162382", 434.0, 314.0, 4, 0, "face"),
        actual_width_mm=width,
        actual_height_mm=height,
        dpi=300.0,
        dpi_x=300.0,
        dpi_y=300.0,
        colorspace="CMYK",
    )


def _validate(item: FileCheck, policy: str = "auto") -> FileCheck:
    BatchProcessor(Path("/in"), Path("/in/PDF"), correction_policy=policy)._validate_raster_size(item)
    return item


@pytest.mark.parametrize(("width", "height"), [(434.0, 314.0), (314.0, 434.0)])
def test_sra3_raster_at_reference_size_is_accepted(width, height):
    item = _validate(_raster(SRA3, width, height))
    assert item.resample_decision == "accept"
    assert not item.needs_resample and item.size_fix is None and item.errors == []
    assert item.expected_mm == (314.0, 434.0)


def test_plain_raster_with_same_size_still_needs_bleed():
    item = _validate(_raster(PLAIN, 434.0, 314.0))
    assert item.expected_mm == (438.0, 318.0)
    assert item.resample_decision != "accept"


def test_sra3_raster_off_reference_gets_white_margins_not_scaling():
    item = _validate(_raster(SRA3, 432.0, 312.0))
    assert item.resample_decision == "auto_correct" and item.needs_resample
    assert item.resample_target_mm == (434.0, 314.0)
    assert item.size_fix["method"] == "canvas"
    assert "белые поля 2.00x2.00 мм" in item.size_fix["detail"]


def test_sra3p_raster_larger_than_reference_is_cropped():
    item = BatchProcessor(Path("/in"), Path("/in/PDF"), correction_policy="auto")
    check = _raster(SRA3P, 473.0, 324.0)
    check.parsed = ParsedFilename("9543", "26161972", 471.0, 324.0, 4, 0, "face")
    item._validate_raster_size(check)
    assert check.resample_target_mm == (471.0, 324.0)
    assert check.size_fix["method"] == "canvas"
    assert "обрезка 2.00x0.00 мм" in check.size_fix["detail"]


def test_dto_carries_expected_size():
    item = _validate(_raster(SRA3, 434.0, 314.0))
    assert file_check_to_dto(item)["expected_mm"] == [314.0, 434.0]


def _pdf(path: Path, width_mm: float, height_mm: float) -> None:
    document = pymupdf.open()
    document.new_page(width=width_mm * PT, height=height_mm * PT)
    document.save(str(path))
    document.close()


def _inspect_pdf(root: Path, name: str, size) -> FileCheck:
    folder = root / name.replace(".pdf", "")
    folder.mkdir()
    _pdf(folder / name, *size)
    processor = BatchProcessor(folder, folder / "output", pdf_resizer=lambda *a, **k: None)
    return processor.inspect_orders()[0].files[0]


def test_sra3_pdf_at_reference_size_passes(tmp_path: Path):
    item = _inspect_pdf(tmp_path, SRA3.replace(".tif", ".pdf"), (434.0, 314.0))
    assert item.expected_mm == (314.0, 434.0)
    assert item.errors == [] and not item.needs_resample and item.size_fix is None


def test_sra3p_pdf_smaller_than_reference_gets_canvas_to_reference(tmp_path: Path):
    item = _inspect_pdf(tmp_path, SRA3P.replace(".tif", ".pdf"), (469.0, 322.0))
    assert item.needs_resample
    plan = item.pdf_resize_plans[1]
    assert plan.method == "canvas" and tuple(sorted(plan.target_mm)) == (324.0, 471.0)


def test_manual_pdf_revision_uses_reference_size(tmp_path: Path):
    from services.pdf_revision_validation import validate_pdf_revision

    pdf = tmp_path / "revision.pdf"
    _pdf(pdf, 434.0, 314.0)
    parsed = {"width_mm": 434.0, "height_mm": 314.0, "side": "face"}
    sra3 = {"files": [{"name": SRA3.replace(".tif", ".pdf"), "parsed": parsed, "page_count": 1}]}
    plain = {"files": [{"name": PLAIN.replace(".tif", ".pdf"), "parsed": parsed, "page_count": 1}]}
    assert not any("размер" in error for error in validate_pdf_revision(pdf, sra3, "digital").errors)
    assert any("размер" in error for error in validate_pdf_revision(pdf, plain, "digital").errors)
