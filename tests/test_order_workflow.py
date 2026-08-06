from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Event, Lock
import unittest

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.database import Database
from server.models import CheckRun, OrderAction, OrderResult
from services.file_lifecycle import FileLifecycle, FileTransition
from services.order_workflow import OrderActionCommand, OrderWorkflowService


class BlockingLifecycle:
    entered = Event()
    release = Event()
    calls: list[str] = []
    calls_guard = Lock()

    def __init__(self, _input_dir: Path) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.entered.clear()
        cls.release.clear()
        cls.calls = []

    @classmethod
    def _transition(cls, action: str) -> FileTransition:
        with cls.calls_guard:
            cls.calls.append(action)
        cls.entered.set()
        if not cls.release.wait(timeout=2):
            raise AssertionError("test did not release lifecycle")
        return FileTransition(
            {"/input/source.jpg": f"/input/{action}/source.jpg"},
            f"/input/{action}/order.pdf",
            [f"/input/{action}/preview.png"],
        )

    def accept_for_print(self, _order):
        return self._transition("print")

    def return_for_rework(self, _order):
        return self._transition("reject")

    def rollback(self, _transition):
        raise AssertionError("rollback should not be needed")


class BrokenLifecycle:
    def __init__(self, _input_dir: Path) -> None:
        raise OSError("cannot initialize lifecycle")


class RollbackLifecycle(BlockingLifecycle):
    def rollback(self, _transition):
        return None


class FakeCoordinator:
    def __init__(self, order):
        self.order = order
        self.transitions = []

    def apply_file_transition(self, _run_id, _order_id, **values):
        self.transitions.append(values["status"])
        self.order["status"] = values["status"]

    def restore_order_snapshot(self, _run_id, _order_id, previous):
        self.order.clear()
        self.order.update(previous)


class OrderWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            f"sqlite:///{Path(self.temporary_directory.name) / 'state.sqlite3'}"
        )
        self.database.create_schema()
        with self.database.session_factory() as session, session.begin():
            session.add(
                CheckRun(
                    id="run-1",
                    input_path=self.temporary_directory.name,
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
                    passed=True,
                )
            )
        self.order = {
            "order_id": "1001",
            "status": "passed",
            "passed": True,
            "files": [{"path": "/input/source.jpg"}],
            "pdf_path": "/input/order.pdf",
            "preview_paths": ["/input/preview.png"],
        }
        self.run = {
            "id": "run-1",
            "options": {"input_path": self.temporary_directory.name},
        }
        self.coordinator = FakeCoordinator(self.order)
        self.service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
        )
        self.command = OrderActionCommand(("1001",), run_id="run-1")
        BlockingLifecycle.reset()

    def tearDown(self):
        BlockingLifecycle.release.set()
        self.database.dispose()
        self.temporary_directory.cleanup()

    def _start_prepare(self, action: str):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.service.prepare, self.command, action)
        self.assertTrue(BlockingLifecycle.entered.wait(timeout=2))
        return executor, future

    def test_repeated_action_is_idempotent_while_first_move_is_pending(self):
        executor, first = self._start_prepare("print")
        try:
            repeated = self.service.prepare(self.command, "print")
            self.assertEqual(
                repeated["items"],
                [
                    {
                        "order_id": "1001",
                        "status": "pending",
                        "idempotent": True,
                        "message": "Это действие над заказом уже выполняется.",
                    }
                ],
            )
            self.assertEqual(BlockingLifecycle.calls, ["print"])
        finally:
            BlockingLifecycle.release.set()
            self.assertEqual(first.result(timeout=2)["items"][0]["status"], "prepared")
            executor.shutdown()

    def test_submit_returns_before_background_file_processing_finishes(self):
        result = self.service.submit(self.command, "reject")

        self.assertEqual(result["items"][0]["status"], "pending")
        self.assertTrue(BlockingLifecycle.entered.wait(timeout=2))
        self.assertEqual(BlockingLifecycle.calls, ["reject"])

        BlockingLifecycle.release.set()
        self.service.shutdown()
        self.assertEqual(self.coordinator.transitions, ["returned_for_rework"])

    def test_conflicting_action_does_not_start_a_second_move(self):
        executor, first = self._start_prepare("print")
        try:
            conflicting = self.service.prepare(self.command, "reject")
            self.assertEqual(conflicting["items"][0]["status"], "conflict")
            self.assertIn("другое действие", conflicting["items"][0]["message"])
            self.assertEqual(BlockingLifecycle.calls, ["print"])
        finally:
            BlockingLifecycle.release.set()
            first.result(timeout=2)
            executor.shutdown()
        self.assertEqual(self.coordinator.transitions, ["accepted_for_print"])

    def test_conflict_is_visible_across_two_service_instances(self):
        executor, first = self._start_prepare("print")
        second_service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
        )
        try:
            conflicting = second_service.prepare(self.command, "reject")
            self.assertEqual(conflicting["items"][0]["status"], "conflict")
            self.assertEqual(BlockingLifecycle.calls, ["print"])
        finally:
            BlockingLifecycle.release.set()
            first.result(timeout=2)
            executor.shutdown()

    def test_completed_print_can_be_followed_by_rework(self):
        BlockingLifecycle.release.set()
        printed = self.service.prepare(self.command, "print")
        returned = self.service.prepare(self.command, "reject")

        self.assertEqual(printed["items"][0]["status"], "prepared")
        self.assertEqual(returned["items"][0]["status"], "prepared")
        self.assertEqual(BlockingLifecycle.calls, ["print", "reject"])
        self.assertEqual(
            self.coordinator.transitions,
            ["accepted_for_print", "returned_for_rework"],
        )

    def test_print_is_rejected_after_rework_without_moving_files(self):
        BlockingLifecycle.release.set()
        self.service.prepare(self.command, "print")
        self.service.prepare(self.command, "reject")
        BlockingLifecycle.release.set()

        repeated = self.service.prepare(self.command, "print")

        self.assertEqual(repeated["items"][0]["status"], "rejected")
        self.assertIn("недопустимый переход", repeated["items"][0]["message"])
        self.assertEqual(BlockingLifecycle.calls, ["print", "reject"])

    def test_error_order_requires_confirmation_before_print(self):
        self.order["status"] = "error"
        BlockingLifecycle.release.set()

        rejected = self.service.prepare(self.command, "print")
        confirmed = self.service.prepare(
            OrderActionCommand(
                ("1001",), run_id="run-1", confirm_failed_processing=True
            ),
            "print",
        )

        self.assertEqual(rejected["items"][0]["status"], "rejected")
        self.assertEqual(confirmed["items"][0]["status"], "prepared")
        self.assertEqual(BlockingLifecycle.calls, ["print"])
        self.assertEqual(self.coordinator.transitions, ["accepted_for_print"])

    def test_reject_is_rejected_after_rework_without_moving_files(self):
        self.order["status"] = "returned_for_rework"

        rejected = self.service.prepare(self.command, "reject")

        self.assertEqual(rejected["items"][0]["status"], "rejected")
        self.assertIn("недопустимый переход", rejected["items"][0]["message"])
        self.assertEqual(BlockingLifecycle.calls, [])
        self.assertEqual(self.coordinator.transitions, [])

    def test_rework_sends_individual_options_and_preview_name_stub(self):
        sent: list[tuple[str, str, str, bool, str]] = []
        preview_path = Path(self.temporary_directory.name) / "preview.png"
        preview_path.write_bytes(b"preview")
        self.order["preview_paths"] = [str(preview_path)]
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
            rework_sender=lambda order_id, comment, preview, design, cost: (
                sent.append((order_id, comment, preview, design, cost))
                or {"http_status": 200, "preview_priem": preview}
            ),
        )
        BlockingLifecycle.release.set()

        result = service.prepare(
            OrderActionCommand(
                ("1001",), run_id="run-1", comment="Неверный размер.", design=False
            ),
            "reject",
        )

        self.assertEqual(result["items"][0]["status"], "prepared")
        self.assertEqual(
            sent, [("1001", "Неверный размер.", "preview.png", False, "0")]
        )

    def test_rework_uses_preview_from_processed_preview_folder(self):
        sent: list[str] = []
        self.order["preview_paths"] = []
        preview_dir = Path(self.temporary_directory.name) / "Previews" / "Processed"
        preview_dir.mkdir(parents=True)
        preview_name = "01_KS_(777-1001)_preview.png"
        (preview_dir / preview_name).write_bytes(b"preview")
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
            rework_sender=lambda _order, _comment, preview, _design, _cost: (
                sent.append(preview) or {"http_status": 200}
            ),
        )
        BlockingLifecycle.release.set()

        result = service.prepare(self.command, "reject")

        self.assertEqual(result["items"][0]["status"], "prepared")
        self.assertEqual(sent, [preview_name])

    def test_rework_continues_without_a_preview(self):
        sent: list[str] = []
        self.order["preview_paths"] = []
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
            rework_sender=lambda _order, _comment, preview, _design, _cost: (
                sent.append(preview) or {"http_status": 200}
            ),
        )
        BlockingLifecycle.release.set()

        result = service.prepare(self.command, "reject")

        self.assertEqual(result["items"][0]["status"], "prepared")
        self.assertEqual(sent, [""])

    def test_uploaded_previews_are_removed_only_by_the_cleanup_step(self):
        preview = Path(self.temporary_directory.name) / "uploaded_preview.png"
        preview.write_bytes(b"preview")

        OrderWorkflowService._remove_uploaded_previews([preview])

        self.assertFalse(preview.exists())

    def test_lifecycle_initialization_failure_marks_action_failed_and_retries(self):
        broken_service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BrokenLifecycle,
        )

        failed = broken_service.prepare(self.command, "print")

        self.assertEqual(failed["items"][0]["status"], "error")
        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")

        BlockingLifecycle.release.set()
        retried = self.service.prepare(self.command, "print")

        self.assertEqual(retried["items"][0]["status"], "prepared")
        self.assertEqual(BlockingLifecycle.calls, ["print"])

    def test_print_conflict_exposes_destination_and_suggested_name(self):
        source = Path(self.temporary_directory.name) / "source.jpg"
        pdf = Path(self.temporary_directory.name) / "PDF" / "source.pdf"
        preview = Path(self.temporary_directory.name) / "Previews" / "source.png"
        conflicted_path = Path(self.temporary_directory.name) / "Processed" / "source.jpg"
        source.write_bytes(b"source")
        pdf.parent.mkdir(parents=True)
        preview.parent.mkdir(parents=True)
        conflicted_path.parent.mkdir(parents=True)
        pdf.write_bytes(b"pdf")
        preview.write_bytes(b"preview")
        conflicted_path.write_bytes(b"existing")
        self.order["files"][0]["path"] = str(source)
        self.order["pdf_path"] = str(pdf)
        self.order["preview_paths"] = [str(preview)]

        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=FileLifecycle,
        )

        result = service.prepare(self.command, "print")

        self.assertEqual(result["items"][0]["status"], "conflict")
        self.assertEqual(
            result["items"][0]["conflict"]["destination_path"],
            str(conflicted_path.resolve()),
        )
        self.assertIn("(новый)", result["items"][0]["conflict"]["suggested_name"])

    def test_print_sends_order_to_prepress_and_stores_response(self):
        BlockingLifecycle.release.set()
        calls = []
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=BlockingLifecycle,
            prepress_sender=lambda order_id, comment: (
                calls.append((order_id, comment))
                or {"http_status": 200, "response": {"ok": True}}
            ),
        )

        result = service.prepare(
            OrderActionCommand(("1001",), run_id="run-1", comment="Проверено"),
            "print",
        )

        self.assertEqual(calls, [("1001", "Проверено")])
        self.assertEqual(result["items"][0]["prepress"]["http_status"], 200)
        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertIn('"ok": true', action.cms_response_json)

    def test_multiple_prints_use_one_prepress_request_without_comment(self):
        BlockingLifecycle.release.set()
        self.order["order_id"] = "1001"
        second_order = dict(self.order, order_id="1002")
        with self.database.session_factory() as session, session.begin():
            session.add(
                OrderResult(
                    id=2,
                    run_id="run-1",
                    order_id="1002",
                    status="passed",
                    passed=True,
                )
            )
        calls = []
        original_finder = lambda order_id, _run_id: (
            self.run,
            second_order if order_id == "1002" else self.order,
        )
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            original_finder,
            lifecycle_factory=BlockingLifecycle,
            prepress_sender=lambda order_ids, comment: (
                calls.append((order_ids, comment))
                or {"http_status": 200, "response": {"ok": True}}
            ),
        )

        result = service.prepare(
            OrderActionCommand(("1001", "1002"), run_id="run-1", comment="не использовать"),
            "print",
        )

        self.assertEqual(calls, [(["1001", "1002"], None)])
        self.assertEqual([item["status"] for item in result["items"]], ["prepared", "prepared"])

    def test_prepress_failure_rolls_back_local_print_transition(self):
        BlockingLifecycle.release.set()
        service = OrderWorkflowService(
            self.coordinator,
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=RollbackLifecycle,
            prepress_sender=lambda _order_id, _comment: (_ for _ in ()).throw(
                RuntimeError("sborka unavailable")
            ),
        )

        result = service.prepare(self.command, "print")

        self.assertEqual(result["items"][0]["status"], "error")
        self.assertEqual(self.order["status"], "passed")
        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction))
            self.assertEqual(action.status, "failed")

    def test_database_rejects_two_pending_actions_even_for_different_actions(self):
        with self.database.session_factory() as session:
            session.add(
                OrderAction(order_result_id=1, action="print", status="pending")
            )
            session.commit()
            session.add(
                OrderAction(order_result_id=1, action="reject", status="pending")
            )
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()
            actions = list(session.scalars(select(OrderAction)))
            self.assertEqual([(item.action, item.status) for item in actions], [
                ("print", "pending")
            ])


if __name__ == "__main__":
    unittest.main()
