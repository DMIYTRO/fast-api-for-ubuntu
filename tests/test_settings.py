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

    def test_missing_admin_password_hash_stops_startup(self):
        with patch.dict(os.environ, {"IMAGE_MAGIC_PASSWORD_HASH": ""}):
            with self.assertRaisesRegex(RuntimeError, "IMAGE_MAGIC_PASSWORD_HASH"):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
