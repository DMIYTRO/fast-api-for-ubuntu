from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select

from server.database import Database
from server.models import CheckRun, CorrectionDecision, FileResult, OrderResult
from server.models import RunEvent as SqlRunEvent
from services.batch_adapter import ProcessingOptions
from services.coordinator import RunCoordinator
from services.sql_repository import SqlRunRepository


def sample_run(run_id="run-1", status="running"):
    return {
        "id": run_id,
        "status": status,
        "stage": "processing",
        "progress": 55,
        "options": {
            "input_path": "/orders/input",
            "direction": "digital",
            "create_pdfs": True,
            "generate_previews": True,
            "copy_failures": False,
        },
        "created_at": "2026-07-25T10:00:00+00:00",
        "started_at": "2026-07-25T10:00:01+00:00",
        "finished_at": None,
        "cancel_requested": False,
        "error": None,
        "total_orders": 1,
        "processed_orders": 0,
        "passed_orders": 0,
        "warning_orders": 0,
        "failed_orders": 0,
        "waiting_orders": 1,
        "orders": {
            "25506185": {
                "order_id": "25506185",
                "customer_id": "12690",
                "status": "waiting_confirmation",
                "passed": False,
                "pending_confirmations": 1,
                "errors": [],
                "warnings": ["проверьте обрезку"],
                "files": [
                    {
                        "path": "/orders/input/order-face.jpg",
                        "name": "order-face.jpg",
                        "parsed": {
                            "customer_id": "12690",
                            "order_id": "25506185",
                            "width_mm": 90.0,
                            "height_mm": 50.0,
                            "front_colors": 4,
                            "back_colors": 0,
                            "side": "face",
                        },
                        "actual_width_mm": 94.0,
                        "actual_height_mm": 54.0,
                        "dpi": 300.0,
                        "dpi_x": 300.0,
                        "dpi_y": 300.0,
                        "width_px": 1110,
                        "height_px": 638,
                        "actual_format": "JPEG",
                        "colorspace": "CMYK",
                        "errors": [],
                        "warnings": ["требуется подтверждение"],
                        "needs_resample": False,
                        "resample_target_mm": [92.0, 52.0],
                        "resample_decision": "ask_confirmation",
                        "resample_reason": "crop",
                        "resample_scale": 1.0,
                        "resample_crop_mm": [2.0, 2.0],
                        "resample_effective_dpi": [300.0, 300.0],
                        "resample_confirmed": None,
                        "rotation_degrees": 0,
                        "orientation_verified": False,
                        "passed": False,
                    }
                ],
                "pdf_path": None,
                "preview_paths": ["/orders/input/Previews/order-face.png"],
                "processing_errors": [],
            }
        },
    }


class SqlRunRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        db_path = Path(self.temporary_directory.name) / "runs.sqlite3"
        self.database = Database(f"sqlite:///{db_path}")
        self.database.create_schema()
        self.repository = SqlRunRepository(
            self.database.session_factory, recover_interrupted=False
        )

    def tearDown(self):
        self.database.dispose()
        self.temporary_directory.cleanup()

    def test_round_trip_preserves_json_safe_run_order_and_file_dtos(self):
        expected = sample_run()
        self.repository.create_run(expected)

        actual = self.repository.get_run("run-1")
        self.assertEqual(actual, expected)

        # Verify the DTO was not merely stored as one opaque blob.
        with self.database.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(CheckRun)), 1
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(OrderResult)), 1
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(FileResult)), 1
            )
            file_record = session.scalar(select(FileResult))
            self.assertEqual(file_record.source_path, "/orders/input/order-face.jpg")
            self.assertEqual(file_record.status, "waiting_confirmation")

    def test_save_updates_rows_without_duplicating_orders_or_files(self):
        run = sample_run()
        self.repository.create_run(run)
        run["status"] = "completed"
        run["stage"] = "completed"
        run["progress"] = 100
        run["processed_orders"] = 1
        run["waiting_orders"] = 0
        run["warning_orders"] = 1
        run["finished_at"] = "2026-07-25T10:00:10+00:00"
        order = run["orders"]["25506185"]
        order["status"] = "warning"
        order["passed"] = True
        order["pending_confirmations"] = 0
        order["pdf_path"] = "/orders/input/PDF/order.pdf"
        order["files"][0]["resample_decision"] = "auto_correct"
        order["files"][0]["resample_confirmed"] = True
        order["files"][0]["passed"] = True
        self.repository.save_run(run)

        self.assertEqual(self.repository.get_run("run-1"), run)
        with self.database.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(OrderResult)), 1
            )
            self.assertEqual(
                session.scalar(select(func.count()).select_from(FileResult)), 1
            )

    def test_two_customers_can_share_the_same_order_number(self):
        run = sample_run()
        first = run["orders"].pop("25506185")
        first["aggregate_id"] = "12690:25506185"
        second = {
            **first,
            "aggregate_id": "777:25506185",
            "customer_id": "777",
            "files": [
                {
                    **first["files"][0],
                    "path": "/orders/input/other-face.jpg",
                    "name": "other-face.jpg",
                    "parsed": {
                        **first["files"][0]["parsed"],
                        "customer_id": "777",
                    },
                }
            ],
        }
        run["orders"] = {
            first["aggregate_id"]: first,
            second["aggregate_id"]: second,
        }

        self.repository.create_run(run)

        actual = self.repository.get_run("run-1")
        self.assertEqual(set(actual["orders"]), {"12690:25506185", "777:25506185"})

    def test_run_summaries_are_paginated_without_loading_orders(self):
        self.repository.create_run(sample_run("run-1"))
        self.repository.create_run(sample_run("run-2"))
        self.repository.create_run(sample_run("run-3"))

        page = self.repository.list_runs(
            limit=1, offset=1, include_orders=False
        )

        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["orders"], {})
        self.assertEqual(self.repository.count_runs(), 3)

    def test_events_survive_repository_recreation_and_support_last_event_id(self):
        run = sample_run(status="completed")
        self.repository.create_run(run)
        first = self.repository.append_event(
            "run-1", "run.started", {"progress": 0}
        )
        second = self.repository.append_event(
            "run-1", "run.completed", {"progress": 100}
        )

        reopened = SqlRunRepository(
            self.database.session_factory, recover_interrupted=False
        )
        events = reopened.list_events("run-1", after_id=first.id)
        self.assertEqual([item.id for item in events], [second.id])
        self.assertEqual(events[0].data, {"progress": 100})
        self.assertIn(f"id: {second.id}", events[0].as_sse())

    def test_state_and_event_are_rolled_back_together(self):
        run = sample_run(status="running")
        self.repository.create_run(run)
        changed = sample_run(status="completed")

        with self.assertRaises(TypeError):
            self.repository.save_run_with_event(
                changed, "run.completed", {"invalid": object()}
            )

        self.assertEqual(self.repository.get_run("run-1")["status"], "running")
        self.assertEqual(self.repository.list_events("run-1"), [])

    def test_correction_decision_is_audited_with_transition(self):
        run = sample_run(status="waiting_confirmation")
        self.repository.create_run(run)
        changed = sample_run(status="running")
        changed["orders"]["25506185"]["files"][0][
            "resample_decision"
        ] = "auto_correct"
        audit = {
            "path": "/orders/input/order-face.jpg",
            "decision": "confirmed",
            "original": {"resample_decision": "ask_confirmation"},
            "proposed": {"target_mm": [92.0, 52.0]},
        }

        self.repository.save_run_with_event(
            changed,
            "order.correction_confirmed",
            {"order_id": "25506185", "decisions": [audit]},
        )

        with self.database.session_factory() as session:
            record = session.scalar(select(CorrectionDecision))
            self.assertEqual(record.decision, "confirmed")
            self.assertIn("ask_confirmation", record.original_parameters_json)
            self.assertIn("target_mm", record.proposed_parameters_json)

    def test_restart_marks_all_unrecoverable_active_states_failed(self):
        for index, status in enumerate(
            ("queued", "running", "waiting_confirmation", "cancelling"), start=1
        ):
            self.repository.create_run(sample_run(f"run-{index}", status))
        completed = sample_run("completed", "completed")
        completed["finished_at"] = "2026-07-25T10:10:00+00:00"
        self.repository.create_run(completed)

        reopened = SqlRunRepository(self.database.session_factory)

        for index in range(1, 5):
            recovered = reopened.get_run(f"run-{index}")
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["stage"], "interrupted")
            self.assertIn("перезапуском", recovered["error"])
            self.assertIsNotNone(recovered["finished_at"])
            event = reopened.list_events(f"run-{index}")[-1]
            self.assertEqual(event.type, "run.failed")
            self.assertEqual(event.data["reason"], "application_restart")
        self.assertEqual(reopened.get_run("completed")["status"], "completed")
        with self.database.session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(SqlRunEvent)
                    .where(SqlRunEvent.event_type == "run.failed")
                ),
                4,
            )

    def test_malformed_legacy_json_is_read_safely(self):
        with self.database.session_factory() as session, session.begin():
            session.add(
                CheckRun(
                    id="legacy",
                    input_path="/legacy",
                    direction="offset",
                    status="completed",
                    stage="completed",
                    progress=100,
                    options_json="{broken",
                )
            )
        actual = self.repository.get_run("legacy")
        self.assertEqual(
            actual["options"],
            {"input_path": "/legacy", "direction": "offset"},
        )
        self.assertEqual(actual["orders"], {})

    def test_coordinator_can_use_sql_repository_from_worker_thread(self):
        class EmptyAdapter:
            def scan_and_inspect(self):
                return []

        coordinator = RunCoordinator(
            self.repository, adapter_factory=lambda _options: EmptyAdapter()
        )
        try:
            submitted = coordinator.submit(
                ProcessingOptions(
                    input_path="/orders/empty",
                    direction="digital",
                    create_pdfs=False,
                    generate_previews=False,
                    copy_failures=False,
                )
            )
            completed = coordinator.wait_for(
                submitted["id"], {"completed"}, timeout=2
            )
        finally:
            coordinator.shutdown()

        self.assertEqual(completed["status"], "completed")
        reopened = SqlRunRepository(
            self.database.session_factory, recover_interrupted=False
        )
        self.assertEqual(reopened.get_run(submitted["id"])["progress"], 100)
        self.assertEqual(
            [event.type for event in reopened.list_events(submitted["id"])],
            ["run.started", "scan.progress", "run.completed"],
        )


if __name__ == "__main__":
    unittest.main()
