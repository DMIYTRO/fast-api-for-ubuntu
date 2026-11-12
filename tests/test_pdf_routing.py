"""Раскладка собранных PDF по папкам статуса: PDF/Print, PDF/Rework, PDF/Failed."""
from __future__ import annotations

from pathlib import Path

import pytest

from services import pdf_routing
from services.callas_fix import fix_dir_for
from services.pdf_routing import (
    completed_status,
    folder_for_status,
    pdf_root_for,
    route_pdf,
)


def _pdf_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "PDF"
    directory.mkdir()
    return directory


def test_status_to_folder_mapping_is_fixed():
    assert folder_for_status("passed") == "Print"
    assert folder_for_status("warning") == "Rework"
    assert folder_for_status("error") == "Failed"
    with pytest.raises(ValueError):
        folder_for_status("accepted_for_print")


def test_route_moves_pdf_into_status_folder_and_removes_stale_copies(tmp_path: Path):
    pdf_dir = _pdf_dir(tmp_path)
    source = pdf_dir / "1001.pdf"
    source.write_bytes(b"new")
    for folder in ("Print", "Failed"):
        (pdf_dir / folder).mkdir()
        (pdf_dir / folder / "1001.pdf").write_bytes(b"stale")

    routed = route_pdf(source, "warning")

    assert routed == pdf_dir / "Rework" / "1001.pdf"
    assert routed.read_bytes() == b"new"
    assert not source.exists()
    assert not (pdf_dir / "Print" / "1001.pdf").exists()
    assert not (pdf_dir / "Failed" / "1001.pdf").exists()
    assert list(pdf_dir.glob("**/.*.tmp.pdf")) == []


def test_route_same_location_is_a_no_op(tmp_path: Path):
    pdf_dir = _pdf_dir(tmp_path)
    pdf = pdf_dir / "Print" / "1001.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"ready")

    assert route_pdf(pdf, "passed") == pdf
    assert pdf.read_bytes() == b"ready"


def test_corrected_pdf_takes_over_production_name_and_folder(tmp_path: Path):
    pdf_dir = _pdf_dir(tmp_path)
    production = pdf_dir / "Rework" / "1001.pdf"
    production.parent.mkdir()
    production.write_bytes(b"old")
    fixed = pdf_dir / "Rework" / "1001_callas-fixed.pdf"
    fixed.write_bytes(b"fixed")

    routed = route_pdf(fixed, "passed", pdf_dir=pdf_root_for(production), name=production.name)

    assert routed == pdf_dir / "Print" / "1001.pdf"
    assert routed.read_bytes() == b"fixed"
    assert not production.exists()
    assert not fixed.exists()


def test_failed_save_keeps_previous_production_pdf_and_leaves_no_temp(tmp_path: Path, monkeypatch):
    pdf_dir = _pdf_dir(tmp_path)
    production = pdf_dir / "Rework" / "1001.pdf"
    production.parent.mkdir()
    production.write_bytes(b"old")
    fixed = pdf_dir / "Rework" / "1001_callas-fixed.pdf"
    fixed.write_bytes(b"fixed")

    def broken_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(pdf_routing.os, "replace", broken_replace)
    with pytest.raises(OSError):
        route_pdf(fixed, "passed", pdf_dir=pdf_dir, name=production.name)

    assert production.read_bytes() == b"old"
    assert fixed.read_bytes() == b"fixed"
    assert not (pdf_dir / "Print").exists() or list((pdf_dir / "Print").iterdir()) == []
    assert list(pdf_dir.glob("**/.*.tmp.pdf")) == []


def test_pdf_root_for_handles_status_folders_and_legacy_root(tmp_path: Path):
    assert pdf_root_for(tmp_path / "PDF" / "Print" / "a.pdf") == tmp_path / "PDF"
    assert pdf_root_for(tmp_path / "PDF" / "a.pdf") == tmp_path / "PDF"


def test_local_error_is_never_lifted_by_pitstop():
    passed_report = {"execution_status": "completed", "verdict": "passed"}
    assert completed_status(local_errors=True, local_warnings=False, pitstop=passed_report) == "error"
    assert completed_status(local_errors=False, local_warnings=False, pitstop=passed_report) == "passed"
    assert completed_status(local_errors=False, local_warnings=True, pitstop=passed_report) == "warning"
    assert completed_status(
        local_errors=False, local_warnings=False,
        pitstop={"execution_status": "completed", "verdict": "warning"},
    ) == "warning"
    assert completed_status(
        local_errors=False, local_warnings=False,
        pitstop={"execution_status": "failed", "verdict": "error"},
    ) == "error"


def test_callas_fix_dir_is_shared_across_status_folders(tmp_path: Path):
    expected = tmp_path / "PDF" / "CallasFix" / "1001"
    assert fix_dir_for(tmp_path / "PDF" / "1001.pdf") == expected
    for folder in ("Print", "Rework", "Failed"):
        assert fix_dir_for(tmp_path / "PDF" / folder / "1001.pdf") == expected
    assert fix_dir_for(tmp_path / "1001-face.pdf") == tmp_path / "PDF" / "CallasFix" / "1001-face.source"


def test_readable_pdf_with_pitstop_errors_requires_rework():
    assert completed_status(local_errors=False, local_warnings=False, pitstop={"execution_status": "completed", "verdict": "error"}) == "warning"


def test_repeated_corrections_replace_in_place_and_remove_previous_status(tmp_path):
    root = _pdf_dir(tmp_path)
    pdf = root / "job.pdf"
    pdf.write_bytes(b"initial")
    current = route_pdf(pdf, "passed")
    for number, status in enumerate(["warning", "warning", "passed", "error", "passed"]):
        candidate = root / "candidate.pdf"
        candidate.write_bytes(f"revision-{number}".encode())
        current = route_pdf(candidate, status, pdf_dir=root, name="job.pdf")
        assert current.read_bytes() == f"revision-{number}".encode()
        assert list(root.glob("*/job.pdf")) == [current]
        if status != "passed":
            assert not (root / "Print" / "job.pdf").exists()


def test_failed_downgrade_keeps_last_committed_green_revision(tmp_path, monkeypatch):
    root = _pdf_dir(tmp_path)
    green = root / "Print" / "job.pdf"
    green.parent.mkdir()
    green.write_bytes(b"checked-green")
    candidate = root / "candidate.pdf"
    candidate.write_bytes(b"unchecked-new")
    monkeypatch.setattr(pdf_routing.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        route_pdf(candidate, "warning", pdf_dir=root, name="job.pdf")
    assert green.read_bytes() == b"checked-green"
    assert not list(root.glob("Rework/*.pdf"))
    assert not list(root.glob("**/.*.tmp.pdf"))
