import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from argon2 import PasswordHasher
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("argon2-cffi is not installed") from exc

from sqlalchemy import event, select

from server.auth import LAST_SEEN_REFRESH_SECONDS, AuthService
from server.database import Database
from server.models import SessionRecord
from server.settings import Settings


class LastSeenThrottleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.tempdir.name) / 'auth.db'}")
        self.database.create_schema()
        self.hash = PasswordHasher().hash("correct horse")
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        settings = Settings(
            database_url="sqlite://",
            password_hash=self.hash,
            session_hours=1,
            login_failure_delay_seconds=0,
        )
        self.service = AuthService(
            settings,
            password_hash_provider=lambda: self.hash,
            clock=lambda: self.now,
            sleeper=lambda _: None,
        )
        self.writes = []
        event.listen(self.database.engine, "before_cursor_execute", self._capture)

    def tearDown(self):
        self.database.dispose()
        self.tempdir.cleanup()

    def _capture(self, _conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            self.writes.append(statement)

    def _stored_last_seen(self, db, record_id):
        value = db.scalar(select(SessionRecord.last_seen_at).where(SessionRecord.id == record_id))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    def test_fresh_session_does_not_write_last_seen(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(db, "correct horse", "c")
            db.commit()
            self.writes.clear()
            for _ in range(5):
                self.now += timedelta(seconds=5)
                self.assertIsNotNone(self.service.validate(db, token))
                db.commit()
            self.assertEqual(self.writes, [])
            self.assertEqual(
                self._stored_last_seen(db, record.id),
                datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

    def test_stale_session_refreshes_last_seen_once(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(db, "correct horse", "c")
            db.commit()
            self.writes.clear()
            self.now += timedelta(seconds=LAST_SEEN_REFRESH_SECONDS + 1)
            self.assertIsNotNone(self.service.validate(db, token))
            db.commit()
            self.assertEqual(len(self.writes), 1)
            self.assertEqual(self._stored_last_seen(db, record.id), self.now)
            # The next request right after is fresh again: no further write.
            self.now += timedelta(seconds=1)
            self.assertIsNotNone(self.service.validate(db, token))
            db.commit()
            self.assertEqual(len(self.writes), 1)

    def test_expiry_is_independent_of_last_seen_staleness(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(db, "correct horse", "c")
            db.commit()
            # Valid, with last_seen_at up to the threshold behind.
            self.now += timedelta(seconds=LAST_SEEN_REFRESH_SECONDS - 1)
            self.assertIsNotNone(self.service.validate(db, token))
            # Expiry is decided by the fixed expires_at, not by last_seen_at.
            self.now = record.expires_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
            self.assertIsNone(self.service.validate(db, token))
            self.assertIsNotNone(record.revoked_at)

    def test_session_stays_valid_just_before_expiry_even_if_never_refreshed(self):
        with self.database.session_factory() as db:
            token, record = self.service.authenticate(db, "correct horse", "c")
            db.commit()
            self.now = record.expires_at.replace(tzinfo=timezone.utc) - timedelta(seconds=1)
            self.assertIsNotNone(self.service.validate(db, token))
