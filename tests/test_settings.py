import os
import unittest
from unittest.mock import patch

from argon2 import PasswordHasher

from server.settings import FIXED_ADMIN_PASSWORD_HASH, Settings


class SettingsTests(unittest.TestCase):
    def test_admin_password_is_fixed_even_when_environment_overrides_it(self):
        with patch.dict(
            os.environ,
            {"IMAGE_MAGIC_PASSWORD_HASH": "must-not-override-fixed-password"},
        ):
            settings = Settings.from_env()

        self.assertEqual(settings.password_hash, FIXED_ADMIN_PASSWORD_HASH)
        self.assertTrue(PasswordHasher().verify(settings.password_hash, "1111"))


if __name__ == "__main__":
    unittest.main()
