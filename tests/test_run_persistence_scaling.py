"""Per-order persistence of active runs (save_order_with_event, envelope)."""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest

from sqlalchemy import event, func, select

from processing.models import FileCheck, OrderCheck, ParsedFilename
from server.database import Database
from server.models import (
    FileResult,
    OrderResult,
    PdfRevision,
    PitstopCheck,
    PitstopIssue,
)
from services.batch_adapter import OrderArtifacts, ProcessingOptions
from services.coordinator import RunCoordinator
from services.dto import order_check_to_dto
from services.repository import (
    ENVELOPE_DEFER,
    ENVELOPE_PATCH,
    InMemoryRunRepository,
)
from services.sql_repository import SqlRunRepository


def make_order(index, *, customer_id="42", waiting=False):
    order_id = str(1000 + index)
    files = []
    for side in ("face", "back"):
        parsed = ParsedFilename(customer_id, order_id, 90, 50, 4, 4, side)
        files.append(
            FileCheck(
                path=Path(f"/orders/{customer_id}-{order_id}-90x50-{side}.tif"),
                parsed=parsed,
                actual_width_mm=92,
                actual_height_mm=52,
                dpi=300,
                dpi_x=300,
                dpi_y=300,
                actual_format="TIFF",
                colorspace="CMYK",
                resample_decision=(
                    "ask_confirmation" if waiting and side == "face" else "accept"
                ),
            )
        )
    return OrderCheck(order_id=order_id, customer_id=customer_id, files=files)


def pitstop_for(order_id, *, message="Шрифт не встроен", tag="run"):
    return {
        "check_id": f"check-{tag}-{order_id}",
        "execution_status": "completed",
        "verdict": "warning",
        "checked_at": "2026-07-25T10:00:08+00:00",
        "checked_revision": 1,
        "profile": {"key": "digital", "name": "Test", "version": "1"},
        "pages": 2,
        "counts": {"errors": 0, "warnings": 1},
        "issues": [
            {
                "id": f"issue-{order_id}",
                "fingerprint": "font:1",
                "severity": "warning",
                "action_id": "1012",
                "message": message,
                "occurrences": 1,
                "locations": [{"page": 1}],
            }
        ],
        "reports": {"json_url": None, "xml_url": None},
    }


class Adapter:
    """Deterministic adapter: PitStop result, previews and one waiting order."""

    pitstop_enabled = True

    def __init__(self, count, *, waiting_index=None, hold_from=None, tag="run"):
        self.tag = tag
        self.count = count
        self.total_orders = count
        self.waiting_index = waiting_index
        self.hold_from = hold_from
        self.release = threading.Event()
        self.completed = threading.Semaphore(0)

    def iter_inspect_orders(self):
        for index in range(self.count):
            yield make_order(index, waiting=index == self.waiting_index)

    def pending_files(self, order):
        return [
            item for item in order.files if item.resample_decision == "ask_confirmation"
        ]

    def decide(self, order, approved):
        for item in self.pending_files(order):
            item.resample_confirmed = approved
            item.resample_decision = "auto_correct" if approved else "reject"

    def process_order(self, order):
        if self.hold_from is not None and int(order.order_id) - 1000 >= self.hold_from:
            self.release.wait(timeout=10)
        order.postpress = {"fold": {"type": "half-fold", "count": 1}}
        result = OrderArtifacts(
            pdf_path=Path(f"/out/{order.order_id}.pdf"),
            preview_paths=[
                Path(f"/out/{order.order_id}-face.webp"),
                Path(f"/out/{order.order_id}-back.webp"),
            ],
            pitstop=pitstop_for(order.order_id, tag=self.tag),
            current_pdf_revision=1,
            current_pdf_sha256="ab" * 32,
        )
        self.completed.release()
        return result


class LegacyFullSaveRepository(SqlRunRepository):
    """Reference: every write goes through the old whole-run save."""

    def save_order_with_event(self, run, order_key, event_type, data, *, envelope=ENVELOPE_PATCH):
        event = super().save_run_with_event(run, event_type, data)
        # The old coordinator re-read the whole run after every save, so its
        # aggregate carried the stored file IDs.
        stored = self.get_run(run["id"])
        run["orders"][order_key] = {
            **run["orders"][order_key],
            "files": stored["orders"][order_key]["files"],
        }
        return event

    def save_run_with_event(self, run, event_type, data, *, include_orders=True):
        return super().save_run_with_event(run, event_type, data)

    def save_run(self, run, *, include_orders=True):
        return super().save_run(run)


class SqlTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.databases = []

    def tearDown(self):
        for database in self.databases:
            database.dispose()
        self.temporary_directory.cleanup()

    def database(self, name="runs.sqlite3"):
        database = Database(f"sqlite:///{Path(self.temporary_directory.name) / name}")
        database.create_schema()
        self.databases.append(database)
        return database

    def run_coordinator(self, repository, adapter, **options):
        coordinator = RunCoordinator(
            repository, adapter_factory=lambda _options: adapter, **options
        )
        self.addCleanup(coordinator.shutdown)
        return coordinator


def _count_statements(engine):
    statements = []

    def listener(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    return statements, lambda: event.remove(engine, "before_cursor_execute", listener)


class SaveOrderScalingTests(SqlTestCase):
    def _stored_run(self, repository, size):
        run = {
            "id": f"run-{size}",
            "status": "running",
            "stage": "processing",
            "progress": 10,
            "options": {"input_path": "/orders", "direction": "digital"},
            "created_at": "2026-07-25T10:00:00+00:00",
            "started_at": "2026-07-25T10:00:01+00:00",
            "finished_at": None,
            "error": None,
            "total_orders": size,
            "processed_orders": 0,
            "orders": {},
        }
        for index in range(size):
            order = make_order(index)
            run["orders"][order.order_id] = order_check_to_dto(order, status="processing")
        repository.create_run(run)
        return run

    def _statements_for_one_order_event(self, size):
        database = self.database(f"scale-{size}.sqlite3")
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        run = self._stored_run(repository, size)
        counts = {}
        for envelope in (ENVELOPE_DEFER, ENVELOPE_PATCH):
            order = make_order(size // 2)
            dto = order_check_to_dto(
                order,
                status="warning",
                pdf_path=f"/out/{order.order_id}.pdf",
                pitstop=pitstop_for(order.order_id),
                current_pdf_revision=1,
                current_pdf_sha256="ab" * 32,
            )
            run["orders"][order.order_id] = dto
            statements, stop = _count_statements(database.engine)
            try:
                repository.save_order_with_event(
                    run, order.order_id, "order.completed",
                    {"order_id": order.order_id, "order": dto}, envelope=envelope,
                )
            finally:
                stop()
            counts[envelope] = len(statements)
        return counts

    def test_statement_count_does_not_grow_with_run_size(self):
        small = self._statements_for_one_order_event(50)
        large = self._statements_for_one_order_event(500)
        for envelope in (ENVELOPE_DEFER, ENVELOPE_PATCH):
            self.assertLessEqual(
                abs(large[envelope] - small[envelope]), 2, (envelope, small, large)
            )
            self.assertLess(large[envelope], 40, (envelope, large))

    def test_in_memory_save_order_copies_only_that_order(self):
        repository = InMemoryRunRepository()
        run = {"id": "mem", "status": "running", "orders": {
            "a": {"status": "processing"}, "b": {"status": "processing"}}}
        repository.create_run(run)
        run["orders"]["a"] = {"status": "passed"}
        run["orders"]["b"] = {"status": "unsaved"}
        run["processed_orders"] = 1
        repository.save_order_with_event(run, "a", "order.completed", {"order_id": "a"})
        stored = repository.get_run("mem")
        self.assertEqual(stored["orders"]["a"]["status"], "passed")
        self.assertEqual(stored["orders"]["b"]["status"], "processing")
        self.assertEqual(stored["processed_orders"], 1)
        self.assertEqual([item.type for item in repository.list_events("mem")],
                         ["order.completed"])


class PitstopIssueReuseTests(SqlTestCase):
    def test_unchanged_check_keeps_issue_rows(self):
        database = self.database()
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        order = make_order(1)
        dto = order_check_to_dto(
            order, status="warning", pdf_path="/out/1001.pdf",
            pitstop=pitstop_for("1001"), current_pdf_revision=1,
            current_pdf_sha256="ab" * 32,
        )
        run = {"id": "r", "status": "running", "options": {}, "orders": {"1001": dto},
               "created_at": "2026-07-25T10:00:00+00:00"}
        repository.create_run(run)

        def issue_rows():
            with database.session_factory() as session:
                return [(item.id, item.message) for item in session.scalars(
                    select(PitstopIssue).order_by(PitstopIssue.id))]

        before = issue_rows()
        statements, stop = _count_statements(database.engine)
        try:
            repository.save_order_with_event(run, "1001", "pitstop.check_completed", {})
        finally:
            stop()
        self.assertEqual(issue_rows(), before)
        self.assertFalse([item for item in statements if "pitstop_issues" in item
                          and (item.startswith("DELETE") or item.startswith("INSERT"))])

        # save_order_with_event replaced the entry with an id-carrying copy.
        run["orders"]["1001"]["pitstop"] = pitstop_for("1001", message="Изменено")
        repository.save_order_with_event(run, "1001", "pitstop.check_completed", {})
        after = issue_rows()
        self.assertEqual([message for _id, message in after], ["Изменено"])
        self.assertNotEqual(after[0][0], before[0][0])


def _normalized_run(run):
    value = deepcopy(run)
    for key in ("id", "created_at", "started_at", "finished_at"):
        value.pop(key, None)
    for order in value["orders"].values():
        for item in order.get("files") or []:
            item.pop("file_result_id", None)
    return value


def _table_snapshot(database):
    with database.session_factory() as session:
        return {
            "orders": session.scalar(select(func.count()).select_from(OrderResult)),
            "files": session.scalar(select(func.count()).select_from(FileResult)),
            "revisions": session.scalar(select(func.count()).select_from(PdfRevision)),
            "checks": session.scalar(select(func.count()).select_from(PitstopCheck)),
            "issues": sorted(
                (item.issue_id, item.message, item.severity)
                for item in session.scalars(select(PitstopIssue))
            ),
        }


class RoundTripEquivalenceTests(SqlTestCase):
    def _process(self, repository_class, name):
        database = self.database(name)
        repository = repository_class(database.session_factory, recover_interrupted=False)
        adapter = Adapter(12, waiting_index=3)
        coordinator = self.run_coordinator(
            repository, adapter, order_workers=3, envelope_checkpoint_seconds=3600
        )
        run_id = coordinator.submit(
            ProcessingOptions(input_path="/orders", direction="digital")
        )["id"]
        coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=10)
        coordinator.confirm_correction(run_id, "1003")
        coordinator.wait_for(run_id, {"completed"}, timeout=10)
        reopened = SqlRunRepository(database.session_factory, recover_interrupted=False)
        events = [(item.type, item.data) for item in reopened.list_events(run_id)]
        return reopened.get_run(run_id), events, _table_snapshot(database)

    def test_new_path_reads_back_like_the_full_save_path(self):
        new_run, new_events, new_tables = self._process(SqlRunRepository, "new.sqlite3")
        old_run, old_events, old_tables = self._process(
            LegacyFullSaveRepository, "old.sqlite3"
        )
        self.assertEqual(_normalized_run(new_run), _normalized_run(old_run))
        self.assertEqual(new_tables, old_tables)
        self.assertEqual(new_tables["orders"], 12)
        self.assertEqual(new_tables["files"], 24)
        self.assertEqual(len(new_tables["issues"]), 12)
        # Orders finish in parallel, so compare the journal per order.
        def by_order(events):
            grouped = {}
            for event_type, data in events:
                grouped.setdefault(data.get("order_id"), []).append((event_type, data))
            return grouped
        self.assertEqual(by_order(new_events), by_order(old_events))
        self.assertEqual(new_events[-1][0], "run.completed")
        self.assertEqual(new_events[-1][1], old_events[-1][1])
        self.assertEqual(new_run["processed_orders"], 12)
        self.assertEqual(new_run["orders"]["1003"]["status"], "warning")
        self.assertEqual(
            new_run["orders"]["1005"]["postpress"]["fold"]["type"], "half-fold"
        )
        self.assertEqual(new_run["orders"]["1005"]["pitstop"]["issues"][0]["id"],
                         "issue-1005")

    def test_file_result_ids_of_live_run_match_stored_rows(self):
        database = self.database()
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        adapter = Adapter(3, waiting_index=1)
        coordinator = self.run_coordinator(repository, adapter)
        run_id = coordinator.submit(
            ProcessingOptions(input_path="/orders", direction="digital")
        )["id"]
        live = coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=10)
        stored = repository.get_run(run_id)
        for key, order in live["orders"].items():
            self.assertEqual(
                [item["file_result_id"] for item in order["files"]],
                [item["file_result_id"] for item in stored["orders"][key]["files"]],
            )
        file_id = live["orders"]["1001"]["files"][0]["file_result_id"]
        self.assertEqual(coordinator.find_file_by_id(str(file_id))[1]["file_result_id"], file_id)


