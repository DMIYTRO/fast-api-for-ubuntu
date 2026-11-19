import tempfile
import unittest
from pathlib import Path

from services.file_lifecycle import FileLifecycle, FileLifecycleError
import pytest


class FileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.source = self.root / "face.jpg"
        self.pdf = self.root / "PDF" / "1001.pdf"
        self.preview = self.root / "Previews" / "face.png"
        self.source.write_bytes(b"source")
        self.pdf.parent.mkdir()
        self.preview.parent.mkdir()
        self.pdf.write_bytes(b"pdf")
        self.preview.write_bytes(b"preview")
        self.order = {
            "order_id": "1001",
            "files": [{"path": str(self.source)}],
            "pdf_path": str(self.pdf),
            "preview_paths": [str(self.preview)],
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_accept_for_print_moves_every_artifact_and_returns_new_paths(self):
        transition = FileLifecycle(self.root).accept_for_print(self.order)

        self.assertFalse(self.source.exists())
        self.assertFalse(self.pdf.exists())
        self.assertFalse(self.preview.exists())
        self.assertEqual(
            transition.source_paths[str(self.source)], str(self.root / "Processed" / "face.jpg")
        )
        self.assertEqual(transition.pdf_path, str(self.root / "PDF" / "Print" / "1001.pdf"))
        self.assertEqual(
            transition.preview_paths, [str(self.root / "Previews" / "Processed" / "face.png")]
        )

    def test_accept_for_print_can_replace_existing_conflicting_targets(self):
        processed_source = self.root / "Processed" / "face.jpg"
        processed_pdf = self.root / "PDF" / "Print" / "1001.pdf"
        processed_preview = self.root / "Previews" / "Processed" / "face.png"
        processed_source.parent.mkdir(parents=True)
        processed_pdf.parent.mkdir(parents=True)
        processed_preview.parent.mkdir(parents=True)
        processed_source.write_bytes(b"old-source")
        processed_pdf.write_bytes(b"old-pdf")
        processed_preview.write_bytes(b"old-preview")

        transition = FileLifecycle(self.root).accept_for_print(
            self.order, conflict_strategy="replace"
        )

        self.assertFalse(self.source.exists())
        self.assertFalse(self.pdf.exists())
        self.assertFalse(self.preview.exists())
        self.assertEqual(
            transition.source_paths[str(self.source)], str(processed_source)
        )

    def test_accept_for_print_can_rename_conflicting_targets(self):
        processed_source = self.root / "Processed" / "face.jpg"
        processed_source.parent.mkdir(parents=True)
        processed_source.write_bytes(b"old-source")

        transition = FileLifecycle(self.root).accept_for_print(
            self.order, conflict_strategy="rename"
        )

        self.assertFalse(self.source.exists())
        self.assertTrue((self.root / "Processed" / "face (1).jpg").exists())
        self.assertEqual(
            transition.source_paths[str(self.source)],
            str(self.root / "Processed" / "face (1).jpg"),
        )

    def test_missing_pdf_does_not_partially_accept_order(self):
        self.pdf.unlink()

        with self.assertRaises(FileLifecycleError):
            FileLifecycle(self.root).accept_for_print(self.order)

        self.assertTrue(self.source.exists())
        self.assertTrue(self.preview.exists())

    def test_completed_transition_can_be_compensated(self):
        lifecycle = FileLifecycle(self.root)
        transition = lifecycle.accept_for_print(self.order)

        lifecycle.rollback(transition)

        self.assertEqual(self.source.read_bytes(), b"source")
        self.assertEqual(self.pdf.read_bytes(), b"pdf")
        self.assertEqual(self.preview.read_bytes(), b"preview")
        self.assertFalse((self.root / "Processed" / "face.jpg").exists())

    def test_error_routing_moves_available_artifacts_to_troubles(self):
        self.pdf.unlink()
        transition = FileLifecycle(self.root).route_to_errors(self.order)

        self.assertFalse(self.source.exists())
        self.assertFalse(self.preview.exists())
        self.assertEqual(
            transition.source_paths[str(self.source)],
            str(self.root / "Troubles" / "1001" / "face.jpg"),
        )
        self.assertEqual(
            transition.preview_paths,
            [str(self.root / "Troubles" / "1001" / "Previews" / "face.png")],
        )

    def test_error_routing_removes_original_when_troubles_already_has_same_copy(self):
        existing = self.root / "Troubles" / "1001" / "face.jpg"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"source")
        self.pdf.unlink()

        transition = FileLifecycle(self.root).route_to_errors(self.order)

        self.assertFalse(self.source.exists())
        self.assertEqual(
            transition.source_paths[str(self.source)], str(existing)
        )

    def test_return_of_pdf_order_with_same_named_built_pdf(self):
        """Заказ 26234881: собранный PDF называется как исходный PDF клиента."""
        name = "23_KS(K)_Bezlam_300_(90x50)_4-4_(36056-26234881)_1.pdf"
        source = self.root / name
        built = self.root / "PDF" / "Rework" / name
        preview = self.root / "Previews" / name.replace(".pdf", "_face_preview.png")
        built.parent.mkdir(parents=True)
        source.write_bytes(b"client")
        built.write_bytes(b"built")
        preview.write_bytes(b"preview")
        order = {
            "order_id": "26234881",
            "files": [{"path": str(source)}],
            "pdf_path": str(built),
            "preview_paths": [str(preview)],
        }

        transition = FileLifecycle(self.root).return_for_rework(order)

        target = self.root / "Troubles" / "26234881"
        self.assertEqual((target / name).read_bytes(), b"client")
        self.assertEqual((target / "PDF" / name).read_bytes(), b"built")
        self.assertEqual(transition.source_paths[str(source)], str(target / name))
        self.assertEqual(transition.pdf_path, str(target / "PDF" / name))
        self.assertEqual(transition.preview_paths, [str(target / "Previews" / preview.name)])
        self.assertFalse(source.exists() or built.exists() or preview.exists())

    def test_return_reuses_check_time_copy_and_keeps_error_note(self):
        name = "job_(90x50)_4-4_(1-1001)-face.pdf"
        source = self.root / name
        built = self.root / "PDF" / "Failed" / name
        built.parent.mkdir(parents=True)
        source.write_bytes(b"client")
        built.write_bytes(b"built")
        target = self.root / "Troubles" / "1001"
        target.mkdir(parents=True)
        (target / name).write_bytes(b"client")
        (target / f"{name}.error.txt").write_text("причина", encoding="utf-8")
        order = {"order_id": "1001", "files": [{"path": str(source)}], "pdf_path": str(built), "preview_paths": []}

        FileLifecycle(self.root).return_for_rework(order)

        self.assertFalse(source.exists())
        self.assertEqual((target / name).read_bytes(), b"client")
        self.assertEqual((target / "PDF" / name).read_bytes(), b"built")
        self.assertTrue((target / f"{name}.error.txt").exists())

    def test_returned_order_can_be_rolled_back(self):
        name = "job_(90x50)_4-0_(1-1001).pdf"
        source = self.root / name
        built = self.root / "PDF" / "Rework" / name
        built.parent.mkdir(parents=True)
        source.write_bytes(b"client")
        built.write_bytes(b"built")
        order = {"order_id": "1001", "files": [{"path": str(source)}], "pdf_path": str(built), "preview_paths": []}
        lifecycle = FileLifecycle(self.root)

        lifecycle.rollback(lifecycle.return_for_rework(order))

        self.assertEqual(source.read_bytes(), b"client")
        self.assertEqual(built.read_bytes(), b"built")

    def test_return_still_rejects_two_sources_with_one_name(self):
        other = self.root / "other" / "face.jpg"
        other.parent.mkdir()
        other.write_bytes(b"other")
        order = dict(self.order, files=[{"path": str(self.source)}, {"path": str(other)}])

        with self.assertRaises(FileLifecycleError):
            FileLifecycle(self.root).return_for_rework(order)
        self.assertTrue(self.source.exists() and other.exists())


