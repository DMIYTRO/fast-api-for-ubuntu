from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import unittest.mock

from services.return_preview import (
    CUSTOM_PREVIEWS_RELATIVE_PATH,
    RETURN_PREVIEWS_RELATIVE_PATH,
    ReturnPreviewCollageError,
    custom_return_preview_path,
    create_return_preview_collage,
    prepare_return_preview_name,
    publish_custom_return_preview,
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


    def test_failed_publication_keeps_legacy_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory)
            directory = input_path / CUSTOM_PREVIEWS_RELATIVE_PATH
            directory.mkdir(parents=True)
            original = directory / "1001_return-preview.png"
            original.write_bytes(b"previous PNG")

            original_open = Path.open

            def fail_staging(path: Path, mode: str):
                if mode == "xb":
                    raise OSError("publish failed")
                return original_open(path, mode)

            failures = (
                unittest.mock.patch.object(Path, "open", autospec=True, side_effect=fail_staging),
                unittest.mock.patch("services.return_preview.os.replace", side_effect=OSError("publish failed")),
            )
            for failure in failures:
                for suffix in (".png", ".jpg"):
                    with self.subTest(failure=failure.attribute, suffix=suffix):
                        with failure:
                            with self.assertRaisesRegex(OSError, "publish failed"):
                                publish_custom_return_preview(
                                    "1001", input_path=input_path, suffix=suffix, content=b"new image"
                                )
                        self.assertEqual(original.read_bytes(), b"previous PNG")
                        self.assertEqual(
                            custom_return_preview_path("1001", input_path=input_path), original
                        )
                        self.assertEqual(list(directory.glob(".upload-*")), [])

    def test_reader_does_not_expose_an_unlocked_first_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory)
            original_open = Path.open

            def publish_after_missing_lock(path: Path, mode: str):
                try:
                    return original_open(path, mode)
                except FileNotFoundError:
                    if mode == "rb" and path.suffix == ".lock":
                        publish_custom_return_preview(
                            "1001", input_path=input_path, suffix=".jpg", content=b"new image"
                        )
                    raise

            with unittest.mock.patch.object(
                Path, "open", autospec=True, side_effect=publish_after_missing_lock
            ):
                self.assertIsNone(custom_return_preview_path("1001", input_path=input_path))
            self.assertEqual(
                custom_return_preview_path("1001", input_path=input_path).read_bytes(),
                b"new image",
            )

    def test_uploaded_preview_preserves_normal_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory)
            directory = input_path / CUSTOM_PREVIEWS_RELATIVE_PATH
            directory.mkdir(parents=True)
            legacy = directory / "1001_return-preview.png"
            legacy.write_bytes(b"old image")
            replacement = publish_custom_return_preview(
                "1002", input_path=input_path, suffix=".jpg", content=b"new image"
            )
            self.assertEqual(
                stat.S_IMODE(replacement.stat().st_mode), stat.S_IMODE(legacy.stat().st_mode)
            )

    def test_concurrent_uploads_publish_one_complete_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory)
            uploads = [
                (".png" if index % 2 else ".jpg", bytes([index]) * 100_000)
                for index in range(16)
            ]

            def publish(upload: tuple[str, bytes]) -> None:
                suffix, content = upload
                publish_custom_return_preview(
                    "1001", input_path=input_path, suffix=suffix, content=content
                )

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(publish, uploads))

            directory = input_path / CUSTOM_PREVIEWS_RELATIVE_PATH
            previews = list(directory.glob("1001_return-preview.*"))
            self.assertEqual(len(previews), 1)
            self.assertIn((previews[0].suffix, previews[0].read_bytes()), uploads)
            self.assertEqual(custom_return_preview_path("1001", input_path=input_path), previews[0])
            self.assertEqual(list(directory.glob(".upload-*")), [])


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

    def test_uses_layered_composite_previews_when_batch_previews_are_missing(self) -> None:
        preview_dir = self.input_path / "Previews"
        face_layered = preview_dir / "art-face_layered-composite.png"
        back_layered = preview_dir / "art-back_layered-composite.png"
        magick = shutil.which("magick")
        assert magick
        subprocess.run([magick, "-size", "300x200", "xc:red", str(face_layered)], check=True)
        subprocess.run([magick, "-size", "300x200", "xc:blue", str(back_layered)], check=True)

        result = prepare_return_preview_name(
            "25625544",
            input_path=self.input_path,
            files=[
                {"path": "/input/art-face.tif", "parsed": {"side": "face"}, "has_unflattened_layers": True},
                {"path": "/input/art-back.tif", "parsed": {"side": "back"}, "has_unflattened_layers": True},
            ],
        )

        self.assertEqual(result, "25625544_return-preview.png")
        self.assertTrue((self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result).is_file())


