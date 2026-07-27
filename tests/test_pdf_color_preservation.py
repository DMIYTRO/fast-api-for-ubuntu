import unittest
from unittest.mock import patch

from core.pdf_exporter import convert_image_to_pdf, merge_pdfs_with_ghostscript


class PdfColorPreservationTests(unittest.TestCase):
    @patch("core.pdf_exporter.run_command")
    @patch("core.pdf_exporter.shutil.which", return_value="/usr/bin/magick")
    def test_image_conversion_does_not_request_colorspace_conversion(self, _which, run):
        convert_image_to_pdf("input.tif", "/tmp/output.pdf", dpi=300)
        command = run.call_args.args[0]
        self.assertNotIn("-colorspace", command)

    @patch("core.pdf_exporter.run_command")
    @patch("core.pdf_exporter.shutil.which", return_value="/usr/bin/gs")
    def test_pdf_merge_leaves_colors_unchanged(self, _which, run):
        merge_pdfs_with_ghostscript(["face.pdf", "back.pdf"], "/tmp/output.pdf")
        command = run.call_args.args[0]
        self.assertIn("-sColorConversionStrategy=LeaveColorUnchanged", command)


if __name__ == "__main__":
    unittest.main()
