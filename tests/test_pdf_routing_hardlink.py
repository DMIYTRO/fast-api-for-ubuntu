"""route_pdf avoids copying the PDF when a hard link is possible, and keeps its guarantees."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from services import pdf_routing
from services.pdf_routing import route_pdf


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "PDF"
    directory.mkdir()
    return directory


def _files(root: Path) -> list[str]:
    """Every file under *root* (empty status folders are created by design)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


@pytest.fixture
def no_copy(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("PDF was copied byte by byte")

    monkeypatch.setattr(pdf_routing.shutil, "copyfile", forbidden)


def _spy_copyfile(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []
    real = shutil.copyfile

    def spy(src, dst, *a, **k):
        calls.append((src, dst))
        return real(src, dst, *a, **k)

    monkeypatch.setattr(pdf_routing.shutil, "copyfile", spy)
    return calls


def test_fast_path_links_instead_of_copying_and_publishes_same_bytes(root, no_copy):
    source = root / "1001.pdf"
    source.write_bytes(b"x" * 100_000)
    inode = source.stat().st_ino
    stale = root / "Print" / "1001.pdf"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")

    routed = route_pdf(source, "warning")

    assert routed == root / "Rework" / "1001.pdf"
    assert routed.read_bytes() == b"x" * 100_000
    assert routed.stat().st_ino == inode  # no data was duplicated
    assert routed.stat().st_nlink == 1  # temporary names are gone
    assert not source.exists() and not stale.exists()
    assert _files(root) == ["Rework/1001.pdf"]  # no .pdf-route-* leftovers


def test_fast_path_when_source_is_already_the_destination(root, no_copy):
    pdf = root / "Print" / "1001.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"ready")
    assert route_pdf(pdf, "passed") == pdf
    assert pdf.read_bytes() == b"ready" and pdf.stat().st_nlink == 1
    assert _files(root) == ["Print/1001.pdf"]


def test_fallback_to_copy_when_hard_links_are_not_possible(root, monkeypatch):
    copies = _spy_copyfile(monkeypatch)

    def no_link(*_a, **_k):
        raise OSError(95, "Operation not supported")  # e.g. SMB / FAT

    monkeypatch.setattr(pdf_routing.os, "link", no_link)
    source = root / "1001.pdf"
    source.write_bytes(b"new")
    old = root / "Failed" / "1001.pdf"
    old.parent.mkdir()
    old.write_bytes(b"old")

    routed = route_pdf(source, "passed")

    assert len(copies) == 1
    assert routed == root / "Print" / "1001.pdf" and routed.read_bytes() == b"new"
    assert not source.exists() and not old.exists()
    assert _files(root) == ["Print/1001.pdf"]


def test_fallback_to_copy_across_filesystems(root, monkeypatch):
    copies = _spy_copyfile(monkeypatch)

    def exdev(*_a, **_k):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(pdf_routing.os, "link", exdev)
    source = root / "a.pdf"
    source.write_bytes(b"data")
    assert route_pdf(source, "error").read_bytes() == b"data"
    assert len(copies) == 1


def test_symlinked_source_is_copied_not_linked(root, tmp_path, monkeypatch):
    copies = _spy_copyfile(monkeypatch)
    target = tmp_path / "outside.pdf"
    target.write_bytes(b"outside")
    link = root / "1001.pdf"
    link.symlink_to(target)

    routed = route_pdf(link, "passed")

    assert len(copies) == 1
    assert routed.read_bytes() == b"outside" and not routed.is_symlink()
    assert routed.stat().st_ino != target.stat().st_ino  # never aliases the outside file
    assert target.read_bytes() == b"outside"


def test_missing_source_still_raises_and_leaves_nothing(root):
    with pytest.raises(FileNotFoundError):
        route_pdf(root / "missing.pdf", "passed")
    assert _files(root) == []


@pytest.mark.parametrize("link_works", [True, False])
def test_failed_publish_rolls_back_source_and_previous_pdf(root, monkeypatch, link_works):
    if not link_works:
        monkeypatch.setattr(pdf_routing.os, "link", lambda *_a, **_k: (_ for _ in ()).throw(OSError(18, "xdev")))
    production = root / "Print" / "job.pdf"
    production.parent.mkdir()
    production.write_bytes(b"checked-green")
    stale = root / "Failed" / "job.pdf"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")
    candidate = root / "candidate.pdf"
    candidate.write_bytes(b"unchecked-new")
    candidate_inode = candidate.stat().st_ino

    monkeypatch.setattr(pdf_routing.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        route_pdf(candidate, "warning", pdf_dir=root, name="job.pdf")

    assert production.read_bytes() == b"checked-green"
    assert stale.read_bytes() == b"stale"
    assert candidate.read_bytes() == b"unchecked-new"
    assert candidate.stat().st_ino == candidate_inode and candidate.stat().st_nlink == 1
    assert not list(root.glob("Rework/*.pdf"))
    assert _files(root) == ["Failed/job.pdf", "Print/job.pdf", "candidate.pdf"]


def test_failure_while_staging_restores_everything(root, monkeypatch):
    source = root / "job.pdf"
    source.write_bytes(b"new")
    production = root / "Print" / "job.pdf"
    production.parent.mkdir()
    production.write_bytes(b"old")
    real_rename = Path.rename
    calls = {"n": 0}

    def flaky_rename(self, target):
        calls["n"] += 1
        if calls["n"] == 2:  # the source was staged, the old production copy fails
            raise OSError("rename failed")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="rename failed"):
        route_pdf(source, "passed", pdf_dir=root)
    monkeypatch.undo()

    assert source.read_bytes() == b"new" and production.read_bytes() == b"old"
    assert _files(root) == ["Print/job.pdf", "job.pdf"]
    assert os.stat(source).st_nlink == 1
