import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.inspector import ImageMetadata
from processing.batch_processor import BatchProcessor
from processing.filename_parser import parse_filename


class OrderIdentityTests(unittest.TestCase):
    def test_parses_production_filename_variants(self):
        names = (
            "08_NP_Bezlam_80_ofset_(90x90)*4-4_T5000*"
            "(1069-25694739)_offset-face.tif",
            "08_NP_Bezlam_80_ofset_(90x90)*4-4_T5000*"
            "(1069--25694739)_offset-face.tif",
            "08_NP_Bezlam_80_ofset_(90x90)*4-4_T5000*"
            "(1069-25694739_offset-face.tif",
        )

        for name in names:
            with self.subTest(name=name):
                parsed = parse_filename(Path(name))
                self.assertEqual(
                    (
                        parsed.customer_id,
                        parsed.order_id,
                        parsed.width_mm,
                        parsed.height_mm,
                        parsed.front_colors,
                        parsed.back_colors,
                        parsed.side,
                    ),
                    ("1069", "25694739", 90.0, 90.0, 4, 4, "face"),
                )

    def test_does_not_accept_ambiguous_order_numbers(self):
        invalid_names = (
            "job_(90x90)*4-4*(1069---25694739)_offset-face.tif",
            "job_(90x90)*4-4*(1069-25694739_offset.tif",
            "job_(90x90)*4-4_1069-25694739_offset-face.tif",
        )

        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "номер клиента"):
                    parse_filename(Path(name))

    def test_side_less_one_sided_jpg_is_treated_as_face(self):
        for colors in ("1-0", "4-0", "5-0"):
            parsed = parse_filename(
                Path(f"job_(90x50)_{colors}_(777-25657208).jpg")
            )
            self.assertEqual(parsed.customer_id, "777")
            self.assertEqual(parsed.order_id, "25657208")
            self.assertEqual(parsed.side, "")

    def test_same_order_number_for_different_customers_stays_separate(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            for customer in ("10", "20"):
                (root / f"job_(90x50)_4-0_({customer}-1001)_face.jpg").write_bytes(
                    b"image"
                )
            metadata = ImageMetadata(
                file_path="image",
                file_name="image.jpg",
                format="JPEG",
                width_px=1110,
                height_px=638,
                dpi=300,
                dpi_x=300,
                dpi_y=300,
                width_mm=94,
                height_mm=54,
                colorspace="CMYK",
                icc_profile="profile",
                image_type="TrueColor",
                depth_bits="8",
                size_mb=1,
            )
            processor = BatchProcessor(root, root / "PDF")
            with patch(
                "processing.batch_processor.count_frames", return_value=1
            ), patch(
                "processing.batch_processor.inspect_file", return_value=metadata
            ):
                orders = processor.inspect_orders()

        self.assertEqual(len(orders), 2)
        self.assertEqual(
            {order.aggregate_id for order in orders},
            {"10:1001", "20:1001"},
        )


if __name__ == "__main__":
    unittest.main()
