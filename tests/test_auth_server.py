import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from argon2 import PasswordHasher
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("argon2-cffi is not installed") from exc

from server.auth import AuthService
from server.database import Database
from server.settings import Settings


class ServerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(
            f"sqlite:///{Path(self.tempdir.name) / 'auth.db'}"
        )
        self.database.create_schema()
        self.hashes = [PasswordHasher().hash("correct horse")]
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        settings = Settings(
            database_url="sqlite://",
            password_hash=self.hashes[0],
            login_attempts=2,
            login_failure_delay_seconds=0,
        )
        self.service = AuthService(
            settings,
            password_hash_provider=lambda: self.hashes[0],
            clock=lambda: self.now,
            sleeper=lambda _: None,
        )

    def tearDown(self):
        self.database.dispose()
        self.tempdir.cleanup()

    def test_token_is_hashed_and_password_change_revokes_session(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(
                db, "correct horse", "client"
            )
            db.commit()
            self.assertNotEqual(record.token_hash, token)
            self.assertNotIn("correct horse", record.token_hash)
            self.assertIsNotNone(self.service.validate(db, token))

            self.hashes[0] = PasswordHasher().hash("new password")
            self.assertIsNone(self.service.validate(db, token))
            self.assertIsNotNone(record.revoked_at)

    def test_expired_session_is_rejected(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(
                db, "correct horse", "client"
            )
            db.commit()
            self.now = record.expires_at + timedelta(seconds=1)
            self.assertIsNone(self.service.validate(db, token))

    def test_wrong_password_and_rate_limit_have_identical_error(self):
        messages = []
        with self.database.session_factory() as db:
            for _ in range(3):
                with self.assertRaises(Exception) as caught:
                    self.service.authenticate(db, "wrong", "client")
                messages.append(str(caught.exception))
        self.assertEqual(len(set(messages)), 1)
