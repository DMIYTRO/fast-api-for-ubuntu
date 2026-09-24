from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from server.database import Database
from server.models import (
    ArchivedRunEvent,
    CheckRun,
    CorrectionDecision,
    FileResult,
    OrderAction,
    OrderResult,
    PdfRevision,
    PitstopCheck,
    PitstopIssue,
)
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
        file_result_id = actual["orders"]["25506185"]["files"][0].pop(
            "file_result_id"
        )
        self.assertGreater(file_result_id, 0)
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

    def test_round_trip_preserves_postpress_metadata(self):
        run = sample_run()
        run["orders"]["25506185"]["postpress"] = {
            "raw": "Сгиб: 2 намотка",
            "fold": {
                "type": "c-fold",
                "count": 2,
                "needs_confirmation": False,
            },
        }
        self.repository.create_run(run)

        self.assertEqual(
            self.repository.get_run("run-1")["orders"]["25506185"]["postpress"],
            run["orders"]["25506185"]["postpress"],
        )

    def test_round_trip_persists_current_pdf_and_latest_pitstop_check(self):
        expected = sample_run(status="completed")
        order = expected["orders"]["25506185"]
        order.update(
            {
                "status": "error",
                "source_status": "passed",
                "pitstop_status": "error",
                "workflow_status": "active",
                "pdf_path": "/orders/input/PDF/order.pdf",
                "current_pdf_revision": 2,
                "current_pdf_sha256": "a" * 64,
                "pitstop": {
                    "check_id": "pitstop-check-2",
                    "execution_status": "completed",
                    "verdict": "error",
                    "checked_at": "2026-07-25T10:00:08+00:00",
                    "checked_revision": 2,
                    "profile": {
                        "key": "sborka",
                        "name": "Sborka 2Corel",
                        "version": "3",
                    },
                    "pages": 2,
                    "counts": {
                        "errors": 1,
                        "warnings": 1,
                        "fixes": 0,
                        "critical_failures": 0,
                        "noncritical_failures": 0,
                        "informations": 2,
                    },
                    "issues": [
                        {
                            "id": "issue-1",
                            "fingerprint": "font:not-embedded:1",
                            "severity": "error",
                            "action_id": "1012",
                            "message": "Шрифт не встроен",
                            "occurrences": 1,
                            "locations": [
                                {
                                    "page": 1,
                                    "bbox": [10.0, 20.0, 30.0, 40.0],
                                    "units": "pt",
                                }
                            ],
                        }
                    ],
                    "reports": {
                        "json_url": "/api/reports/check-2.json",
                        "xml_url": "/api/reports/check-2.xml",
                    },
                },
            }
        )

        self.repository.create_run(expected)
        actual = self.repository.get_run("run-1")
        actual["orders"]["25506185"]["files"][0].pop("file_result_id")
        self.assertEqual(actual, expected)

        with self.database.session_factory() as session:
            revision = session.scalar(select(PdfRevision))
            self.assertEqual(revision.revision_number, 2)
            self.assertTrue(revision.is_current)
            self.assertEqual(revision.sha256, "a" * 64)
            self.assertEqual(
                session.scalar(select(func.count()).select_from(PitstopCheck)), 1
            )
            issue = session.scalar(select(PitstopIssue))
            self.assertEqual(issue.issue_id, "issue-1")
            self.assertEqual(json.loads(issue.locations_json)[0]["page"], 1)

    def test_new_pdf_revision_preserves_history_and_switches_current(self):
        run = sample_run(status="completed")
        order = run["orders"]["25506185"]
        order.update(
            {
                "pdf_path": "/orders/input/PDF/order.pdf",
                "current_pdf_revision": 1,
                "current_pdf_sha256": "a" * 64,
            }
        )
        self.repository.create_run(run)
        stored = self.repository.get_run("run-1")
        stored_order = stored["orders"]["25506185"]
        stored_order["current_pdf_revision"] = 2
        stored_order["current_pdf_sha256"] = "b" * 64
        self.repository.save_run(stored)

        with self.database.session_factory() as session:
            revisions = list(
                session.scalars(
                    select(PdfRevision).order_by(PdfRevision.revision_number)
                )
            )
            self.assertEqual([item.revision_number for item in revisions], [1, 2])
            self.assertEqual([item.is_current for item in revisions], [False, True])

    def test_pending_pitstop_fields_round_trip_without_a_pdf(self):
        expected = sample_run()
        expected["orders"]["25506185"].update(
            {
                "source_status": "waiting_confirmation",
                "pitstop_status": "not_checked",
                "workflow_status": "active",
                "current_pdf_revision": None,
                "current_pdf_sha256": None,
                "pitstop": None,
            }
        )
        self.repository.create_run(expected)

        actual = self.repository.get_run("run-1")
        actual["orders"]["25506185"]["files"][0].pop("file_result_id")
        self.assertEqual(actual, expected)

    def test_save_updates_rows_without_duplicating_orders_or_files(self):
        self.repository.create_run(sample_run())
        run = self.repository.get_run("run-1")
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

    def test_path_transition_keeps_file_row_and_correction_decision(self):
        run = sample_run(status="completed")
        run["orders"]["25506185"]["status"] = "passed"
        self.repository.create_run(run)
        stored = self.repository.get_run("run-1")
        file_dto = stored["orders"]["25506185"]["files"][0]
        original_id = file_dto["file_result_id"]
        audit = {
            "file_result_id": original_id,
            "path": file_dto["path"],
            "decision": "confirmed",
            "original": {"resample_decision": "ask_confirmation"},
            "proposed": {"target_mm": [92.0, 52.0]},
        }
        self.repository.save_run_with_event(
            stored,
            "order.correction_confirmed",
            {"order_id": "25506185", "decisions": [audit]},
        )
        coordinator = RunCoordinator(self.repository, autostart=False)

        transitioned = coordinator.apply_file_transition(
            "run-1",
            "25506185",
            status="accepted_for_print",
            source_paths={
                "/orders/input/order-face.jpg":
                    "/orders/Processed/order-face.jpg"
            },
            pdf_path=None,
            preview_paths=[],
        )

        transitioned_file = transitioned["orders"]["25506185"]["files"][0]
        self.assertEqual(transitioned_file["file_result_id"], original_id)
        self.assertEqual(
            transitioned_file["path"], "/orders/Processed/order-face.jpg"
        )
        with self.database.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(FileResult)), 1
            )
            decision = session.scalar(select(CorrectionDecision))
            self.assertIsNotNone(decision)
            self.assertEqual(decision.file_result_id, original_id)

    def test_file_result_id_from_another_order_is_rejected(self):
        first = sample_run("run-1", "completed")
        second = sample_run("run-2", "completed")
        second["orders"]["25506185"]["files"][0]["path"] = (
            "/orders/input/second-face.jpg"
        )
        self.repository.create_run(first)
        self.repository.create_run(second)
        first_stored = self.repository.get_run("run-1")
        second_stored = self.repository.get_run("run-2")
        foreign_id = second_stored["orders"]["25506185"]["files"][0][
            "file_result_id"
        ]
        first_file = first_stored["orders"]["25506185"]["files"][0]
        original_id = first_file["file_result_id"]
        original_path = first_file["path"]
        first_file["file_result_id"] = foreign_id
        first_file["path"] = "/orders/Processed/order-face.jpg"

        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.repository.save_run(first_stored)

        unchanged = self.repository.get_run("run-1")
        unchanged_file = unchanged["orders"]["25506185"]["files"][0]
        self.assertEqual(unchanged_file["file_result_id"], original_id)
        self.assertEqual(unchanged_file["path"], original_path)

    def test_file_extras_follow_stable_ids_when_files_are_reordered(self):
        run = sample_run()
        order = run["orders"]["25506185"]
        first = order["files"][0]
        first["resample_reason"] = "first-reason"
        first["parsed"]["side"] = "face"
        second = deepcopy(first)
        second.update(
            {
                "path": "/orders/input/order-back.jpg",
                "name": "order-back.jpg",
                "resample_reason": "second-reason",
                "resample_scale": 2.0,
            }
        )
        second["parsed"]["side"] = "back"
        order["files"].append(second)
        order["preview_paths"].append(
            "/orders/input/Previews/order-back.png"
        )
        self.repository.create_run(run)
        stored = self.repository.get_run("run-1")
        stored_files = stored["orders"]["25506185"]["files"]
        expected_extras = {
            item["file_result_id"]: (
                item["parsed"]["side"],
                item["resample_reason"],
                item["resample_scale"],
            )
            for item in stored_files
        }
        moved_paths = {
            item["file_result_id"]: (
                f"/orders/Processed/{Path(item['path']).name}"
            )
            for item in stored_files
        }
        stored_files.reverse()
        for item in stored_files:
            item["path"] = moved_paths[item["file_result_id"]]
        stored["orders"]["25506185"]["preview_paths"].reverse()

        self.repository.save_run(stored)

        reloaded_files = self.repository.get_run("run-1")["orders"][
            "25506185"
        ]["files"]
        self.assertEqual(
            {item["file_result_id"] for item in reloaded_files},
            set(expected_extras),
        )
        for item in reloaded_files:
            file_id = item["file_result_id"]
            self.assertEqual(item["path"], moved_paths[file_id])
            self.assertEqual(
                (
                    item["parsed"]["side"],
                    item["resample_reason"],
                    item["resample_scale"],
                ),
                expected_extras[file_id],
            )

    def test_nonexistent_file_result_id_is_rejected(self):
        self.repository.create_run(sample_run())
        stored = self.repository.get_run("run-1")
        stored["orders"]["25506185"]["files"][0][
            "file_result_id"
        ] = 999_999

        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.repository.save_run(stored)

    def test_duplicate_file_result_id_in_one_order_is_rejected(self):
        self.repository.create_run(sample_run())
        stored = self.repository.get_run("run-1")
        duplicate = deepcopy(stored["orders"]["25506185"]["files"][0])
        duplicate["path"] = "/orders/input/duplicate.jpg"
        stored["orders"]["25506185"]["files"].append(duplicate)

        with self.assertRaisesRegex(ValueError, "used more than once"):
            self.repository.save_run(stored)

    def test_legacy_file_dto_without_id_still_matches_by_path(self):
        self.repository.create_run(sample_run())
        stored = self.repository.get_run("run-1")
        file_dto = stored["orders"]["25506185"]["files"][0]
        original_id = file_dto.pop("file_result_id")
        file_dto["resample_reason"] = "legacy-updated"

        self.repository.save_run(stored)

        reloaded = self.repository.get_run("run-1")["orders"]["25506185"][
            "files"
        ][0]
        self.assertEqual(reloaded["file_result_id"], original_id)
        self.assertEqual(reloaded["resample_reason"], "legacy-updated")

    def test_legacy_list_file_extras_envelope_is_still_read(self):
        self.repository.create_run(sample_run())
        with self.database.session_factory() as session, session.begin():
            record = session.get(CheckRun, "run-1")
            envelope = json.loads(record.options_json)
            file_extras = envelope["__run_repository__"]["file_extras"][
                "25506185"
            ]
            record.options_json = json.dumps(
                {
                    **envelope,
                    "__run_repository__": {
                        **envelope["__run_repository__"],
                        "file_extras": {
                            "25506185": list(file_extras.values())
                        },
                    },
                }
            )

        reloaded = self.repository.get_run("run-1")["orders"]["25506185"][
            "files"
        ][0]
        self.assertEqual(reloaded["resample_reason"], "crop")
        self.assertEqual(reloaded["parsed"]["side"], "face")

    def test_correction_audit_uses_id_when_event_path_is_stale(self):
        self.repository.create_run(sample_run(status="waiting_confirmation"))
        changed = self.repository.get_run("run-1")
        file_dto = changed["orders"]["25506185"]["files"][0]
        file_id = file_dto["file_result_id"]
        stale_path = file_dto["path"]
        file_dto["path"] = "/orders/Processed/order-face.jpg"

        self.repository.save_run_with_event(
            changed,
            "order.correction_confirmed",
            {
                "order_id": "25506185",
                "decisions": [
                    {
                        "file_result_id": file_id,
                        "path": stale_path,
                        "decision": "confirmed",
                        "original": {},
                        "proposed": {},
                    }
                ],
            },
        )

        with self.database.session_factory() as session:
            decision = session.scalar(select(CorrectionDecision))
            self.assertIsNotNone(decision)
            self.assertEqual(decision.file_result_id, file_id)
            self.assertEqual(
                session.get(FileResult, file_id).source_path,
                "/orders/Processed/order-face.jpg",
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

    def _make_old_run_and_event(self, *, status="completed", run_id="run-1"):
        run = sample_run(run_id, status=status)
        run["finished_at"] = "2025-01-01T00:00:00+00:00"
        self.repository.create_run(run)
        event = self.repository.append_event(run_id, "run.completed", {"x": 1})
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with self.database.session_factory() as session, session.begin():
            session.get(CheckRun, run_id).finished_at = old
            session.get(SqlRunEvent, event.id).created_at = old
        return event

    def test_archives_only_old_events_for_terminal_runs_and_keeps_sse_replay(self):
        event = self._make_old_run_and_event()
        self._make_old_run_and_event(status="waiting_confirmation", run_id="run-2")
        self._make_old_run_and_event(status="running", run_id="run-3")
        cutoff = datetime(2025, 4, 1, tzinfo=timezone.utc)

        moved = self.repository.archive_old_terminal_events(cutoff, batch_size=1)

        self.assertEqual(moved, 1)
        with self.database.session_factory() as session:
            self.assertIsNone(session.get(SqlRunEvent, event.id))
            archived = session.get(ArchivedRunEvent, event.id)
            self.assertIsNotNone(archived)
            self.assertEqual(archived.payload_json, '{"x":1}')
            self.assertEqual(session.get(CheckRun, "run-1").status, "completed")
            self.assertEqual(session.get(CheckRun, "run-2").status, "waiting_confirmation")
        replay = self.repository.list_events("run-1")
        self.assertEqual([(item.id, item.type, item.data) for item in replay],
                         [(event.id, "run.completed", {"x": 1})])
        self.assertEqual(self.repository.list_events("run-1", after_id=event.id), [])
        self.assertEqual(len(self.repository.list_events("run-2")), 1)
        self.assertEqual(len(self.repository.list_events("run-3")), 1)

    def test_event_archiving_is_idempotent_and_respects_event_cutoff(self):
        event = self._make_old_run_and_event()
        cutoff = datetime(2025, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(self.repository.archive_old_terminal_events(cutoff), 1)
        self.assertEqual(self.repository.archive_old_terminal_events(cutoff), 0)
        self.assertEqual(self.repository.list_events("run-1")[0].id, event.id)

        newer_event = self.repository.append_event("run-1", "late.update", {"v": 2})
        self.assertEqual(self.repository.archive_old_terminal_events(cutoff), 0)
        self.assertEqual([item.id for item in self.repository.list_events("run-1")],
                         [event.id, newer_event.id])

    def test_event_archive_insert_failure_rolls_back_without_losing_event(self):
        event = self._make_old_run_and_event()
        with self.database.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TRIGGER fail_archive BEFORE INSERT ON archived_run_events "
                "BEGIN SELECT RAISE(ABORT, 'archive blocked'); END"
            )
        with self.assertRaises(DBAPIError):
            self.repository.archive_old_terminal_events(
                datetime(2025, 4, 1, tzinfo=timezone.utc)
            )
        with self.database.session_factory() as session:
            self.assertIsNotNone(session.get(SqlRunEvent, event.id))
            self.assertIsNone(session.get(ArchivedRunEvent, event.id))
        self.assertEqual(self.repository.list_events("run-1")[0].id, event.id)

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

    def test_restart_fails_pending_action_without_guessing_filesystem_state(self):
        run = sample_run(status="completed")
        run["orders"]["25506185"]["status"] = "passed"
        self.repository.create_run(run)
        with self.database.session_factory() as session, session.begin():
            order = session.scalar(select(OrderResult))
            session.add(
                OrderAction(
                    order_result_id=order.id,
                    action="print",
                    status="pending",
                )
            )

        SqlRunRepository(self.database.session_factory)

        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")
            recovery = action.cms_response_json
            self.assertIn("application_restart", recovery)
            self.assertIn('"filesystem_reconciled":false', recovery)

    def test_restart_promotes_pending_action_if_order_transition_was_persisted(self):
        run = sample_run(status="completed")
        run["orders"]["25506185"]["status"] = "accepted_for_print"
        input_dir = Path(self.temporary_directory.name) / "orders"
        preview_dir = input_dir / "Previews"
        preview_dir.mkdir(parents=True)
        source_path = input_dir / "order-face.jpg"
        pdf_path = input_dir / "order.pdf"
        preview_path = preview_dir / "order-face.png"
        source_path.write_text("source")
        pdf_path.write_text("pdf")
        preview_path.write_text("preview")
        run["orders"]["25506185"]["files"][0]["path"] = str(source_path)
        run["orders"]["25506185"]["pdf_path"] = str(pdf_path)
        run["orders"]["25506185"]["preview_paths"] = [str(preview_path)]
        self.repository.create_run(run)
        with self.database.session_factory() as session, session.begin():
            order = session.scalar(select(OrderResult))
            session.add(
                OrderAction(
                    order_result_id=order.id,
                    action="print",
                    status="pending",
                )
            )

        SqlRunRepository(self.database.session_factory)

        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "prepared")
            self.assertIn(
                "persisted_transition_completed", action.cms_response_json
            )

    def test_restart_does_not_promote_pending_action_when_files_are_missing(self):
        run = sample_run(status="completed")
        run["orders"]["25506185"]["status"] = "accepted_for_print"
        input_dir = Path(self.temporary_directory.name) / "orders"
        preview_dir = input_dir / "Previews"
        preview_dir.mkdir(parents=True)
        source_path = input_dir / "order-face.jpg"
        pdf_path = input_dir / "order.pdf"
        source_path.write_text("source")
        pdf_path.write_text("pdf")
        run["orders"]["25506185"]["files"][0]["path"] = str(source_path)
        run["orders"]["25506185"]["pdf_path"] = str(pdf_path)
        run["orders"]["25506185"]["preview_paths"] = [
            str(preview_dir / "missing-preview.png")
        ]
        self.repository.create_run(run)
        with self.database.session_factory() as session, session.begin():
            order = session.scalar(select(OrderResult))
            session.add(
                OrderAction(
                    order_result_id=order.id,
                    action="print",
                    status="pending",
                )
            )

        SqlRunRepository(self.database.session_factory)

        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")
            recovery = action.cms_response_json
            self.assertIn('"filesystem_reconciled":false', recovery)

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

    def test_orders_page_loads_only_requested_rows_and_searches_server_side(self):
        run = sample_run("paged-run")
        template = deepcopy(next(iter(run["orders"].values())))
        run["orders"] = {}
        for index in range(23):
            order = deepcopy(template)
            order_id = f"{2000 + index:05d}"
            order["order_id"] = order_id
            order["customer_id"] = "customer"
            order["status"] = "warning" if index % 2 else "passed"
            order["files"][0]["name"] = f"layout-{index:02d}.jpg"
            order["files"][0]["path"] = f"/orders/input/layout-{index:02d}.jpg"
            run["orders"][f"customer:{order_id}"] = order
        self.repository.create_run(run)

        page = self.repository.list_orders_page(
            "paged-run", page=2, page_size=10, status="all", search="", active_only=True
        )
        self.assertEqual(page["total"], 23)
        self.assertEqual(len(page["items"]), 10)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["counts"]["warning"], 11)
        self.assertEqual(page["counts"]["passed"], 23)

        searched = self.repository.list_orders_page(
            "paged-run", page=1, page_size=10, status="all", search="layout-17", active_only=True
        )
        self.assertEqual(searched["total"], 1)
        self.assertEqual(searched["items"][0]["order_id"], "02017")

    def test_orders_page_order_stays_stable_when_new_lower_sorting_order_is_added(self):
        run = sample_run("stable-page-run")
        template = deepcopy(next(iter(run["orders"].values())))
        run["orders"] = {}
        first = deepcopy(template)
        first["customer_id"] = "customer-z"
        first["order_id"] = "900"
        second = deepcopy(template)
        second["customer_id"] = "customer-a"
        second["order_id"] = "100"
        run["orders"] = {"first": first, "second": second}
        self.repository.create_run(run)

        page = self.repository.list_orders_page(
            "stable-page-run", page=1, page_size=10, status="all", search="", active_only=True
        )

        self.assertEqual([order["order_id"] for order in page["items"]], ["900", "100"])

    def test_run_summary_omits_orders(self):
        self.repository.create_run(sample_run("summary-run"))
        summary = self.repository.get_run_summary("summary-run")
        self.assertEqual(summary["id"], "summary-run")
        self.assertEqual(summary["orders"], {})


if __name__ == "__main__":
    unittest.main()
