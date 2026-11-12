import unittest
from dataclasses import replace
from processing.profile_rules import evaluate_metadata_rules
from pathlib import Path

from config.profiles import DEFAULT_PROFILE, PROFILES, get_profile
from processing import BatchProcessor


class ProfileTests(unittest.TestCase):
    def test_shared_profile_rules_enforce_maximum_file_size(self):
        profile = replace(PROFILES["digital"], max_file_size_mb=10)
        result = evaluate_metadata_rules(
            profile=profile,
            size_mb=12,
            colorspace="CMYK",
            dpi_x=300,
            dpi_y=300,
        )
        self.assertTrue(any("превышает лимит" in value for value in result.errors))

    def test_minimum_dpi_is_250_with_small_rounding_tolerance(self):
        profile = get_profile("digital")
        at_limit = evaluate_metadata_rules(
            profile=profile,
            size_mb=1,
            colorspace="CMYK",
            dpi_x=250.0,
            dpi_y=250.0,
        )
        below_limit = evaluate_metadata_rules(
            profile=profile,
            size_mb=1,
            colorspace="CMYK",
            dpi_x=249.4,
            dpi_y=250.0,
        )

        self.assertFalse(any("разрешение" in error for error in at_limit.errors))
        self.assertTrue(any("разрешение" in error for error in below_limit.errors))

    def test_digital_is_default_and_profiles_are_independent(self):
        self.assertIs(DEFAULT_PROFILE, PROFILES["digital"])
        self.assertEqual(get_profile("digital").direction, "digital")
        self.assertEqual(get_profile("offset").direction, "offset")
        self.assertIsNotNone(get_profile("digital").sheet_trim)
        self.assertIsNone(get_profile("offset").sheet_trim)

    def test_batch_processor_reads_values_from_profile(self):
        profile = get_profile("offset")
        processor = BatchProcessor(Path("input"), Path("output"), profile=profile)
        self.assertIs(processor.profile, profile)
        self.assertEqual(processor.size_extra_mm, profile.bleed_mm * 2)
        self.assertEqual(processor.tolerance_mm, profile.size_tolerance_mm)
        self.assertEqual(processor.min_dpi, profile.min_dpi)
        self.assertEqual(profile.min_dpi, 250.0)
        self.assertEqual(profile.target_dpi, 300.0)
        self.assertIn((4, 4), profile.allowed_color_modes)

    def test_profiles_carry_ink_coverage_thresholds(self):
        for direction in ("digital", "offset"):
            profile = get_profile(direction)
            self.assertIsNone(profile.ink_min_percent)
            self.assertIsNone(profile.ink_max_percent)

    def test_unknown_direction_has_clear_error(self):
        with self.assertRaisesRegex(ValueError, "доступно: digital, offset"):
            get_profile("unknown")


if __name__ == "__main__":
    unittest.main()
