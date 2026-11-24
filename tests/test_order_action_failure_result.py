"""Неудача «В печать»/«Вернуть» сохраняет итог для карточки (order_actions 346, 367, 380).

Первое нажатие «В печать» для TIFF со слоями создаёт PDF недостающих сторон и
останавливается (``review_required``); раньше в БД оставался только ``failed`` и
карточка после перечитывания показывала зелёное «Действие: failed».
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import select

from server.database import Database
from server.models import CheckRun, OrderAction, OrderResult
from services.file_lifecycle import FileConflictError, FileLifecycleError, FileTransition
from services.layered_tiff_print import ReviewedLayeredPdfError
from services.order_action_result import (
    RESTART_FAILURE_MESSAGE,
    UNKNOWN_FAILURE_MESSAGE,
    stored_action_result,
)
from services.order_journal import build_order_journal
from services.order_workflow import OrderActionCommand, OrderWorkflowService
from services.sql_repository import SqlRunRepository
from tests.test_sql_run_repository import sample_run

LAYERED_MESSAGE = (
    "Созданы PDF для недостающих сторон. Проверьте их в карточке заказа и повторите "
    "отправку в печать."
)


def raising_lifecycle(exc: Exception):
    class _Lifecycle:
        def __init__(self, _input_dir: Path) -> None:
            pass

        def accept_for_print(self, _order, **_kwargs):
            raise exc

        def return_for_rework(self, _order, **_kwargs):
            raise exc

        def rollback(self, _transition):
            return None

    return _Lifecycle


class _OkLifecycle:
    def __init__(self, _input_dir: Path) -> None:
        pass

    def accept_for_print(self, _order, **_kwargs):
        return FileTransition({"/input/source.jpg": "/input/Processed/source.jpg"}, "/input/order.pdf", [])

    def rollback(self, _transition):
        return None


class _Coordinator:
    def __init__(self, order):
        self.order = order

    def apply_file_transition(self, _run_id, _order_id, **values):
        self.order["status"] = values["status"]

    def restore_order_snapshot(self, _run_id, _order_id, previous):
        self.order.clear()
        self.order.update(previous)


class ActionFailureResultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.tmp.name) / 'state.sqlite3'}")
        self.database.create_schema()
        with self.database.session_factory() as session, session.begin():
            session.add(CheckRun(id="run-1", input_path=self.tmp.name, direction="digital", status="completed"))
            session.add(OrderResult(id=1, run_id="run-1", order_id="1001", status="warning", passed=True))
        self.order = {
            "order_id": "1001",
            "status": "warning",
            "passed": True,
            "files": [{"path": "/input/source.jpg"}],
            "pdf_path": "/input/order.pdf",
            "preview_paths": ["/input/preview.png"],
        }
        self.run = {"id": "run-1", "options": {"input_path": self.tmp.name}}
        self.command = OrderActionCommand(("1001",), run_id="run-1")

    def tearDown(self):
        self.database.dispose()
        self.tmp.cleanup()

    def _service(self, lifecycle, **kwargs) -> OrderWorkflowService:
        return OrderWorkflowService(
            _Coordinator(self.order),
            self.database.session_factory,
            lambda _order_id, _run_id: (self.run, self.order),
            lifecycle_factory=lifecycle,
            **kwargs,
        )

    def _stored(self) -> tuple[str, dict]:
        with self.database.session_factory() as session:
            action = session.scalar(select(OrderAction).order_by(OrderAction.id.desc()))
            return action.status, json.loads(action.cms_response_json or "null")

    def test_layered_review_required_is_stored_with_generated_ids(self):
        exc = ReviewedLayeredPdfError(LAYERED_MESSAGE, generated_file_ids=["run-1:1001:1"])
        with self.assertLogs("image_magic.order_workflow", level=logging.WARNING) as logs:
            item = self._service(raising_lifecycle(exc)).prepare(self.command, "print")["items"][0]

        self.assertEqual(item["status"], "review_required")
        self.assertEqual(item["code"], "layered_review_required")
        self.assertEqual(item["generated_layered_file_ids"], ["run-1:1001:1"])
        status, response = self._stored()
        self.assertEqual(status, "failed")
        self.assertEqual(
            response["result"],
            {
                "status": "review_required",
                "code": "layered_review_required",
                "message": LAYERED_MESSAGE,
                "generated_layered_file_ids": ["run-1:1001:1"],
            },
        )
        line = next(record for record in logs.output if "order.action_failed" in record)
        for part in ("run_id=run-1", "order_id=1001", "action=print", "status=review_required", "code=layered_review_required", "Созданы PDF"):
            self.assertIn(part, line)

    def test_layered_failure_without_generated_files_is_an_error(self):
        exc = ReviewedLayeredPdfError("callas недоступен")
        self._service(raising_lifecycle(exc)).prepare(self.command, "print")

        _, response = self._stored()
        self.assertEqual(
            response["result"],
            {"status": "error", "code": "layered_print_failed", "message": "callas недоступен"},
        )

    def test_lifecycle_error_is_stored(self):
        pdf = Path(self.tmp.name) / "order.pdf"
        pdf.write_bytes(b"%PDF-test")
        self.order["pdf_path"] = str(pdf)
        exc = FileLifecycleError("Не найден один или несколько исходных файлов заказа.")
        with self.assertLogs("image_magic.order_workflow", level=logging.WARNING):
            item = self._service(raising_lifecycle(exc)).prepare(self.command, "print")["items"][0]

        self.assertEqual(item["status"], "error")
        status, response = self._stored()
        self.assertEqual(status, "failed")
        self.assertEqual(
            response["result"],
            {"status": "error", "message": "Не найден один или несколько исходных файлов заказа."},
        )

    def test_file_conflict_is_stored(self):
        exc = FileConflictError(Path("/input/source.jpg"), Path("/input/Processed/source.jpg"))
        item = self._service(raising_lifecycle(exc)).prepare(self.command, "print")["items"][0]

        self.assertEqual(item["status"], "conflict")
        _, response = self._stored()
        self.assertEqual(response["result"]["status"], "conflict")
        self.assertEqual(response["result"]["code"], "file_conflict")
        self.assertTrue(response["result"]["message"])

    def test_unexpected_error_is_stored(self):
        item = self._service(raising_lifecycle(RuntimeError("диск недоступен"))).prepare(
            self.command, "reject"
        )["items"][0]

        self.assertEqual(item["status"], "error")
        _, response = self._stored()
        self.assertEqual(
            response["result"],
            {"status": "error", "code": "preparation_failed", "message": "диск недоступен"},
        )

    def test_failure_after_transition_is_stored(self):
        service = self._service(
            _OkLifecycle,
            prepress_sender=lambda _order_id, _comment: (_ for _ in ()).throw(RuntimeError("sborka unavailable")),
        )
        item = service.prepare(self.command, "print")["items"][0]

        self.assertEqual(item["code"], "transition_failed")
        status, response = self._stored()
        self.assertEqual(status, "failed")
        self.assertEqual(response["result"]["message"], "sborka unavailable")

    def test_failure_keeps_previously_stored_details(self):
        with self.database.session_factory() as session:
            record = OrderAction(
                order_result_id=1, action="print", status="pending",
                cms_response_json=json.dumps({"pitstop": {"verdict": "error"}}),
            )
            session.add(record)
            session.commit()
            OrderWorkflowService._store_action_failure(
                session, record,
                {"order_id": "1001", "status": "error", "message": "сбой"},
                run_id="run-1", action="print", extra={"print_build": {"status": "failed"}},
            )
        status, response = self._stored()
        self.assertEqual(status, "failed")
        self.assertEqual(response["pitstop"], {"verdict": "error"})
        self.assertEqual(response["print_build"], {"status": "failed"})
        self.assertEqual(response["result"], {"status": "error", "message": "сбой"})


class StoredActionResultTests(unittest.TestCase):
    def test_review_required_survives_reload_from_repository(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        database = Database(f"sqlite:///{Path(tmp.name) / 'runs.sqlite3'}")
        self.addCleanup(database.dispose)
        database.create_schema()
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        repository.create_run(sample_run())
        result = {
            "status": "review_required",
            "code": "layered_review_required",
            "message": LAYERED_MESSAGE,
            "generated_layered_file_ids": ["run-1:25506185:0"],
        }
        with database.session_factory() as session, session.begin():
            order = session.scalar(select(OrderResult))
            session.add(OrderAction(
                order_result_id=order.id, action="print", status="failed",
                cms_response_json=json.dumps({"result": result}),
            ))

        order = repository.get_run("run-1")["orders"]["25506185"]
        value = stored_action_result("25506185", order["actions"][-1])

        self.assertEqual(value, {"order_id": "25506185", "action": "print", **result})

    def test_old_failed_row_gets_generic_message(self):
        value = stored_action_result("1", {"action": "print", "status": "failed"})

        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["message"], UNKNOWN_FAILURE_MESSAGE)

    def test_restart_failure_is_explained(self):
        value = stored_action_result(
            "1", {"action": "print", "status": "failed", "reason": "application_restart"}
        )

        self.assertEqual(value["message"], RESTART_FAILURE_MESSAGE)

    def test_prepared_action_has_no_message(self):
        value = stored_action_result("1", {"action": "print", "status": "prepared", "pitstop": {"verdict": "ok"}})

        self.assertEqual(value, {"order_id": "1", "status": "prepared", "action": "print", "pitstop": {"verdict": "ok"}})

    def test_journal_shows_the_stored_failure(self):
        journal = build_order_journal(
            {"order_id": "1", "status": "warning"},
            actions=[{
                "id": 7, "action": "print", "status": "failed", "created_at": "2026-11-23T14:07:18+00:00",
                "cms_response": {"result": {"status": "review_required", "code": "layered_review_required", "message": LAYERED_MESSAGE}},
            }],
        )
        entries = [
            entry for section in journal.values() if isinstance(section, list)
            for entry in section if isinstance(entry, dict) and entry.get("code") == "action:print"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["data"]["result_status"], "review_required")
        self.assertEqual(entries[0]["data"]["result_code"], "layered_review_required")
        self.assertEqual(entries[0]["data"]["message"], LAYERED_MESSAGE)


if __name__ == "__main__":
    unittest.main()


def test_orders_from_run_rebuilds_review_required_action_result():
    from control_panel import _orders_from_run

    run = {
        "id": "run-1",
        "options": {},
        "orders": {
            "1001": {
                "order_id": "1001",
                "status": "warning",
                "files": [],
                "actions": [
                    {"id": 1, "action": "print", "status": "failed"},
                    {
                        "id": 2, "action": "print", "status": "failed",
                        "result": {
                            "status": "review_required", "code": "layered_review_required",
                            "message": LAYERED_MESSAGE, "generated_layered_file_ids": ["f1"],
                        },
                    },
                ],
            }
        },
    }

    action_result = _orders_from_run(run)[0]["action_result"]

    assert action_result["status"] == "review_required"
    assert action_result["message"] == LAYERED_MESSAGE
    assert action_result["generated_layered_file_ids"] == ["f1"]
