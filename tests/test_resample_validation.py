import unittest
from pathlib import Path
from types import SimpleNamespace

from processing.batch_processor import BatchProcessor
from processing.models import FileCheck


class ResampleValidationTests(unittest.TestCase):
    def setUp(self):
        self.processor = BatchProcessor(Path("input"), Path("output"))

    def make_check(self, actual, pixels, product=(210.0, 148.0)):
        check = FileCheck(path=Path("order_face.tif"))
        check.parsed = SimpleNamespace(
            width_mm=product[0],
            height_mm=product[1],
            front_colors=4,
            back_colors=4,
            side="face",
        )
        check.actual_width_mm, check.actual_height_mm = actual
        check.width_px, check.height_px = pixels
        check.dpi = check.dpi_x = check.dpi_y = 300.0
        check.colorspace = "CMYK"
        return check

    def test_rejects_mixed_shrink_and_enlarge_case(self):
        check = self.make_check((202.0, 214.0), (2386, 2528))

        self.processor._validate_file(check)

        self.assertFalse(check.needs_resample)
        self.assertEqual(check.resample_decision, "reject")
        self.assertTrue(any("потребуется обрезка" in error for error in check.errors))

    def test_allows_proportional_downsample_in_matching_orientation(self):
        # 428 x 304 mm is exactly twice the required 214 x 152 mm layout; at 150 DPI the
        # shrink raises the resolution to 300 DPI, so it stays automatic.
        check = self.make_check((428.0, 304.0), (2528, 1795))
        check.dpi = check.dpi_x = check.dpi_y = 150.0

        self.processor._validate_file(check)

        self.assertTrue(check.needs_resample)
        self.assertEqual(check.resample_target_mm, (214.0, 152.0))
        self.assertFalse(check.errors)
        self.assertEqual(check.size_fix["method"], "shrink")
        self.assertTrue(check.size_fix["auto"])

    def test_large_high_dpi_downsample_requires_confirmation(self):
        # The same layout at 300 DPI: shrinking 214 mm is far above the 2 mm auto limit.
        check = self.make_check((428.0, 304.0), (5055, 3591))

        self.processor._validate_file(check)

        self.assertFalse(check.needs_resample)
        self.assertEqual(check.resample_decision, "ask_confirmation")
        self.assertEqual(check.resample_target_mm, (214.0, 152.0))
        self.assertIsNone(check.size_fix)
        self.assertFalse(check.errors)

    def test_large_source_with_small_crop_requires_confirmation(self):
        self.processor.correction_policy = "ask"
        # 960:560 does not match the required 94:54 layout. Fitting the width
        # proportionally would produce a height of about 54.83 mm.
        check = self.make_check(
            (960.0, 560.0),
            (11339, 6614),
            product=(90.0, 50.0),
        )

        self.processor._validate_file(check)

        self.assertFalse(check.needs_resample)
        self.assertEqual(check.resample_decision, "ask_confirmation")
        self.assertTrue(any("требуется подтверждение" in warning for warning in check.warnings))

    def test_rgb_is_warning_and_does_not_reject_file(self):
        check = self.make_check((214.0, 152.0), (2528, 1795))
        check.colorspace = "sRGB"

        self.processor._validate_file(check)

        self.assertFalse(check.errors)
        self.assertTrue(check.passed)
        self.assertTrue(any("без преобразования цветовой модели" in warning for warning in check.warnings))

    def test_single_sided_swapped_dimensions_are_not_rotated(self):
        check = self.make_check(
            (94.1, 54.0),
            (1111, 638),
            product=(50.0, 90.0),
        )
        check.parsed.back_colors = 0

        self.processor._validate_file(check)

        self.assertTrue(check.passed)
        self.assertEqual(check.rotation_degrees, 0)
        self.assertFalse(check.needs_resample)


if __name__ == "__main__":
    unittest.main()
