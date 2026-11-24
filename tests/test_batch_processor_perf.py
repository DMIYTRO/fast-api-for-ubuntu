"""Per-file / per-run cost of BatchProcessor: inspection launches, scan, Troubles copies."""
from __future__ import annotations

import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from core import inspector
from processing import batch_processor
from processing.batch_processor import (
    REJECTED_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    BatchProcessor,
)
from processing.models import FileCheck, OrderCheck, ParsedFilename

MAGICK = shutil.which("magick")


def _magick(*args) -> None:
    subprocess.run([MAGICK, *map(str, args)], check=True, capture_output=True)


# --- inspection --------------------------------------------------------------

@pytest.mark.skipif(not MAGICK, reason="requires ImageMagick")
def test_inspect_orders_uses_one_launch_per_plain_file_and_still_flags_alpha(tmp_path, monkeypatch):
    rgb = tmp_path / "job_(90x50)_4-4_(1-1)_face.tif"
    rgba = tmp_path / "job_(90x50)_4-4_(1-1)_back.tif"
    jpg = tmp_path / "job_(90x50)_4-0_(1-2)_face.jpg"
    _magick("-size", "1110x638", "xc:red", "-units", "PixelsPerInch", "-density", "300", rgb)
    _magick("-size", "1110x638", "xc:red", "-alpha", "set", "-channel", "A", "-evaluate", "set", "50%",
            "+channel", "-units", "PixelsPerInch", "-density", "300", rgba)
    _magick("-size", "1110x638", "xc:red", "-units", "PixelsPerInch", "-density", "300", jpg)

    commands: list[list[str]] = []
    real = inspector.run_command

    def counting(command, **kwargs):
        commands.append(list(command))
        return real(command, **kwargs)

    monkeypatch.setattr(inspector, "run_command", counting)
    orders = {order.order_id: order for order in BatchProcessor(tmp_path, tmp_path / "PDF").inspect_orders()}

    by_name = {item.path.name: item for order in orders.values() for item in order.files}
    assert by_name[jpg.name].errors == []
    assert by_name[jpg.name].width_px == 1110 and by_name[jpg.name].dpi == 300.0
    assert by_name[jpg.name].colorspace == "sRGB"
    assert not by_name[rgb.name].has_alpha
    assert by_name[rgba.name].has_alpha
    assert by_name[rgba.name].errors == []
    assert any("несведённой прозрачностью" in warning for warning in by_name[rgba.name].warnings)
    assert by_name[rgba.name].channels == "srgba 4.0"
    # rgb.tif: 1, jpg: 1, rgba.tif: 2 (alpha needs a full decode) instead of 3 + 2 + 3.
    assert len(commands) == 4


# --- scan --------------------------------------------------------------------

def test_scan_matches_iterdir_semantics(tmp_path):
    names = [
        "b.jpg", "a.JPEG", "c.Tif", "d.tiff", "e.png", "f.pdf", "g.psd", "h.BMP", "i.heic", "j.heif",
        "k.txt", "noext", ".jpg", "m.jpg.bak", "ж_(90x50)_4-0_(1-1)_face.jpg",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "dir.jpg").mkdir()  # a directory with a supported suffix is not a file
    (tmp_path / "link.png").symlink_to(tmp_path / "b.jpg")
    (tmp_path / "broken.png").symlink_to(tmp_path / "missing")

    expected = sorted(
        path for path in tmp_path.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS | REJECTED_EXTENSIONS
    )
    found = BatchProcessor(tmp_path, tmp_path / "PDF").scan()
    assert found == expected
    assert [p.name for p in found] == sorted(p.name for p in found)
    assert (tmp_path / "dir.jpg") not in found and (tmp_path / "broken.png") not in found
    assert tmp_path / "link.png" in found


def test_scan_does_not_stat_every_entry(tmp_path, monkeypatch):
    for index in range(20):
        (tmp_path / f"f{index}.txt").write_bytes(b"x")
    (tmp_path / "a.jpg").write_bytes(b"x")
    calls = []
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self, *a, **k: calls.append(self) or original(self, *a, **k))
    assert [p.name for p in BatchProcessor(tmp_path, tmp_path / "PDF").scan()] == ["a.jpg"]
    assert calls == []


# --- Troubles ------------------------------------------------------------------

def _failed_order(root: Path, number: int) -> OrderCheck:
    files = []
    for side in ("face", "back"):
        path = root / f"job_(90x50)_4-4_(1-{number})_{side}.jpg"
        path.write_bytes(f"image {number} {side}".encode())
        check = FileCheck(path=path, parsed=ParsedFilename("1", str(number), 90, 50, 4, 4, side))
        check.errors.append(f"низкое разрешение {number}")
        files.append(check)
    order = OrderCheck(order_id=str(number), customer_id="1", files=files)
    order.errors.append(f"ошибка заказа {number}")
    return order


