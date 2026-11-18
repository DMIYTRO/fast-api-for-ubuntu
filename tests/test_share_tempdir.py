"""Временные папки на SMB: ошибка уборки ([Errno 39]) не роняет готовый PDF."""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from core import share_tempdir
from core.share_tempdir import remove_tree_quietly, share_temp_dir, sweep_stale_temp_dirs
from processing.batch_processor import BatchProcessor
from tests.fake_callas import FakeCallasMerger
from tests.test_pdf_creation_with_pdf_inputs import _make_pdf

REAL_RMTREE = shutil.rmtree


def _not_empty(path, *args, **kwargs):
    raise OSError(errno.ENOTEMPTY, "Directory not empty", str(path))


def _flaky(failures: int):
    """rmtree, который первые ``failures`` раз падает как CIFS с delete-pending файлом."""
    calls = {"n": 0}

    def rmtree(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            _not_empty(path)
        return REAL_RMTREE(path, *args, **kwargs)

    return rmtree, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(share_tempdir.time, "sleep", lambda _seconds: None)


def test_retry_removes_folder_after_transient_enotempty(tmp_path, caplog):
    folder = tmp_path / ".1_x"
    (folder / "validation").mkdir(parents=True)
    rmtree, calls = _flaky(2)
    with patch.object(share_tempdir, "_rmtree", rmtree), caplog.at_level(logging.INFO):
        assert remove_tree_quietly(folder) is True
    assert not folder.exists()
    assert calls["n"] == 3
    assert "share_tempdir.cleanup_retried" in caplog.text


def test_persistent_failure_is_logged_not_raised(tmp_path, caplog):
    with patch.object(share_tempdir, "_rmtree", _not_empty), caplog.at_level(logging.WARNING):
        with share_temp_dir(prefix=".26234424_", dir=tmp_path) as folder:
            Path(folder, "combined.pdf").write_bytes(b"%PDF")
    assert "share_tempdir.cleanup_deferred" in caplog.text
    assert "Directory not empty" in caplog.text


def test_missing_folder_counts_as_removed(tmp_path):
    assert remove_tree_quietly(tmp_path / "gone") is True


def test_body_exception_still_propagates_and_folder_is_removed(tmp_path):
    created = []
    with pytest.raises(ValueError, match="boom"):
        with share_temp_dir(prefix=".1_", dir=tmp_path) as folder:
            created.append(Path(folder))
            raise ValueError("boom")
    assert created and not created[0].exists()


def _aged(path: Path, seconds_ago: float, now: float) -> None:
    os.utime(path, (now - seconds_ago, now - seconds_ago))


def test_sweep_removes_only_old_image_magic_temp_folders(tmp_path):
    now = 1_000_000.0
    old = [".26234424__4t0qfk0", ".26234896_diag_2sto1__i", ".raster-pdf-ab", ".pdf-route-cd", ".revision-ef"]
    for name in old:
        (tmp_path / name).mkdir()
        (tmp_path / name / "left.png").write_bytes(b"x")
        _aged(tmp_path / name, 7200, now)
    fresh = tmp_path / ".26240000_running"
    fresh.mkdir()
    _aged(fresh, 60, now)
    foreign = [tmp_path / "Print", tmp_path / ".git", tmp_path / ".preview-cache"]
    for path in foreign:
        path.mkdir()
        _aged(path, 7200, now)
    pdf = tmp_path / ".26234424_old.pdf"
    pdf.write_bytes(b"%PDF")
    _aged(pdf, 7200, now)

    removed = sweep_stale_temp_dirs(tmp_path, now=now)

    assert sorted(path.name for path in removed) == sorted(old)
    assert fresh.exists() and pdf.exists()
    assert all(path.exists() for path in foreign)


def test_sweep_of_missing_root_is_silent(tmp_path):
    assert sweep_stale_temp_dirs(tmp_path / "absent") == []


def test_create_pdfs_keeps_published_pdf_when_cleanup_fails(tmp_path, caplog):
    """Заказ 26234424: PDF опубликован, а уборка папки упала — заказ не должен стать ошибкой."""
    _make_pdf(tmp_path / "job_(90x50)_4-4_(1-200)_input.pdf", 2)
    processor = BatchProcessor(tmp_path, tmp_path / "output", pdf_merger=FakeCallasMerger())
    orders = processor.inspect_orders()

    with patch.object(share_tempdir, "_rmtree", _not_empty), caplog.at_level(logging.WARNING):
        results = processor.create_pdfs(orders)

    order, output, error = results[0]
    assert error is None
    assert output.is_file()
    assert "share_tempdir.cleanup_deferred" in caplog.text


def test_create_pdfs_sweeps_stale_folders_once(tmp_path):
    _make_pdf(tmp_path / "job_(90x50)_4-4_(1-200)_input.pdf", 2)
    output_dir = tmp_path / "output"
    stale = output_dir / ".111_leftover"
    stale.mkdir(parents=True)
    _aged(stale, 7200, __import__("time").time())
    processor = BatchProcessor(tmp_path, output_dir, pdf_merger=FakeCallasMerger())
    orders = processor.inspect_orders()

    with patch("processing.batch_processor.sweep_stale_temp_dirs", wraps=sweep_stale_temp_dirs) as sweep:
        assert processor.create_pdfs(orders)[0][2] is None
        processor.create_pdfs(orders)

    assert not stale.exists()
    assert sweep.call_count == 1


def test_diagnostic_pdf_survives_cleanup_failure(tmp_path):
    """Заказ 26234896: диагностический PDF в PDF/Failed не теряется из-за уборки."""
    _make_pdf(tmp_path / "job_(90x50)_4-4_(1-200)_input.pdf", 2)
    processor = BatchProcessor(tmp_path, tmp_path / "output", pdf_merger=FakeCallasMerger())
    order = processor.inspect_orders()[0]

    with patch.object(share_tempdir, "_rmtree", _not_empty):
        path, _note = processor.create_diagnostic_pdf(order, tmp_path / "output" / "Failed")

    assert path is not None and path.is_file()
