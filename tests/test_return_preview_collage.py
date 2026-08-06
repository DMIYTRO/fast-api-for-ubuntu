from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from services.return_preview import (
    CUSTOM_PREVIEWS_RELATIVE_PATH,
    RETURN_PREVIEWS_RELATIVE_PATH,
    ReturnPreviewCollageError,
    custom_return_preview_path,
    create_return_preview_collage,
    prepare_return_preview_name,
)


class CustomReturnPreviewTests(unittest.TestCase):
    def test_uploaded_preview_has_priority_over_generated_previews(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory)
            custom_path = input_path / CUSTOM_PREVIEWS_RELATIVE_PATH / "1001_return-preview.jpg"
            custom_path.parent.mkdir(parents=True)
            custom_path.write_bytes(b"\xff\xd8\xffcustom")

            self.assertEqual(
                custom_return_preview_path("1001", input_path=input_path), custom_path
            )
            self.assertEqual(
                prepare_return_preview_name("1001", input_path=input_path),
                custom_path.name,
            )


@unittest.skipUnless(shutil.which("magick"), "ImageMagick is required")
class ReturnPreviewCollageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.input_path = Path(self.temporary_directory.name)
        self.processed_path = self.input_path / "Previews" / "Processed"
        self.processed_path.mkdir(parents=True)
        self.face = self.processed_path / "order-face_preview.png"
        self.back = self.processed_path / "order-back_preview.png"
        magick = shutil.which("magick")
        assert magick
        subprocess.run(
            [magick, "-size", "600x400", "xc:#d61f1f", str(self.face)],
            check=True,
        )
        subprocess.run(
            [magick, "-size", "400x600", "xc:#1959c7", str(self.back)],
            check=True,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_deterministic_side_by_side_collage(self) -> None:
        result = create_return_preview_collage(
            "25625544",
            input_path=self.input_path,
            face_preview_path=self.face,
            back_preview_path=self.back,
        )

        self.assertEqual(
            result,
            self.input_path
            / RETURN_PREVIEWS_RELATIVE_PATH
            / "25625544_return-preview.png",
        )
        self.assertTrue(result.is_file())
        self.assertGreater(result.stat().st_size, 0)

        magick = shutil.which("magick")
        assert magick
        dimensions = subprocess.run(
            [magick, "identify", "-format", "%w,%h", str(result)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        width, height = (int(value) for value in dimensions.split(","))
        # Both sources are resized to a common 1000px height.  The wide face
        # must remain on the left and hence make the final horizontal canvas
        # substantially wider than it is tall.
        self.assertEqual(height, 1000)
        self.assertGreater(width, height * 2)

        pixels = subprocess.run(
            [
                magick,
                str(result),
                "-format",
                "%[pixel:p{10,500}]\\n%[pixel:p{2000,500}]",
                "info:",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.lower()
        left, right = pixels.splitlines()
        self.assertIn("214", left)  # red face source
        self.assertIn("25", right)  # blue back source

    def test_rejects_missing_source_preview(self) -> None:
        with self.assertRaisesRegex(ReturnPreviewCollageError, "Не найдены файлы"):
            create_return_preview_collage(
                "25625544",
                input_path=self.input_path,
                face_preview_path=self.face,
                back_preview_path=self.processed_path / "missing-back_preview.png",
            )
