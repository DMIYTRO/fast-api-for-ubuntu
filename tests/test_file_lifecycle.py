import tempfile
import unittest
from pathlib import Path

from services.file_lifecycle import FileLifecycle, FileLifecycleError


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

    def test_missing_pdf_does_not_partially_accept_order(self):
        self.pdf.unlink()

        with self.assertRaises(FileLifecycleError):
            FileLifecycle(self.root).accept_for_print(self.order)

        self.assertTrue(self.source.exists())
        self.assertTrue(self.preview.exists())

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
            [str(self.root / "Troubles" / "1001" / "face.png")],
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


if __name__ == "__main__":
    unittest.main()
