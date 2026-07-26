import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect

from server.database import Database, upgrade_database
from server.settings import Settings


class ServerDatabaseTests(unittest.TestCase):
    def test_complete_schema_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(f"sqlite:///{Path(directory) / 'test.db'}")
            database.create_schema()
            self.assertEqual(
                set(inspect(database.engine).get_table_names()),
                {
                    "sessions",
                    "check_runs",
                    "order_results",
                    "file_results",
                    "correction_decisions",
                    "run_events",
                    "order_actions",
                },
            )
            database.dispose()

    def test_alembic_upgrade_creates_and_versions_fresh_database(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'migrated.db'}"
            settings = Settings(database_url=database_url, password_hash=None)
            upgrade_database(settings)
            database = Database(database_url)
            tables = set(inspect(database.engine).get_table_names())
            self.assertIn("alembic_version", tables)
            self.assertIn("check_runs", tables)
            database.dispose()

    def test_alembic_safely_baselines_complete_legacy_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'legacy.db'}"
            database = Database(database_url)
            database.create_schema()
            database.dispose()
            upgrade_database(Settings(database_url=database_url, password_hash=None))
            reopened = Database(database_url)
            self.assertIn(
                "alembic_version", inspect(reopened.engine).get_table_names()
            )
            reopened.dispose()
