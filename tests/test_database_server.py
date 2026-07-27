import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, select, text

from server.database import Database, upgrade_database
from server.models import CheckRun, OrderAction, OrderResult
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
            with database.engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    "0003_active_order_action",
                )
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
            with reopened.engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    "0003_active_order_action",
                )
            reopened.dispose()

    def test_active_action_migration_normalizes_legacy_pending_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'legacy-actions.db'}"
            database = Database(database_url)
            database.create_schema()
            with database.engine.begin() as connection:
                connection.exec_driver_sql(
                    "DROP INDEX uq_order_actions_one_pending_per_order"
                )
                connection.exec_driver_sql(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO alembic_version VALUES ('0002_order_identity')"
                )
            with database.session_factory() as session, session.begin():
                session.add(
                    CheckRun(
                        id="run-1",
                        input_path=directory,
                        direction="digital",
                        status="completed",
                    )
                )
                session.add(
                    OrderResult(
                        id=1,
                        run_id="run-1",
                        order_id="1001",
                        status="passed",
                    )
                )
                session.add_all(
                    [
                        OrderAction(
                            order_result_id=1,
                            action="print",
                            status="pending",
                        ),
                        OrderAction(
                            order_result_id=1,
                            action="reject",
                            status="pending",
                        ),
                    ]
                )
            database.dispose()

            upgrade_database(Settings(database_url=database_url, password_hash=None))

            reopened = Database(database_url)
            with reopened.session_factory() as session:
                actions = list(session.scalars(select(OrderAction).order_by(OrderAction.id)))
                self.assertEqual(
                    [action.status for action in actions], ["failed", "pending"]
                )
                self.assertIn(
                    "duplicate_pending_normalized",
                    actions[0].cms_response_json,
                )
            indexes = {
                item["name"]
                for item in inspect(reopened.engine).get_indexes("order_actions")
            }
            self.assertIn("uq_order_actions_one_pending_per_order", indexes)
            reopened.dispose()
