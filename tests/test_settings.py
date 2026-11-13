import os
import unittest
from unittest.mock import patch

from server.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_admin_password_hash_is_read_from_environment(self):
        with patch.dict(
            os.environ,
            {"IMAGE_MAGIC_PASSWORD_HASH": "test-password-hash"},
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.password_hash, "test-password-hash")

    def test_missing_admin_password_hash_uses_test_server_password(self):
        with patch.dict(os.environ, {"IMAGE_MAGIC_PASSWORD_HASH": ""}):
            settings = Settings.from_env()
        self.assertTrue(settings.password_hash)

    def test_pitstop_configuration_is_loaded_without_passwords(self):
        with patch.dict(
            os.environ,
            {
                "IMAGE_MAGIC_PITSTOP_ENABLED": "true",
                "IMAGE_MAGIC_PITSTOP_HOST": "pitstop.internal",
                "IMAGE_MAGIC_PITSTOP_USERNAME": "operator",
                "IMAGE_MAGIC_PITSTOP_CLI_PATH": r"C:\\PitStop\\PitStopServerCLI.exe",
                "IMAGE_MAGIC_PITSTOP_PROFILE_DIGITAL": r"C:\\Profiles\\digital.ppp",
                "IMAGE_MAGIC_PITSTOP_COMMAND_TIMEOUT_SECONDS": "75",
            },
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.pitstop_enabled)
        self.assertEqual(settings.pitstop_host, "pitstop.internal")
        self.assertEqual(settings.pitstop_command_timeout_seconds, 75)
        self.assertEqual(settings.pitstop_profiles[0][0], "digital")

    def test_pitstop_shared_roots_default_to_current_linux_smb_mapping(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(str(settings.pitstop_mac_shared_root), "/mnt/shared")
        self.assertEqual(
            settings.pitstop_windows_shared_root,
            "//192.168.88.100/shared",
        )

    def test_ink_coverage_flag_and_thresholds_default_to_disabled_and_profile(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertFalse(settings.callas_inkcoverage_enabled)
        self.assertIsNone(settings.ink_min_percent)
        self.assertIsNone(settings.ink_max_percent)

    def test_ink_coverage_flag_and_thresholds_are_read_from_environment(self):
        with patch.dict(
            os.environ,
            {
                "IMAGE_MAGIC_CALLAS_INKCOVERAGE_ENABLED": "true",
                "IMAGE_MAGIC_INK_MIN_PERCENT": "15",
                "IMAGE_MAGIC_INK_MAX_PERCENT": "275,5",
            },
        ):
            settings = Settings.from_env()

        self.assertTrue(settings.callas_inkcoverage_enabled)
        self.assertEqual(settings.ink_min_percent, 15.0)
        self.assertEqual(settings.ink_max_percent, 275.5)

    def test_invalid_ink_threshold_has_clear_error(self):
        with patch.dict(os.environ, {"IMAGE_MAGIC_INK_MAX_PERCENT": "много"}):
            with self.assertRaisesRegex(ValueError, "IMAGE_MAGIC_INK_MAX_PERCENT"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()


class PrepressSettingsTests(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for name in ("IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD", "IMAGE_MAGIC_HIGH_PPI_LIMIT",
                         "IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED", "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE"):
                os.environ.pop(name, None)
            settings = Settings.from_env()
        self.assertEqual(settings.black_solid_area_threshold, 0.25)
        self.assertEqual(settings.high_ppi_limit, 500.0)
        self.assertTrue(settings.callas_prepress_enabled)
        self.assertEqual(
            settings.callas_rgb_to_cmyk_profile, "Image Magic/NEW_Convert color to 4c for digital printing"
        )

    def test_overrides_and_bad_values(self):
        with patch.dict(os.environ, {
            "IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD": "0,4",
            "IMAGE_MAGIC_HIGH_PPI_LIMIT": "600",
            "IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED": "false",
            "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE": "Convert colors/Old",
        }):
            settings = Settings.from_env()
        self.assertEqual((settings.black_solid_area_threshold, settings.high_ppi_limit), (0.4, 600.0))
        self.assertFalse(settings.callas_prepress_enabled)
        self.assertEqual(settings.callas_rgb_to_cmyk_profile, "Convert colors/Old")
        with patch.dict(os.environ, {"IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD": "abc", "IMAGE_MAGIC_HIGH_PPI_LIMIT": "5"}):
            settings = Settings.from_env()
        self.assertEqual(settings.black_solid_area_threshold, 0.25)
        self.assertEqual(settings.high_ppi_limit, 72.0)  # нижняя граница
