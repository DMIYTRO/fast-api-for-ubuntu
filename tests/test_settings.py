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


if __name__ == "__main__":
    unittest.main()
