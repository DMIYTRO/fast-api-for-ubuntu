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


if __name__ == "__main__":
    unittest.main()
