import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from core.pdf_exporter import convert_image_to_pdf, merge_pdfs_with_pymupdf


class PdfColorPreservationTests(unittest.TestCase):
    @patch("core.pdf_exporter.run_command")
    @patch("core.pdf_exporter.shutil.which", return_value="/usr/bin/magick")
    def test_image_conversion_does_not_request_colorspace_conversion(self, _which, run):
        convert_image_to_pdf("input.tif", "/tmp/output.pdf", dpi=300)
        command = run.call_args.args[0]
        self.assertNotIn("-colorspace", command)

    @staticmethod
    def _make_pdf(path, page_specs):
        document = pymupdf.open()
        for number, (width, height, rotation) in enumerate(page_specs, start=1):
            page = document.new_page(width=width, height=height)
            page.set_cropbox(page.rect)
            page.set_trimbox(page.rect)
            page.set_bleedbox(page.rect)
            page.set_artbox(page.rect)
            page.set_rotation(rotation)
            page.insert_text((20, 30), f"source-page-{number}")
        document.save(path)
        document.close()

    def test_pdf_merge_rejects_empty_input(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                merge_pdfs_with_pymupdf([], str(Path(directory) / "output.pdf"))

    def test_pdf_merge_copies_all_pages_in_order_and_preserves_geometry(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            first = directory / "first.pdf"
            second = directory / "second.pdf"
            output = directory / "nested" / "output.pdf"
            self._make_pdf(first, [(200, 100, 0), (300, 150, 90)])
            self._make_pdf(second, [(400, 250, 180)])
            source_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (first, second)
            }

            result = merge_pdfs_with_pymupdf([str(first), str(second)], str(output))

            self.assertEqual(result, str(output))
            merged = pymupdf.open(output)
            try:
                self.assertEqual(merged.page_count, 3)
                self.assertEqual(
                    [page.get_text().strip() for page in merged],
                    ["source-page-1", "source-page-2", "source-page-1"],
                )
                expected = [(200, 100, 0), (300, 150, 90), (400, 250, 180)]
                for page, (width, height, rotation) in zip(merged, expected):
                    self.assertAlmostEqual(page.mediabox.width, width, places=2)
                    self.assertAlmostEqual(page.mediabox.height, height, places=2)
                    for box in (page.cropbox, page.trimbox, page.bleedbox, page.artbox):
                        self.assertAlmostEqual(box.width, width, places=2)
                        self.assertAlmostEqual(box.height, height, places=2)
                    self.assertEqual(page.rotation, rotation)
            finally:
                merged.close()

            for path, original_hash in source_hashes.items():
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), original_hash)

    def test_pdf_merge_rejects_missing_empty_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            with self.assertRaises(FileNotFoundError):
                merge_pdfs_with_pymupdf([str(directory / "missing.pdf")], str(directory / "out.pdf"))

            empty = directory / "empty.pdf"
            empty.touch()
            with self.assertRaises(ValueError):
                merge_pdfs_with_pymupdf([str(empty)], str(directory / "out.pdf"))

            invalid = directory / "invalid.pdf"
            invalid.write_bytes(b"not a PDF")
            with self.assertRaises(ValueError):
                merge_pdfs_with_pymupdf([str(invalid)], str(directory / "out.pdf"))

    def test_pdf_merge_result_opens_with_pymupdf(self):
        with tempfile.TemporaryDirectory() as directory_name:
            directory = Path(directory_name)
            source = directory / "source.pdf"
            output = directory / "output.pdf"
            self._make_pdf(source, [(120, 80, 0)])
            merge_pdfs_with_pymupdf([str(source)], str(output))
            with pymupdf.open(output) as result:
                self.assertEqual(result.page_count, 1)


if __name__ == "__main__":
    unittest.main()