class RestartRecoveryTests(SqlTestCase):
    def _crash_mid_run(self, checkpoint_seconds):
        database = self.database(f"crash-{checkpoint_seconds}.sqlite3")
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        finished = self.run_coordinator(repository, Adapter(2, tag="history"))
        finished_id = finished.submit(
            ProcessingOptions(input_path="/orders", direction="digital")
        )["id"]
        finished.wait_for(finished_id, {"completed"}, timeout=10)
        finished.shutdown()

        adapter = Adapter(5, hold_from=3)
        coordinator = self.run_coordinator(
            repository, adapter, order_workers=1,
            envelope_checkpoint_seconds=checkpoint_seconds,
        )
        run_id = coordinator.submit(
            ProcessingOptions(input_path="/orders", direction="digital")
        )["id"]
        deadline = time.monotonic() + 10
        while coordinator.get_run_summary(run_id).get("processed_orders", 0) < 3:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        # Simulated crash: a new process opens the same database while the
        # old coordinator is still mid-run (nothing more is flushed).
        restarted = SqlRunRepository(database.session_factory)
        recovered = restarted.get_run(run_id)
        history = restarted.get_run(finished_id)
        adapter.release.set()
        # Let the old (simulated dead) coordinator drain before cleanup.
        coordinator.wait_for(run_id, {"completed", "failed", "cancelled"}, timeout=10)
        coordinator.shutdown()
        return recovered, history

    def test_crash_without_checkpoint_keeps_order_rows_and_recounts(self):
        recovered, history = self._crash_mid_run(checkpoint_seconds=3600)
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["stage"], "interrupted")
        self.assertIn("перезапуском", recovered["error"])
        self.assertEqual(recovered["processed_orders"], 3)
        self.assertEqual(recovered["total_orders"], 5)
        self.assertEqual(recovered["warning_orders"], 3)
        self.assertEqual(set(recovered["orders"]), {"1000", "1001", "1002", "1003", "1004"})
        done = recovered["orders"]["1000"]
        self.assertEqual(done["status"], "warning")
        self.assertEqual(done["pdf_path"], "/out/1000.pdf")
        self.assertEqual(len(done["files"]), 2)
        self.assertEqual(done["preview_paths"],
                         ["/out/1000-face.webp", "/out/1000-back.webp"])
        # Documented loss without a checkpoint: envelope-only extras.
        self.assertNotIn("postpress", done)
        # Finished history is untouched.
        self.assertEqual(history["status"], "completed")
        self.assertEqual(history["orders"]["1000"]["postpress"]["fold"]["type"], "half-fold")
        self.assertEqual(history["orders"]["1000"]["pitstop"]["issues"][0]["id"], "issue-1000")

    def test_checkpoint_keeps_extras_of_processed_orders(self):
        recovered, _history = self._crash_mid_run(checkpoint_seconds=0)
        self.assertEqual(recovered["stage"], "interrupted")
        for key in ("1000", "1001", "1002"):
            order = recovered["orders"][key]
            self.assertEqual(order["postpress"]["fold"]["type"], "half-fold", key)
            self.assertEqual(order["aggregate_id"], f"42:{key}")
            self.assertEqual(order["pitstop"]["check_id"], f"check-run-{key}")
        self.assertEqual(recovered["processed_orders"], 3)


class ListEventsLimitTests(SqlTestCase):
    def test_limit_is_applied_in_id_order(self):
        database = self.database()
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        repository.create_run({"id": "r", "status": "completed", "options": {},
                               "orders": {}, "created_at": "2026-07-25T10:00:00+00:00"})
        ids = [repository.append_event("r", f"e{index}", {"i": index}).id for index in range(5)]
        self.assertEqual([item.id for item in repository.list_events("r", 0, limit=2)], ids[:2])
        self.assertEqual([item.id for item in repository.list_events("r", ids[1], limit=2)],
                         ids[2:4])
        self.assertEqual(len(repository.list_events("r")), 5)
        self.assertEqual(repository.list_events("r", limit=0), [])
        self.assertTrue(repository.run_exists("r"))
        self.assertFalse(repository.run_exists("missing"))


class SqlitePragmaTests(SqlTestCase):
    def test_wal_with_synchronous_normal(self):
        database = self.database()
        with database.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal"
            )
            # 1 == NORMAL
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA synchronous").scalar(), 1
            )


if __name__ == "__main__":
    unittest.main()
