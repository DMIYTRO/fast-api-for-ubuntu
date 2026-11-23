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
        # Светлые участки: флаг от половины площади страницы, ниже 3 % — белая бумага.
        self.assertEqual(settings.ink_light_min_area_percent, 50.0)
        self.assertEqual(settings.ink_light_floor_percent, 3.0)

    def test_ink_light_area_settings_are_read_from_environment(self):
        with patch.dict(
            os.environ,
            {"IMAGE_MAGIC_INK_LIGHT_MIN_AREA_PERCENT": "60", "IMAGE_MAGIC_INK_LIGHT_FLOOR_PERCENT": "2"},
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.ink_light_min_area_percent, 60.0)
        self.assertEqual(settings.ink_light_floor_percent, 2.0)

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
            for name in ("IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD", "IMAGE_MAGIC_BLACK_SOLID_FIX_AREA_THRESHOLD",
                         "IMAGE_MAGIC_HIGH_PPI_LIMIT",
                         "IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED", "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE"):
                os.environ.pop(name, None)
            settings = Settings.from_env()
        self.assertEqual(settings.black_solid_area_threshold, 0.25)
        self.assertEqual(settings.black_solid_fix_area_threshold, 0.01)
        self.assertEqual(settings.high_ppi_limit, 500.0)
        self.assertTrue(settings.callas_prepress_enabled)
        self.assertEqual(
            settings.callas_rgb_to_cmyk_profile, "Image Magic/NEW_Convert color to 4c for digital printing"
        )

    def test_downsample_profile_default_and_override(self):
        with patch.dict(os.environ, {"IMAGE_MAGIC_CALLAS_DOWNSAMPLE_PROFILE": ""}):
            self.assertEqual(
                Settings.from_env().callas_downsample_profile,
                "PDF fixups/Downsample image resolution to specified value",
            )
        with patch.dict(os.environ, {"IMAGE_MAGIC_CALLAS_DOWNSAMPLE_PROFILE": " Image Magic/Downsample "}):
            self.assertEqual(Settings.from_env().callas_downsample_profile, "Image Magic/Downsample")

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


class PitStopFixBaselineSettingsTests(unittest.TestCase):
    NAMES = (
        "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ENABLED",
        "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS",
        "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_PROFILES",
        "IMAGE_MAGIC_PITSTOP_BLEED_FIX_ALL_ACTIONS",
    )

    def test_defaults_compare_clipping_and_transparency_for_digital(self):
        environment = {key: value for key, value in os.environ.items() if key not in self.NAMES}
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.pitstop_fix_baseline_enabled)
        self.assertEqual(settings.pitstop_fix_baseline_actions, frozenset({"2079", "3004"}))
        self.assertEqual(settings.pitstop_fix_baseline_profiles, frozenset({"digital"}))
        self.assertTrue(settings.pitstop_bleed_fix_all_actions)

    def test_overrides_are_trimmed_and_blank_means_default(self):
        with patch.dict(os.environ, {
            "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ENABLED": "false",
            "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS": " 2079, ,1012 ",
            "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_PROFILES": "digital,offset",
            "IMAGE_MAGIC_PITSTOP_BLEED_FIX_ALL_ACTIONS": "false",
        }):
            settings = Settings.from_env()
        self.assertFalse(settings.pitstop_fix_baseline_enabled)
        self.assertFalse(settings.pitstop_bleed_fix_all_actions)
        self.assertEqual(settings.pitstop_fix_baseline_actions, frozenset({"2079", "1012"}))
        self.assertEqual(settings.pitstop_fix_baseline_profiles, frozenset({"digital", "offset"}))
        with patch.dict(os.environ, {"IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS": "  "}):
            self.assertEqual(
                Settings.from_env().pitstop_fix_baseline_actions, frozenset({"2079", "3004"})
            )

    def test_create_app_wires_the_policy_into_the_order_workflow(self):
        import tempfile
        from dataclasses import replace
        from pathlib import Path

        import control_panel

        with tempfile.TemporaryDirectory() as directory:
            settings = replace(
                Settings.from_env(),
                database_url=f"sqlite:///{Path(directory) / 'state.db'}",
                log_dir=Path(directory) / "logs",
                pitstop_fix_baseline_enabled=False,
                pitstop_fix_baseline_actions=frozenset({"3004"}),
                pitstop_fix_baseline_profiles=frozenset({"offset"}),
                pitstop_bleed_fix_all_actions=False,
            )
            app = control_panel.create_app(
                settings=settings,
                allowed_roots=(Path(directory),),
                default_input_dir=Path(directory),
                initialize_schema=True,
            )
            policy = app.state.order_workflow.pitstop_fix_baseline
            self.assertEqual(
                (policy.enabled, policy.actions, policy.profiles, policy.bleed_fix_all_actions),
                (False, frozenset({"3004"}), frozenset({"offset"}), False),
            )
            app.state.database.dispose()
