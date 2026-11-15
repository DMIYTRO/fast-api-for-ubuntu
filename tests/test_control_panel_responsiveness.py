"""SSE, order-history and shared-client behaviour that keeps the UI responsive."""

import asyncio
import dataclasses
import os
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import event

os.environ.setdefault(
    "IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password")
)

import control_panel
from server.models import CheckRun, FileResult, OrderAction, OrderResult
from server.settings import Settings
from services.coordinator import RunCoordinator


class _EmptyAdapter:
    def __init__(self, options):
        self.options = options

    def scan_and_inspect(self):
        return []

    def pending_files(self, _order):
        return []

    def decide(self, _order, _approved):
        return None

    def process_order(self, _order):
        raise AssertionError("empty adapter has no orders")


def parse_sse(body: str) -> list[tuple[int, str]]:
    return [
        (int(event_id), name)
        for event_id, name in re.findall(r"^id: (\d+)\nevent: (\S+)\n", body, re.M)
    ]


class ControlPanelResponsivenessTests(unittest.TestCase):
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
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: _EmptyAdapter(options),
            initialize_schema=True,
        )
        self.app = app
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        response = self.client.post("/api/auth/login", json={"password": "correct horse"})
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    # ----------------------------------------------------------------- SSE

    def _finished_run(self) -> str:
        response = self.client.post(
            "/api/checks",
            json={
                "input_path": str(self.root),
                "direction": "digital",
                "create_pdfs": False,
                "generate_previews": False,
                "copy_failures": False,
            },
        )
        run_id = response.json()["id"]
        self.app.state.coordinator.wait_for(run_id, timeout=5)
        return run_id

    def _stream(self, run_id, *, params=None, headers=None) -> str:
        with self.client.stream(
            "GET", f"/api/checks/{run_id}/events", params=params, headers=headers
        ) as stream:
            self.assertEqual(stream.status_code, 200)
            return "".join(stream.iter_text())

    def test_sse_delivers_ordered_ids_and_resumes_from_cursor(self):
        run_id = self._finished_run()
        full = parse_sse(self._stream(run_id))
        ids = [event_id for event_id, _ in full]
        self.assertGreaterEqual(len(ids), 2)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(full[0][1], "run.started")
        self.assertEqual(full[-1][1], "run.completed")

        first = ids[0]
        expected_tail = [item for item in full if item[0] > first]
        by_query = parse_sse(self._stream(run_id, params={"last_event_id": str(first)}))
        by_header = parse_sse(self._stream(run_id, headers={"Last-Event-ID": str(first)}))
        self.assertEqual(by_query, expected_tail)
        self.assertEqual(by_header, expected_tail)

        # Both given: the later cursor wins; the header keeps working alone.
        both = parse_sse(
            self._stream(
                run_id,
                params={"last_event_id": str(first)},
                headers={"Last-Event-ID": str(ids[-1])},
            )
        )
        self.assertEqual(both, [])
        # Fully caught up: stream ends immediately (terminal run), nothing replayed.
        self.assertEqual(parse_sse(self._stream(run_id, params={"last_event_id": str(ids[-1])})), [])
        # Garbage cursors fall back to a full replay instead of failing.
        self.assertEqual(parse_sse(self._stream(run_id, params={"last_event_id": "abc"})), full)
        self.assertEqual(parse_sse(self._stream(run_id, headers={"Last-Event-ID": "-5"})), full)

    def test_sse_unknown_run_is_404(self):
        response = self.client.get("/api/checks/missing/events")
        self.assertEqual(response.status_code, 404)

    def test_sse_does_not_load_full_run_aggregate(self):
        run_id = self._finished_run()
        coordinator = self.app.state.coordinator
        calls = {"get_run": 0, "summary": 0, "events": 0}
        original_get_run = coordinator.get_run
        original_summary = coordinator.get_run_summary
        original_events = coordinator.events

        def get_run(*args, **kwargs):
            calls["get_run"] += 1
            return original_get_run(*args, **kwargs)

        def summary(*args, **kwargs):
            calls["summary"] += 1
            return original_summary(*args, **kwargs)

        def events(*args, **kwargs):
            calls["events"] += 1
            return original_events(*args, **kwargs)

        with patch.object(coordinator, "get_run", get_run), patch.object(
            coordinator, "get_run_summary", summary
        ), patch.object(coordinator, "events", events):
            body = self._stream(run_id)
        self.assertIn("event: run.completed", body)
        self.assertEqual(calls["get_run"], 0)
        self.assertEqual(calls["events"], 0)  # coordinator.events loads the whole run
        self.assertGreaterEqual(calls["summary"], 1)

    def test_sse_blocking_calls_run_in_threads(self):
        run_id = self._finished_run()
        offloaded = []
        real_to_thread = asyncio.to_thread

        async def spy(func, /, *args, **kwargs):
            offloaded.append(getattr(func, "__name__", repr(func)))
            return await real_to_thread(func, *args, **kwargs)

        with patch.object(control_panel.asyncio, "to_thread", spy):
            self._stream(run_id)
        self.assertIn("_get_run_summary_or_404", offloaded)
        self.assertIn("_list_events_batch", offloaded)
        self.assertIn("get_run_summary", offloaded)

    def test_sse_slow_repository_does_not_stall_the_event_loop(self):
        import httpx

        run_id = self._finished_run()
        repository = self.app.state.coordinator.repository
        original = repository.list_events

        def slow_list_events(run, after_id=0):
            time.sleep(0.3)
            return original(run, after_id)

        cookies = dict(self.client.cookies)

        async def scenario():
            gaps = []

            async def ticker():
                previous = time.monotonic()
                while True:
                    await asyncio.sleep(0.02)
                    now = time.monotonic()
                    gaps.append(now - previous)
                    previous = now

            task = asyncio.create_task(ticker())
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver", cookies=cookies
            ) as client:
                response = await client.get(f"/api/checks/{run_id}/events")
            task.cancel()
            return response, gaps

        with patch.object(repository, "list_events", slow_list_events):
            response, gaps = asyncio.run(scenario())
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: run.completed", response.text)
        self.assertGreater(len(gaps), 5)
        # 0.3 s reads happen at least twice; an inline call would show a 0.3 s gap.
        self.assertLess(max(gaps), 0.2)

    def test_sse_event_batches_are_bounded_and_complete(self):
        run_id = self._finished_run()
        full = parse_sse(self._stream(run_id))
        repository = self.app.state.coordinator.repository
        original = repository.list_events
        requested = []

        def limited(run, after_id=0, limit=None):
            requested.append(limit)
            return original(run, after_id)[:limit]

        with patch.object(control_panel, "SSE_EVENT_BATCH", 1), patch.object(
            repository, "list_events", limited
        ):
            batched = parse_sse(self._stream(run_id))
        self.assertEqual(batched, full)
        self.assertTrue(requested)
        self.assertTrue(all(limit == 1 for limit in requested))

        # A repository without ``limit`` is still served (sent in chunks).
        with patch.object(control_panel, "SSE_EVENT_BATCH", 1):
            self.assertEqual(parse_sse(self._stream(run_id)), full)

    # ------------------------------------------------------------ heartbeat

    def test_heartbeat_reads_active_run_summary_without_the_coordinator_lock(self):
        release = threading.Event()

        class BlockingAdapter(_EmptyAdapter):
            def scan_and_inspect(self):
                release.wait(timeout=10)
                return []

        settings = Settings(
            database_url=f"sqlite:///{self.root / 'heartbeat.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "heartbeat-logs",
            log_heartbeat_seconds=0.05,
        )
        app = control_panel.create_app(
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: BlockingAdapter(options),
            initialize_schema=True,
        )
        with patch.object(
            RunCoordinator, "get_active_run", side_effect=AssertionError("heavy read")
        ), self.assertLogs(control_panel.logger, level="INFO") as logs:
            with TestClient(app) as client:
                client.post("/api/auth/login", json={"password": "correct horse"})
                run_id = client.post(
                    "/api/checks",
                    json={
                        "input_path": str(self.root),
                        "direction": "digital",
                        "create_pdfs": False,
                        "generate_previews": False,
                        "copy_failures": False,
                    },
                ).json()["id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not any(
                    f"active_run_id={run_id}" in line for line in logs.output
                ):
                    time.sleep(0.05)
                release.set()
                app.state.coordinator.wait_for(run_id, timeout=5)
        beats = [line for line in logs.output if "application.heartbeat" in line]
        self.assertTrue(any(f"active_run_id={run_id}" in line for line in beats), beats)
        self.assertFalse(any("heartbeat_failed" in line for line in beats), beats)

    # -------------------------------------------------------- order history

    def _seed_history(self, count: int) -> None:
        with self.app.state.database.session_factory() as session:
            run = CheckRun(input_path=str(self.root), direction="digital", status="completed")
            session.add(run)
            session.flush()
            for index in range(count):
                order = OrderResult(run_id=run.id, order_id=f"o{index}", customer_id=f"c{index}")
                session.add(order)
                session.flush()
                for side in ("face", "back"):
                    session.add(
                        FileResult(
                            order_result_id=order.id,
                            filename=f"{index}-{side}.tif",
                            source_path=str(self.root / f"{index}-{side}.tif"),
                            side=side,
                            preview_path=str(self.root / f"{index}-{side}.png"),
                        )
                    )
                session.add(
                    OrderAction(
                        order_result_id=order.id,
                        action="print",
                        created_at=datetime(2026, 1, 1, 0, 0, index % 60, tzinfo=timezone.utc),
                    )
                )
            session.commit()

    def _history_statement_count(self) -> tuple[int, int]:
        statements = []
        engine = self.app.state.database.engine

        def capture(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = self.client.get("/api/order-history", params={"limit": 100})
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        self.assertEqual(response.status_code, 200)
        return len(statements), len(response.json()["items"])

    def test_order_history_query_count_is_constant_per_page(self):
        self._seed_history(3)
        small_queries, small_items = self._history_statement_count()
        self._seed_history(40)
        large_queries, large_items = self._history_statement_count()
        self.assertEqual(small_items, 3)
        self.assertEqual(large_items, 43)
        self.assertEqual(small_queries, large_queries)
        self.assertLessEqual(large_queries, 8)  # session check + count + page + eager loads

    # ------------------------------------------------------- callas client

    def test_callas_clients_are_shared_per_configuration(self):
        settings = dataclasses.replace(
            self.app.state.settings,
            callas_url="http://callas.test",
            callas_token="token",
            callas_share=self.root,
            callas_server_share="/srv/share",
            callas_inkcoverage_enabled=True,
        )
        first = control_panel._shared_callas_client(settings)
        self.assertIs(first, control_panel._shared_callas_client(settings))
        processor = control_panel._callas_processor_factory(settings)
        self.assertIs(first, processor.keywords["callas_client"])
        with patch(
            "services.callas.ink_coverage.build_ink_coverage_checker"
        ) as build_checker:
            control_panel._ink_coverage_checker(settings)
        self.assertIs(build_checker.call_args.args[0], first)
        other = dataclasses.replace(settings, callas_url="http://other.test")
        self.assertIsNot(first, control_panel._shared_callas_client(other))


if __name__ == "__main__":
    unittest.main()
