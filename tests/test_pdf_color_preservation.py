import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from core.pdf_exporter import convert_image_to_pdf


class PdfColorPreservationTests(unittest.TestCase):
    @patch("core.pdf_exporter.run_command")
    @patch("core.pdf_exporter.shutil.which", return_value="/usr/bin/magick")
    def test_image_conversion_does_not_request_colorspace_conversion(self, _which, run):
        convert_image_to_pdf("input.tif", "/tmp/output.pdf", dpi=300)
        command = run.call_args.args[0]
        self.assertNotIn("-colorspace", command)

    @patch("core.pdf_exporter.run_command")
    @patch("core.pdf_exporter.shutil.which", return_value="/usr/bin/magick")
    def test_single_layer_composite_define_precedes_tiff_input(self, _which, run):
        convert_image_to_pdf(
            "input.tif", "/tmp/output.pdf", dpi=300, ignore_tiff_layers=True
        )
        command = run.call_args.args[0]
        self.assertLess(command.index("-define"), command.index("input.tif"))
        self.assertEqual(command[command.index("-define") + 1], "tiff:ignore-layers=true")

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

if __name__ == "__main__":
    unittest.main()