def _make_image(path: Path, size: str, color: str) -> None:
    magick = shutil.which("magick")
    assert magick
    subprocess.run([magick, "-size", size, f"xc:{color}", str(path)], check=True)


def _format(path: Path) -> str:
    magick = shutil.which("magick")
    assert magick
    return subprocess.run(
        [magick, "identify", "-format", "%m", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout


FACE_BACK_FILES = [
    {"path": "/input/order-face.tif", "parsed": {"side": "face"}},
    {"path": "/input/order-back.tif", "parsed": {"side": "back"}},
]


@unittest.skipUnless(shutil.which("magick"), "ImageMagick is required")
class JpegReturnPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.input_path = Path(self.temporary_directory.name)
        self.processed = self.input_path / "Previews" / "Processed"
        self.processed.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_collage_output_stays_png(self) -> None:
        with unittest.mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("IMAGE_MAGIC_PREVIEW_FORMAT", None)
            _make_image(self.processed / "order-face_preview.jpg", "600x400", "red")
            _make_image(self.processed / "order-back_preview.jpg", "400x600", "blue")
            result = prepare_return_preview_name(
                "777", input_path=self.input_path, files=FACE_BACK_FILES
            )
        self.assertEqual(result, "777_return-preview.png")

    def test_jpeg_previews_are_found_and_face_stays_left_in_jpeg_collage(self) -> None:
        _make_image(self.processed / "order-face_preview.jpg", "600x400", "#d61f1f")
        _make_image(self.processed / "order-back_preview.jpg", "400x600", "#1959c7")
        with unittest.mock.patch.dict("os.environ", {"IMAGE_MAGIC_PREVIEW_FORMAT": "jpeg"}):
            result = prepare_return_preview_name(
                "777", input_path=self.input_path, files=FACE_BACK_FILES
            )
        self.assertEqual(result, "777_return-preview.jpg")
        collage = self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result
        self.assertEqual(_format(collage), "JPEG")
        magick = shutil.which("magick")
        left, right = subprocess.run(
            [magick, str(collage), "-format", "%[fx:int(255*p{10,500}.r)]\n%[fx:int(255*p{2000,500}.b)]", "info:"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertGreater(int(left), 180)  # red face on the left
        self.assertGreater(int(right), 150)  # blue back on the right

    def test_mixed_historical_png_and_new_jpeg_sides(self) -> None:
        _make_image(self.processed / "order-face_preview.png", "600x400", "red")
        _make_image(self.processed / "order-back_preview.jpg", "400x600", "blue")
        result = prepare_return_preview_name(
            "777", input_path=self.input_path, files=FACE_BACK_FILES
        )
        self.assertTrue((self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result).is_file())

    def test_filename_scan_finds_jpeg_and_png_and_sorts_face_before_back(self) -> None:
        # No ``files``: previews are discovered by order marker in the file name.
        _make_image(self.processed / "art-(x-7)-back_preview.jpg", "400x600", "#1959c7")
        _make_image(self.processed / "art-(x-7)-face_preview.png", "600x400", "#d61f1f")
        result = prepare_return_preview_name("7", input_path=self.input_path)
        self.assertEqual(result, "7_return-preview.png")
        magick = shutil.which("magick")
        left, right = subprocess.run(
            [magick, str(self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result), "-format",
             "%[fx:int(255*p{10,500}.r)]\n%[fx:int(255*p{2000,500}.b)]", "info:"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertGreater(int(left), 180)
        self.assertGreater(int(right), 150)

    def test_single_jpeg_preview_is_returned_by_name(self) -> None:
        _make_image(self.processed / "solo-(x-55)_preview.jpg", "300x200", "red")
        self.assertEqual(
            prepare_return_preview_name("55", input_path=self.input_path),
            "solo-(x-55)_preview.jpg",
        )

    def test_png_and_jpeg_twins_collapse_to_the_newest(self) -> None:
        png = self.processed / "solo-(x-56)_preview.png"
        jpg = self.processed / "solo-(x-56)_preview.jpg"
        _make_image(png, "300x200", "red")
        _make_image(jpg, "300x200", "red")
        os.utime(png, ns=(1_000_000_000, 1_000_000_000))
        os.utime(jpg, ns=(2_000_000_000, 2_000_000_000))
        self.assertEqual(
            prepare_return_preview_name("56", input_path=self.input_path),
            "solo-(x-56)_preview.jpg",
        )
        os.utime(png, ns=(3_000_000_000, 3_000_000_000))
        self.assertEqual(
            prepare_return_preview_name("56", input_path=self.input_path),
            "solo-(x-56)_preview.png",
        )

    def test_preview_paths_with_jpeg_are_accepted(self) -> None:
        preview = self.processed / "solo-(x-57)_preview.jpg"
        _make_image(preview, "300x200", "red")
        self.assertEqual(
            prepare_return_preview_name(
                "57", input_path=self.input_path, preview_paths=[str(preview.name)]
            ),
            preview.name,
        )

    def test_multi_page_sheet_accepts_jpeg_pages_in_page_order(self) -> None:
        for page in (2, 10, 1):
            _make_image(self.processed / f"book-(x-58)_page{page}_preview.jpg", "300x200", "red")
        result = prepare_return_preview_name("58", input_path=self.input_path)
        self.assertEqual(result, "58_return-preview.png")

    def test_layered_composite_jpeg_is_used_when_flagged(self) -> None:
        _make_image(self.input_path / "Previews" / "art-face_layered-composite.jpg", "300x200", "red")
        _make_image(self.input_path / "Previews" / "art-back_layered-composite.png", "300x200", "blue")
        result = prepare_return_preview_name(
            "59",
            input_path=self.input_path,
            files=[
                {"path": "/input/art-face.tif", "parsed": {"side": "face"}, "has_unflattened_layers": True},
                {"path": "/input/art-back.tif", "parsed": {"side": "back"}, "has_unflattened_layers": True},
            ],
        )
        self.assertTrue((self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result).is_file())


@unittest.skipUnless(shutil.which("magick"), "ImageMagick is required")
class MissingSideReturnPreviewTests(unittest.TestCase):
    """Двусторонний заказ без одной стороны: пустой лист на её месте (заказ 26262655)."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.input_path = Path(self.temporary_directory.name)
        self.processed = self.input_path / "Previews" / "Processed"
        self.processed.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _pixels(self, path: Path, *points: tuple[int, int]) -> list[str]:
        magick = shutil.which("magick")
        fmt = "\n".join(f"%[fx:int(255*p{{{x},{y}}}.r)],%[fx:int(255*p{{{x},{y}}}.b)]" for x, y in points)
        return subprocess.run(
            [magick, str(path), "-format", fmt, "info:"], check=True, capture_output=True, text=True,
        ).stdout.split()

    def test_back_only_duplex_order_gets_blank_face_on_the_left(self) -> None:
        _make_image(self.processed / "order-back_preview.png", "600x400", "blue")
        result = prepare_return_preview_name(
            "26262655",
            input_path=self.input_path,
            files=[{"path": "/input/order-back.pdf", "parsed": {"side": "back", "back_colors": 4}}],
        )
        self.assertEqual(result, "26262655_return-preview.png")
        collage = self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result
        # Слева белый лист (вдали от креста), справа синий оборот.
        left, right = self._pixels(collage, (750, 150), (2500, 500))
        self.assertEqual(left, "255,255")
        self.assertEqual(right, "0,255")

    def test_face_only_duplex_order_gets_blank_back_on_the_right(self) -> None:
        _make_image(self.processed / "order-face_preview.png", "600x400", "red")
        result = prepare_return_preview_name(
            "9", input_path=self.input_path,
            files=[{"path": "/input/order-face.pdf", "parsed": {"side": "face", "back_colors": 4}}],
        )
        collage = self.input_path / RETURN_PREVIEWS_RELATIVE_PATH / result
        left, right = self._pixels(collage, (300, 200), (2200, 200))
        self.assertEqual(left, "255,0")
        self.assertEqual(right, "255,255")

    def test_one_sided_order_keeps_its_single_preview(self) -> None:
        _make_image(self.processed / "order-face_preview.png", "600x400", "red")
        result = prepare_return_preview_name(
            "10", input_path=self.input_path,
            files=[{"path": "/input/order-face.pdf", "parsed": {"side": "face", "back_colors": 0}}],
        )
        self.assertEqual(result, "order-face_preview.png")