def _junk(root: Path, count: int) -> list[FileCheck]:
    items = []
    for index in range(count):
        path = root / f"junk_{index}.jpg"
        path.write_bytes(f"junk {index}".encode())
        check = FileCheck(path=path)
        check.errors.append(f"не распознано {index}")
        items.append(check)
    return items


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _processor_with_junk(root: Path, unparsed: int, unsupported: int) -> BatchProcessor:
    processor = BatchProcessor(root, root / "PDF")
    processor.unparsed = _junk(root, unparsed)
    processor.unsupported = []
    for index in range(unsupported):
        path = root / f"image_{index}.psd"
        path.write_bytes(f"psd {index}".encode())
        check = FileCheck(path=path)
        check.errors.append("формат PSD исключён из обработки")
        processor.unsupported.append(check)
    return processor


def test_unparsed_files_are_copied_once_however_many_orders_fail(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    orders = [_failed_order(source, number) for number in range(1, 31)]
    processor = _processor_with_junk(source, unparsed=40, unsupported=5)
    troubles = tmp_path / "Troubles"

    copies: list[Path] = []
    real_copy2 = shutil.copy2
    lock = threading.Lock()

    def counting_copy2(src, dst, *args, **kwargs):
        with lock:
            copies.append(Path(dst))
        return real_copy2(src, dst, *args, **kwargs)

    with patch.object(batch_processor.shutil, "copy2", counting_copy2), ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda order: processor.copy_failed_to_troubles([order], troubles), orders))

    # 30 orders x 2 files + 45 junk files, each exactly once (was 30 x 45 extra copies).
    assert len(copies) == 60 + 45
    assert len(set(copies)) == len(copies)
    assert all(error is None for per_order in results for _s, _t, error in per_order)
    assert sum(len(per_order) for per_order in results) == 105

    # Same final folder as the legacy single call with every failed order.
    reference_root = tmp_path / "ref"
    reference_root.mkdir()
    reference = _processor_with_junk(source, unparsed=40, unsupported=5)
    reference.copy_failed_to_troubles(orders, reference_root)
    assert _tree(troubles) == _tree(reference_root)
    assert len(_tree(troubles)) == (60 + 45) * 2  # file + .error.txt


def test_a_single_call_with_all_orders_keeps_legacy_result_order(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    orders = [_failed_order(source, n) for n in (1, 2)]
    processor = _processor_with_junk(source, unparsed=2, unsupported=1)
    results = processor.copy_failed_to_troubles(orders, tmp_path / "Troubles")
    groups = [target.parent.name for _s, target, _e in results]
    assert groups == ["1", "1", "2", "2", "UNPARSED", "UNPARSED", "UNSUPPORTED"]


def test_extras_are_copied_again_for_another_target_and_after_a_rescan(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    order = _failed_order(source, 1)
    processor = _processor_with_junk(source, unparsed=2, unsupported=0)
    first, second = tmp_path / "T1", tmp_path / "T2"
    processor.copy_failed_to_troubles([order], first)
    processor.copy_failed_to_troubles([order], second)
    assert len(list((first / "UNPARSED").glob("*.jpg"))) == len(list((second / "UNPARSED").glob("*.jpg"))) == 2

    processor.copy_failed_to_troubles([order], first)  # same run, same target: no extras again
    shutil.rmtree(first / "UNPARSED")
    processor.copy_failed_to_troubles([order], first)
    assert not (first / "UNPARSED").exists()

    list(processor.iter_inspect_orders())  # a new scan starts a new run
    processor.unparsed = _junk(source, 2)
    processor.copy_failed_to_troubles([order], first)
    assert len(list((first / "UNPARSED").glob("*.jpg"))) == 2


def test_passed_orders_contribute_nothing_but_junk_still_copied_once(tmp_path):
    source = tmp_path / "in"
    source.mkdir()
    ok = _failed_order(source, 1)
    ok.errors.clear()
    for item in ok.files:
        item.errors.clear()
    processor = _processor_with_junk(source, unparsed=3, unsupported=0)
    results = processor.copy_failed_to_troubles([ok], tmp_path / "Troubles")
    assert [target.parent.name for _s, target, _e in results] == ["UNPARSED"] * 3
    assert processor.copy_failed_to_troubles([ok], tmp_path / "Troubles") == []


# --- render job id ---------------------------------------------------------------

def test_render_does_not_read_the_pdf_to_build_its_job_id(tmp_path, monkeypatch):
    pdf = tmp_path / "order.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    seen: list[str] = []

    class Client:
        def render(self, path, dpi, job_id, **_kwargs):
            seen.append(job_id)
            return []

        def delete(self, job_id):
            pass

        def job_dir(self, job_id):
            return tmp_path / "jobs" / job_id

    processor = BatchProcessor(tmp_path, tmp_path / "PDF", callas_client=Client(), pdf_merger=lambda *a: None)
    real_open = open
    opened = []

    def spying_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spying_open)
    processor._render_pdf_with_callas(pdf, 72.0, tmp_path / "out")
    processor._render_pdf_with_callas(pdf, 72.0, tmp_path / "out")
    assert str(pdf) not in opened
    assert len(seen) == 2 and seen[0] != seen[1]  # unique per operation
    assert all(job.startswith("render-order-") for job in seen)