if __name__ == "__main__":
    unittest.main()


def test_yellow_pdf_goes_to_print_and_red_only_when_confirmed(tmp_path):
    import pytest
    pdf = tmp_path / "PDF" / "Rework" / "job.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"rework")
    source = tmp_path / "job-face.jpg"
    source.write_bytes(b"src")
    preview = tmp_path / "Previews" / "job.png"
    preview.parent.mkdir()
    preview.write_bytes(b"png")
    order = {
        "status": "error", "pdf_path": str(pdf),
        "files": [{"path": str(source)}], "preview_paths": [str(preview)],
    }
    with pytest.raises(FileLifecycleError, match="подтверждения"):
        FileLifecycle(tmp_path).accept_for_print(order)
    assert pdf.is_file() and not (tmp_path / "PDF" / "Print" / "job.pdf").exists()
    FileLifecycle(tmp_path).accept_for_print(order, allow_failed=True)
    assert (tmp_path / "PDF" / "Print" / "job.pdf").is_file()


def test_yellow_pdf_is_accepted_for_print(tmp_path):
    pdf = tmp_path / "PDF" / "Rework" / "job.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"rework")
    source = tmp_path / "job-face.jpg"
    source.write_bytes(b"src")
    preview = tmp_path / "Previews" / "job.png"
    preview.parent.mkdir()
    preview.write_bytes(b"png")
    FileLifecycle(tmp_path).accept_for_print(
        {
            "status": "warning", "pdf_path": str(pdf),
            "files": [{"path": str(source)}], "preview_paths": [str(preview)],
        }
    )
    assert (tmp_path / "PDF" / "Print" / "job.pdf").is_file()


