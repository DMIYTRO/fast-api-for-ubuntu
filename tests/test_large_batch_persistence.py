"""Regression coverage for large-run persistence work."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import event

from server.database import Database
from services.sql_repository import SqlRunRepository


class LargeBatchPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database_path = Path(self.temporary_directory.name) / "runs.sqlite3"
        self.database = Database(f"sqlite:///{database_path}")
        self.database.create_schema()
        self.repository = SqlRunRepository(
            self.database.session_factory, recover_interrupted=False
        )

    def tearDown(self) -> None:
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_targeted_save_adds_an_order_without_replacing_existing_rows(self) -> None:
        run = {
            "id": "incremental", "status": "running", "stage": "processing",
            "progress": 0, "options": {"input_path": "/orders", "direction": "digital"},
            "created_at": "2026-09-23T00:00:00+00:00", "orders": {},
        }
        self.repository.create_run(run)
        for order_id in ("first", "second"):
            run["orders"][order_id] = {
                "order_id": order_id, "customer_id": "customer",
                "status": "detected", "passed": False, "errors": [],
                "warnings": [], "files": [
                    {"path": f"/orders/{order_id}.tif", "name": f"{order_id}.tif",
                     "errors": [], "warnings": [], "special_metadata": order_id}
                ],
            }
            self.repository.save_run_with_event(
                run, "order.detected", {"order_id": order_id},
                changed_order_keys=(order_id,),
            )
        reloaded = self.repository.get_run("incremental")
        self.assertEqual(set(reloaded["orders"]), {"first", "second"})
        self.assertEqual(
            reloaded["orders"]["first"]["files"][0]["special_metadata"], "first"
        )
        self.assertEqual(
            reloaded["orders"]["second"]["files"][0]["special_metadata"], "second"
        )

    def test_changed_order_save_preserves_other_orders_without_updating_them(self) -> None:
        run = {
            "id": "large-run", "status": "running", "stage": "processing",
            "progress": 0, "options": {"input_path": "/orders", "direction": "digital"},
            "created_at": "2026-09-23T00:00:00+00:00", "orders": {},
        }
        for number in range(120):
            order_id = f"{number:04d}"
            run["orders"][order_id] = {
                "order_id": order_id, "customer_id": "customer", "status": "detected",
                "passed": False, "errors": [], "warnings": [],
                "files": [{"path": f"/orders/{order_id}.tif", "name": f"{order_id}.tif",
                           "errors": [], "warnings": [], "special_metadata": order_id}],
                "custom_metadata": order_id,
            }
        self.repository.create_run(run)
        stored = self.repository.get_run("large-run")
        original = deepcopy(stored["orders"]["0000"])
        stored["orders"]["0119"]["status"] = "passed"
        stored["orders"]["0119"]["custom_metadata"] = "changed"
        changed_updates = []

        def count_order_updates(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("UPDATE ORDER_RESULTS"):
                changed_updates.append(statement)

        event.listen(self.database.engine, "before_cursor_execute", count_order_updates)
        try:
            self.repository.save_run_with_event(
                stored, "order.completed", {"order_id": "0119"},
                changed_order_keys=("0119",),
            )
        finally:
            event.remove(self.database.engine, "before_cursor_execute", count_order_updates)

        reloaded = self.repository.get_run("large-run")
        self.assertEqual(reloaded["orders"]["0000"], original)
        self.assertEqual(reloaded["orders"]["0119"]["status"], "passed")
        self.assertEqual(reloaded["orders"]["0119"]["custom_metadata"], "changed")
        self.assertLessEqual(len(changed_updates), 1)


if __name__ == "__main__":
    unittest.main()
