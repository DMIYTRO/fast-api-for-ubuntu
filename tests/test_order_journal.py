"""Журнал заказа: чистый построитель и API (синтетические данные, без сети)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ.setdefault("IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password"))

import control_panel  # noqa: E402  (pytest only: conftest isolates the database)
from server.models import ArchivedRunEvent, OrderAction, OrderResult, PdfRevision  # noqa: E402
from server.settings import Settings  # noqa: E402
from services.order_journal import SCHEMA, VERSION, build_order_journal  # noqa: E402

ROOT = "/mnt/shared/Input"


def _final_order() -> dict:
    return {
        "aggregate_id": "42:1001",
        "order_id": "1001",
        "customer_id": "42",
        "status": "passed",
        "current_pdf_revision": 2,
        "check_policy": {"key": "basic", "label": "Облегчённая проверка (Input)", "reason": "input_root"},
        "errors": [],
        "warnings": [],
        "processing_errors": [],
        "files": [
            {
                "file_result_id": 7,
                "path": f"{ROOT}/face.pdf",
                "name": "face.pdf",
                "actual_format": "PDF",
                "errors": [],
                "warnings": [],
                "size_fix": {
                    "method": "bleed", "auto": True, "from_mm": [90.0, 50.0], "to_mm": [94.0, 54.0],
                    "detail": "Callas дорисовал поля вылета 2 мм: 90.0×50.0 → 94.0×54.0 мм",
                },
                "callas_source_fix": {
                    "status": "accepted", "plan": "size_correction", "plan_title": "Коррекция размера макета",
                    "bleed_only": True, "auto": True, "baseline_pdf": f"{ROOT}/.im/face-before.pdf",
                    "pdf_path": f"{ROOT}/.im/face-fixed.pdf",
                },
                "processing_actions": [],
            },
            {
                "file_result_id": 8,
                "path": f"{ROOT}/back.tif",
                "name": "back.tif",
                "actual_format": "TIFF",
                "errors": [],
                "warnings": [],
                "tiff_flattened": True,
                "tiff_visible_layer_count": 2,
                "tiff_hidden_layer_count": 1,
                "size_fix": {
                    "method": "fill", "auto": False, "from_mm": [93.0, 55.0], "to_mm": [94.0, 54.0],
                    "detail": "93.0x55.0 → 94.0x54.0 мм: подтверждено оператором",
                },
                "processing_actions": [
                    {"kind": "size_correction", "target_mm": [94.0, 54.0], "target_dpi": 300.0},
                    {"kind": "strip_icc"},
                    {"kind": "mystery_step", "path": f"{ROOT}/tmp/x.pdf"},
                ],
            },
        ],
        "pitstop": {
            "execution_status": "completed",
            "verdict": "passed",
            "issues": [
                {
                    "action_id": "2079", "severity": "information", "message": "Clipping objects found (3x on pages 1-2)",
                    "occurrences": 3, "policy_demoted": True, "policy_original_severity": "warning",
                    "policy_category": "clipping", "policy_category_label": "Обтравка",
                    "locations": [{"page": 1, "bbox": {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1}},
                                  {"page": 2, "bbox": {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1}}],
                },
                {
                    "action_id": "3004", "severity": "information", "message": "Transparency used (1x on page 1)",
                    "occurrences": 1, "policy_demoted": True, "policy_original_severity": "warning",
                    "policy_category": "transparency", "policy_category_label": "Прозрачность",
                    "locations": [{"page": 1}],
                },
                {
                    "action_id": "1153", "severity": "information",
                    "message": "Total ink coverage of an object is more than 310% (4x on page 1)",
                    "occurrences": 4, "hidden_by_baseline": True, "original_severity": "error",
                    "introduced_by_fix": 4, "baseline_occurrences": 0, "locations": [{"page": 1}],
                },
            ],
        },
        "prepress": {
            "fonts": {"status": "ok", "count": 0, "fonts": [], "pages": [], "not_embedded": 0},
            "high_ppi": {"status": "ok", "count": 0, "pages": [], "message": None, "one_bit": {"count": 0}},
            "black_solid": {
                "status": "ok", "detected": True, "share": 0.41, "threshold": 0.25, "pages": [1],
                "fix_available": True, "fix_pages": [1], "fix_share": 0.41, "manual_check": False,
                "message": "Плашка 100% K: 41% площади, стр. 1. Можно перевести в составной чёрный (C60 M40 Y40 K100).",
            },
        },
        "color_conversion": {
            "status": "converted", "reason": "rgb", "from": "RGB", "to": "CMYK",
            "profile": "Image Magic/NEW_Convert color to 4c for digital printing",
            "pages": [1, 2], "icc_pages": [1, 2], "icc_remaining_pages": [], "icc_removed": True,
            "output_intent_removed": False,
        },
        "ink_coverage": {"status": "completed", "flags": [{"kind": "overflow", "page": 2, "percent": 262.0, "threshold": 250.0}]},
    }


def _initial_order() -> dict:
    initial = deepcopy(_final_order())
    initial["current_pdf_revision"] = 1
    initial["status"] = "warning"
    initial["prepress"]["high_ppi"] = {
        "status": "ok", "count": 2, "pages": [2], "max_ppi": 1200.0, "limit": 500.0,
        "message": "Растры выше 500 ppi: 2 объекта, стр. 2 (до 1200 ppi).", "one_bit": {"count": 0},
    }
    initial["pitstop"]["issues"][2] = {
        "action_id": "1153", "severity": "error",
        "message": "Total ink coverage of an object is more than 310% (4x on page 1)",
        "occurrences": 4, "locations": [{"page": 1}],
    }
    return initial


def _events() -> list[dict]:
    aggregate = {"aggregate_id": "42:1001", "order_id": "1001"}
    revision_snapshot = deepcopy(_final_order())
    return [
        {"id": 10, "type": "order.completed", "created_at": "2026-11-21T10:00:00+00:00",
         "data": {**aggregate, "status": "warning", "order": _initial_order()}},
        {"id": 11, "type": "order.callas_fix.started", "created_at": "2026-11-21T10:01:00+00:00",
         "data": {**aggregate, "plan": "remove_clipping", "file_id": None, "status": "processing"}},
        {"id": 12, "type": "order.callas_fix.finished", "created_at": "2026-11-21T10:01:10+00:00",
         "data": {**aggregate, "plan": "remove_clipping", "file_id": None, "status": "ready"}},
        {"id": 13, "type": "order.callas_fix.rejected", "created_at": "2026-11-21T10:01:30+00:00",
         "data": {**aggregate, "decision": "reject", "status": "rejected"}},
        {"id": 14, "type": "order.callas_fix.started", "created_at": "2026-11-21T10:02:00+00:00",
         "data": {**aggregate, "plan": "reduce_tac", "file_id": None, "status": "processing"}},
        {"id": 15, "type": "order.callas_fix.finished", "created_at": "2026-11-21T10:02:09+00:00",
         "data": {**aggregate, "plan": "reduce_tac", "file_id": None, "status": "ready"}},
        {"id": 16, "type": "order.pdf_revision", "created_at": "2026-11-21T10:02:30.100000+00:00",
         "data": {**aggregate, "status": "passed", "order": revision_snapshot}},
        {"id": 17, "type": "order.callas_fix.accepted", "created_at": "2026-11-21T10:02:30.104000+00:00",
         "data": {**aggregate, "decision": "accept", "status": "accepted"}},
        {"id": 9, "type": "order.correction_confirmed", "created_at": "2026-11-21T09:59:00+00:00",
         "data": {**aggregate, "customer_id": "42", "decisions": [{
             "path": f"{ROOT}/back.tif", "decision": "confirmed",
             "original": {"width_mm": 93.0, "height_mm": 55.0, "resample_decision": "ask_confirmation"},
             "proposed": {"target_mm": [94.0, 54.0], "crop_mm": [0.0, 1.0], "scale": 1.01, "rotation_degrees": 0},
         }]}},
    ]


ACTIONS = [{"id": 5, "action": "print", "status": "prepared", "comment": None,
            "created_at": "2026-11-21T10:05:00+00:00", "cms_response": {"http_status": 200, "response": {"raw": "<div>ok</div>"}}}]


def _by_code(journal: dict) -> dict[str, dict]:
    return {entry["code"]: entry for entry in journal["entries"]}


class BuildOrderJournalTests(unittest.TestCase):
    def setUp(self):
        self.journal = build_order_journal(
            _final_order(), _events(), ACTIONS,
            [{"revision_number": 1, "created_at": "2026-11-21T10:00:00"}, {"revision_number": 2, "created_at": "2026-11-21T10:02:30"}],
            run_id="run-1", generated_at="2026-11-21T12:00:00+00:00",
        )
        self.codes = _by_code(self.journal)

    def test_envelope_and_order(self):
        journal = self.journal
        self.assertEqual((journal["schema"], journal["version"]), (SCHEMA, VERSION))
        self.assertEqual((journal["run_id"], journal["order_id"], journal["customer_id"]), ("run-1", "1001", "42"))
        self.assertEqual(journal["check_policy"]["key"], "basic")
        kinds = [entry["kind"] for entry in journal["entries"]]
        rank = {"issue": 0, "auto_change": 1, "operator_change": 2, "decision": 3}
        self.assertEqual(kinds, sorted(kinds, key=rank.__getitem__))
        summary = journal["summary"]
        self.assertEqual(summary["auto_changes"], sum(kind == "auto_change" for kind in kinds))
        self.assertEqual(summary["issues"] + summary["resolved_issues"], kinds.count("issue"))
        for entry in journal["entries"]:
            self.assertEqual(set(entry), {"kind", "code", "source", "severity", "title", "detail", "file",
                                          "pages", "count", "at", "outcome", "data"})

    def test_no_absolute_paths_leak(self):
        self.assertNotIn("/mnt/shared", json.dumps(self.journal, ensure_ascii=False))

    def test_pitstop_issues_keep_policy_and_fix_flags(self):
        clipping = self.codes["pitstop:2079"]
        self.assertEqual(clipping["severity"], "info")
        self.assertTrue(clipping["data"]["policy_demoted"])
        self.assertEqual(clipping["data"]["original_severity"], "warning")
        self.assertEqual(clipping["pages"], [1, 2])
        self.assertIn("Обтравка", clipping["title"])
        transparency = self.codes["pitstop:3004"]
        self.assertTrue(transparency["title"].startswith("Прозрачность"))
        self.assertTrue(transparency["data"]["policy_demoted"])
        tac = [entry for entry in self.journal["entries"] if entry["code"] == "pitstop:1153"]
        self.assertEqual(len(tac), 1)
        self.assertTrue(tac[0]["data"]["added_by_fix"])
        self.assertEqual(tac[0]["data"]["original_severity"], "error")

    def test_prepress_and_ink_findings(self):
        black = self.codes["prepress:black_solid"]
        self.assertEqual((black["severity"], black["pages"]), ("warning", [1]))
        self.assertEqual(black["data"]["fix_plan"], "black_to_rich")
        self.assertIn("Перелив", self.codes["ink:overflow"]["title"])
        resolved = self.codes["prepress:high_ppi"]
        self.assertEqual(resolved["outcome"], "resolved")
        self.assertEqual(resolved["at"], "2026-11-21T10:00:00+00:00")
        self.assertNotIn("prepress:fonts", self.codes)

    def test_auto_changes(self):
        self.assertEqual(self.codes["auto:color_conversion:rgb"]["outcome"], "converted")
        self.assertEqual(self.codes["auto:icc_removed"]["pages"], [1, 2])
        bleed = self.codes["auto:size_fix:bleed"]
        self.assertEqual((bleed["file"], bleed["source"], bleed["title"]), ("face.pdf", "callas", "Добавлены поля вылета (Callas)"))
        self.assertEqual(bleed["data"]["callas_plan"], "size_correction")
        self.assertEqual(bleed["at"], "2026-11-21T10:00:00+00:00")
        self.assertNotIn("auto:callas_source_fix:size_correction", self.codes)
        self.assertEqual(self.codes["auto:tiff_flatten"]["file"], "back.tif")
        self.assertIn("auto:strip_icc", self.codes)
        unknown = self.codes["auto:action:mystery_step"]
        self.assertEqual(unknown["data"]["path"], "x.pdf")
        # size_fix already describes the size ladder result
        self.assertNotIn("auto:size_correction", self.codes)
        self.assertEqual(sum(entry["code"] == "auto:size_fix:bleed" for entry in self.journal["entries"]), 1)

    def test_operator_changes(self):
        operator = [entry for entry in self.journal["entries"] if entry["kind"] == "operator_change"]
        self.assertEqual([entry["code"] for entry in operator],
                         ["correction:confirmed", "callas_fix:remove_clipping", "callas_fix:reduce_tac"])
        correction, clipping, tac = operator
        self.assertEqual((correction["file"], correction["outcome"]), ("back.tif", "accepted"))
        self.assertEqual(correction["data"]["method"], "fill")
        self.assertIn("93×55 → 94×54 мм", correction["detail"])
        self.assertEqual(clipping["outcome"], "rejected")
        self.assertEqual(tac["outcome"], "accepted")
        self.assertEqual(tac["title"], "Исправление Callas: Снижение суммарного покрытия до 300%")
        self.assertEqual(tac["data"]["pdf_revision"], 2)
        self.assertEqual(tac["data"]["started_at"], "2026-11-21T10:02:00+00:00")
        self.assertEqual(tac["at"], "2026-11-21T10:02:30.104000+00:00")
        self.assertNotIn("pdf_revision", self.codes)
        self.assertNotIn("operator:size_fix:fill", self.codes)

    def test_decision(self):
        decision = self.journal["entries"][-1]
        self.assertEqual((decision["kind"], decision["code"], decision["outcome"]), ("decision", "action:print", "done"))
        self.assertNotIn("raw", json.dumps(decision["data"]))

    def test_missing_or_odd_fields_never_raise(self):
        for order in ({}, {"files": "x", "pitstop": [], "prepress": {"fonts": "bad", "black_solid": {"status": "ok", "message": "m"}},
                           "color_conversion": 5, "ink_coverage": {"flags": [{"kind": "overflow", "percent": None}]},
                           "check_policy": "full"},
                      {"files": [None, {"size_fix": "bad", "processing_actions": [None, 3]}]}):
            journal = build_order_journal(order, [None, {"type": "order.completed", "data": "x"}, {"type": "order.callas_fix.accepted", "data": {}}],
                                          [None, {"action": "reject", "status": "failed"}])
            self.assertEqual(journal["schema"], SCHEMA)
        orphan = _by_code(journal)
        self.assertEqual(orphan["callas_fix:unknown"]["outcome"], "accepted")
        self.assertEqual(orphan["action:reject"]["severity"], "warning")

    def test_manual_revision_without_fix_and_forced_print(self):
        events = [
            {"id": 1, "type": "order.pdf_revision", "created_at": "2026-11-21T10:00:00+00:00",
             "data": {"aggregate_id": "42:1001", "status": "error", "order": {"current_pdf_revision": 3}}},
            {"id": 2, "type": "order.forced_print", "created_at": "2026-11-21T10:10:00+00:00",
             "data": {"aggregate_id": "42:1001", "errors": ["ошибка"], "pdf_revision": 3, "forced": True}},
        ]
        codes = _by_code(build_order_journal({"order_id": "1001"}, events, [], [{"revision_number": 2, "created_at": "2026-11-20T10:00:00"}]))
        self.assertEqual(codes["pdf_revision"]["data"]["pdf_revision"], 3)
        self.assertEqual(codes["forced_print"]["severity"], "warning")
        revisions = [entry for entry in build_order_journal({}, events, [], [{"revision_number": 2}])["entries"] if entry["code"] == "pdf_revision"]
        self.assertEqual({entry["data"]["pdf_revision"] for entry in revisions}, {2, 3})


class OrderJournalApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        app = control_panel.create_app(
            settings=settings, allowed_roots=(self.root,), default_input_dir=self.root, initialize_schema=True,
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def _seed(self) -> int:
        repository = self.client.app.state.coordinator.repository
        order = {**_final_order(), "pdf_path": str(self.root / "PDF" / "face.pdf"), "current_pdf_sha256": "a" * 64}
        for item in order["files"]:
            item.pop("file_result_id")  # the repository assigns its own row ids
        other = {"aggregate_id": "77:1001", "order_id": "1001", "customer_id": "77", "status": "error", "files": []}
        repository.create_run({"id": "run-j", "status": "completed", "options": {"input_path": str(self.root)},
                               "orders": {"42:1001": order, "77:1001": other}})
        for event in _events():
            if event["id"] == 10:
                continue
            repository.append_event("run-j", event["type"], event["data"])
        repository.append_event("run-j", "order.callas_fix.started", {"aggregate_id": "77:1001", "order_id": "1001", "plan": "black_to_rich"})
        repository.append_event("run-j", "preview.created", {"aggregate_id": "42:1001", "order_id": "1001"})
        with self.client.app.state.database.session_factory() as session:
            record = session.scalar(select(OrderResult).where(OrderResult.run_id == "run-j", OrderResult.customer_id == "42"))
            first = _events()[0]
            # The check-time event was archived: it must still be part of the journal.
            session.add(ArchivedRunEvent(
                id=1, run_id="run-j", event_type=first["type"],
                payload_json=json.dumps(first["data"], ensure_ascii=False, separators=(",", ":")),
                created_at=datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc),
            ))
            session.add(PdfRevision(order_result_id=record.id, revision_number=5, pdf_path="/x/a.pdf", is_current=False))
            action = OrderAction(order_result_id=record.id, action="print", status="prepared",
                                 cms_response_json=json.dumps({"kit_main_id": "1000", "returned_with_kit": True, "message": "ok"}))
            session.add(action)
            session.commit()
            return action.id

    def login(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "correct horse"}).status_code, 200)

    def test_history_and_run_journal(self):
        action_id = self._seed()
        url = f"/api/order-history/{action_id}/journal"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.login()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        journal = response.json()
        codes = _by_code(journal)
        self.assertEqual((journal["run_id"], journal["customer_id"]), ("run-j", "42"))
        self.assertEqual(codes["prepress:high_ppi"]["outcome"], "resolved")  # from the archived event
        self.assertEqual(codes["callas_fix:reduce_tac"]["outcome"], "accepted")
        self.assertNotIn("callas_fix:black_to_rich", codes)  # other customer's order
        self.assertEqual(codes["action:print"]["data"]["kit_main_id"], "1000")
        self.assertEqual(codes["pdf_revision"]["data"]["pdf_revision"], 5)

        direct = self.client.get("/api/checks/run-j/orders/42:1001/journal")
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(
            [entry["code"] for entry in direct.json()["entries"]],
            [entry["code"] for entry in journal["entries"]],
        )
        ambiguous = self.client.get("/api/checks/run-j/orders/1001/journal")
        self.assertEqual(ambiguous.status_code, 409)
        self.assertEqual(self.client.get("/api/checks/run-j/orders/42:9999/journal").status_code, 404)
        missing_run = self.client.get("/api/checks/nope/orders/42:1001/journal")
        self.assertEqual(missing_run.status_code, 404)
        self.assertEqual(missing_run.json()["error"]["code"], "run_not_found")
        self.assertEqual(self.client.get("/api/order-history/99999/journal").status_code, 404)


if __name__ == "__main__":
    unittest.main()
