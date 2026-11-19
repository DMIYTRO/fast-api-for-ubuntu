"""Зелёный PDF ждёт решения оператора в ``PDF/Checked``; в ``PDF/Print`` — только по «в печать».

Заказ 26237201 (2026-11-19): проверка прошла без замечаний и сразу положила PDF в
``PDF/Print``, за 18 секунд до нажатия «в печать».
"""

from __future__ import annotations

from pathlib import Path

from processing.models import FileCheck, OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.file_lifecycle import FileLifecycle
from services.pdf_routing import pdf_root_for, route_pdf

NAME = "22_KS(P)_(148x105)_4-4_(12287-26237201)_curve4-face"


def _order(root: Path, pdf: Path) -> dict:
    source = root / f"{NAME}.jpg"
    source.write_bytes(b"face")
    preview = root / "Previews" / f"{NAME}_preview.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    preview.write_bytes(b"png")
    return {
        "order_id": "26237201",
        "status": "passed",
        "files": [{"path": str(source)}],
        "pdf_path": str(pdf),
        "preview_paths": [str(preview)],
    }


def test_green_check_waits_in_checked_and_print_button_moves_it(tmp_path):
    built = tmp_path / "PDF" / f"{NAME}.pdf"
    built.parent.mkdir()
    built.write_bytes(b"%PDF-green")

    routed = route_pdf(built, "passed")

    assert routed == tmp_path / "PDF" / "Checked" / f"{NAME}.pdf"
    assert not (tmp_path / "PDF" / "Print").exists()
    assert pdf_root_for(routed) == tmp_path / "PDF"

    transition = FileLifecycle(tmp_path).accept_for_print(_order(tmp_path, routed))

    printed = tmp_path / "PDF" / "Print" / f"{NAME}.pdf"
    assert transition.pdf_path == str(printed)
    assert printed.read_bytes() == b"%PDF-green"
    assert not routed.exists()


def test_order_checked_before_the_change_is_still_printable_from_print(tmp_path):
    legacy = tmp_path / "PDF" / "Print" / f"{NAME}.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"%PDF-old")

    transition = FileLifecycle(tmp_path).accept_for_print(_order(tmp_path, legacy))

    assert transition.pdf_path == str(legacy)
    assert legacy.read_bytes() == b"%PDF-old"


def test_recheck_removes_old_copy_from_print_and_checked(tmp_path):
    pdf_dir = tmp_path / "PDF"
    for folder in ("Print", "Checked"):
        (pdf_dir / folder).mkdir(parents=True)
        (pdf_dir / folder / f"{NAME}.pdf").write_bytes(b"stale")
    candidate = pdf_dir / f"{NAME}.pdf"
    candidate.write_bytes(b"%PDF-new")

    routed = route_pdf(candidate, "warning")

    assert routed == pdf_dir / "Rework" / f"{NAME}.pdf"
    assert sorted(path.relative_to(pdf_dir).as_posix() for path in pdf_dir.rglob("*.pdf")) == [
        f"Rework/{NAME}.pdf"
    ]


def test_failed_recheck_quarantines_previous_green_pdf_from_checked(tmp_path):
    previous = tmp_path / "PDF" / "Checked" / f"{NAME}.pdf"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"%PDF-previous")
    adapter = BatchProcessorAdapter(ProcessingOptions(input_path=str(tmp_path), direction="digital"))
    order = OrderCheck("26237201", "12287", files=[FileCheck(path=tmp_path / f"{NAME}.jpg")])

    adapter._quarantine_previous_print(order)

    assert not previous.exists()
    assert (tmp_path / "PDF" / "Failed" / f"{NAME}.pdf").read_bytes() == b"%PDF-previous"


def test_stale_copy_vanishing_during_route_is_ignored(tmp_path, monkeypatch):
    """Заказ 26237531: копию в Print забрали между проверкой и переносом (CIFS cache=loose)."""
    pdf_dir = tmp_path / "PDF"
    stale = pdf_dir / "Print" / f"{NAME}.pdf"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"%PDF-printed")
    candidate = pdf_dir / f"{NAME}.pdf"
    candidate.write_bytes(b"%PDF-new")
    real_rename = Path.rename

    def rename(self, target):
        if self == stale:
            self.unlink()  # файл исчез в тот же миг
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)

    routed = route_pdf(candidate, "warning")

    assert routed == pdf_dir / "Rework" / f"{NAME}.pdf"
    assert routed.read_bytes() == b"%PDF-new"


def test_missing_source_still_fails_route(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        route_pdf(tmp_path / "PDF" / "absent.pdf", "passed")


def test_quarantine_ignores_a_copy_that_vanished(tmp_path, monkeypatch):
    previous = tmp_path / "PDF" / "Print" / f"{NAME}.pdf"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"%PDF-previous")
    real_replace = Path.replace

    def replace(self, target):
        if self == previous:
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    adapter = BatchProcessorAdapter(ProcessingOptions(input_path=str(tmp_path), direction="digital"))
    order = OrderCheck("26237531", "36056", files=[FileCheck(path=tmp_path / f"{NAME}.jpg")])

    adapter._quarantine_previous_print(order)  # не падает