def _eio(path):
    import errno as _errno
    return OSError(_errno.EIO, "Input/output error", str(path))


def test_return_retries_a_file_locked_on_the_share(tmp_path, monkeypatch):
    """Заказ 26237531 (19.11): исходник держал другой клиент SMB — [Errno 5] при удалении."""
    from services import file_lifecycle

    monkeypatch.setattr(file_lifecycle.time, "sleep", lambda _s: None)
    source = tmp_path / "job_(36056-26237531).pdf"
    source.write_bytes(b"%PDF")
    copy = tmp_path / "Troubles" / "26237531" / source.name
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"%PDF")  # копия, сделанная при проверке
    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self == source and calls["n"] < 2:
            calls["n"] += 1
            raise _eio(self)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    order = {"order_id": "26237531", "files": [{"path": str(source)}], "pdf_path": None, "preview_paths": []}

    FileLifecycle(tmp_path).return_for_rework(order)

    assert not source.exists() and copy.is_file()
    assert calls["n"] == 2


def test_persistently_locked_file_gets_an_actionable_message(tmp_path, monkeypatch):
    from services import file_lifecycle

    monkeypatch.setattr(file_lifecycle.time, "sleep", lambda _s: None)
    source = tmp_path / "job_(36056-26237531).pdf"
    source.write_bytes(b"%PDF")

    def locked_move(src, dst, *args, **kwargs):
        raise _eio(src)

    monkeypatch.setattr(file_lifecycle.shutil, "move", locked_move)
    order = {"order_id": "26237531", "files": [{"path": str(source)}], "pdf_path": None, "preview_paths": []}

    with pytest.raises(FileLifecycleError) as error:
        FileLifecycle(tmp_path).return_for_rework(order)

    assert "занят другой программой на сетевом диске" in str(error.value)
    assert "job_(36056-26237531).pdf" in str(error.value)
    assert source.is_file()
