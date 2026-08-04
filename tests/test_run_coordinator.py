import json
from pathlib import Path
import threading
import unittest

from processing.models import FileCheck, OrderCheck, ParsedFilename
from services.batch_adapter import OrderArtifacts, ProcessingOptions
from services.coordinator import InvalidRunStateError, RunCoordinator
from services.dto import order_check_to_dto
from services.repository import InMemoryRunRepository


def make_order(order_id: str, *, waiting: bool = False, warning: bool = False):
    parsed = ParsedFilename(
        customer_id="42",
        order_id=order_id,
        width_mm=90,
        height_mm=50,
        front_colors=4,
        back_colors=0,
        side="face",
    )
    item = FileCheck(
        path=Path(f"/orders/{order_id}-face.jpg"),
        parsed=parsed,
        actual_width_mm=92,
        actual_height_mm=52,
        dpi=300,
        dpi_x=300,
        dpi_y=300,
        actual_format="JPEG",
        colorspace="CMYK",
        resample_decision="ask_confirmation" if waiting else "accept",
    )
    if warning:
        item.warnings.append("проверить цвет")
    return OrderCheck(order_id=order_id, customer_id="42", files=[item])


class FakeAdapter:
    def __init__(
        self,
        orders,
        *,
        calls=None,
        scan_error=None,
        processing_errors=None,
    ):
        self.orders = orders
        self.calls = calls if calls is not None else []
        self.scan_error = scan_error
        self.processing_errors = processing_errors or {}

    def scan_and_inspect(self):
        self.calls.append("scan")
        if self.scan_error:
            raise self.scan_error
        return self.orders

    def pending_files(self, order):
        return [
            item
            for item in order.files
            if item.resample_decision == "ask_confirmation"
        ]

    def decide(self, order, approved):
        self.calls.append(("decide", order.order_id, approved))
        for item in self.pending_files(order):
            item.resample_confirmed = approved
            item.resample_decision = "auto_correct" if approved else "reject"
            item.needs_resample = approved
            if not approved:
                item.errors.append("коррекция отклонена")

    def process_order(self, order):
        self.calls.append(("process", order.order_id))
        errors = self.processing_errors.get(order.order_id, [])
        return OrderArtifacts(
            pdf_path=Path(f"/out/{order.order_id}.pdf") if not errors else None,
            preview_paths=(
                [Path(f"/out/{order.order_id}-face.png")] if not errors else []
            ),
            errors=errors,
        )


class BlockingAdapter(FakeAdapter):
    def __init__(self, orders, block_order):
        super().__init__(orders)
        self.block_order = block_order
        self.processing_started = threading.Event()
        self.release_processing = threading.Event()

    def process_order(self, order):
        if order.order_id == self.block_order:
            self.processing_started.set()
            self.release_processing.wait(timeout=2)
        return super().process_order(order)


class ParallelBlockingAdapter(FakeAdapter):
    def __init__(self, orders):
        super().__init__(orders)
        self.processing_started = threading.Event()
        self.release_processing = threading.Event()
        self._started_order_ids = set()
        self._started_lock = threading.Lock()

    def process_order(self, order):
        with self._started_lock:
            self._started_order_ids.add(order.order_id)
            if len(self._started_order_ids) == 2:
                self.processing_started.set()
        self.release_processing.wait(timeout=2)
        return super().process_order(order)


class PitStopBlockingAdapter(FakeAdapter):
    pitstop_enabled = True

    def __init__(self, orders):
        super().__init__(orders)
        self.processing_started = threading.Event()
        self.release_processing = threading.Event()

    def process_order(self, order):
        self.processing_started.set()
        self.release_processing.wait(timeout=2)
        return OrderArtifacts(
            pdf_path=Path(f"/out/{order.order_id}.pdf"),
            preview_paths=[Path(f"/out/{order.order_id}.png")],
            pitstop={
                "check_id": "pitstop-1",
                "execution_status": "completed",
                "verdict": "error",
                "checked_revision": 1,
                "profile": {"key": "digital", "name": "Test", "version": None},
                "counts": {"errors": 1, "warnings": 0},
                "issues": [],
                "reports": {"json_url": None, "xml_url": None},
            },
            current_pdf_revision=1,
            current_pdf_sha256="a" * 64,
        )


class IncrementalBlockingAdapter(FakeAdapter):
    def __init__(self, orders):
        super().__init__(orders)
        self.second_inspection_started = threading.Event()
        self.release_second_inspection = threading.Event()
        self.total_orders = len(orders)

    def iter_inspect_orders(self):
        yield self.orders[0]
        self.second_inspection_started.set()
        self.release_second_inspection.wait(timeout=2)
        yield self.orders[1]


class RunCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryRunRepository()
        self.adapters = {}

        def factory(options):
            return self.adapters[options.input_path]

        self.coordinator = RunCoordinator(
            self.repository, adapter_factory=factory
        )

    def tearDown(self):
        self.coordinator.shutdown()

    def submit(self, name):
        return self.coordinator.submit(
            ProcessingOptions(input_path=name, direction="digital")
        )

    def test_waiting_order_does_not_block_other_orders(self):
        calls = []
        self.adapters["first"] = FakeAdapter(
            [make_order("needs-decision", waiting=True), make_order("ready")],
            calls=calls,
        )
        run_id = self.submit("first")["id"]
        run = self.coordinator.wait_for(
            run_id, {"waiting_confirmation"}, timeout=2
        )

        self.assertEqual(run["orders"]["needs-decision"]["status"], "waiting_confirmation")
        self.assertEqual(run["orders"]["ready"]["status"], "passed")
        self.assertIn(("process", "ready"), calls)
        self.assertNotIn(("process", "needs-decision"), calls)

        self.coordinator.confirm_correction(run_id, "needs-decision")
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)
        self.assertEqual(completed["processed_orders"], 2)
        self.assertEqual(completed["orders"]["needs-decision"]["status"], "passed")

        event_types = [event.type for event in self.coordinator.events(run_id)]
        self.assertLess(
            event_types.index("order.waiting_confirmation"),
            event_types.index("order.completed"),
        )
        self.assertIn("order.correction_confirmed", event_types)
        self.assertEqual(event_types[-1], "run.completed")

    def test_first_order_events_are_persisted_before_second_inspection_finishes(self):
        adapter = IncrementalBlockingAdapter(
            [make_order("first"), make_order("second")]
        )
        self.adapters["incremental"] = adapter
        run_id = self.submit("incremental")["id"]

        self.assertTrue(adapter.second_inspection_started.wait(timeout=2))
        events_before_release = self.coordinator.events(run_id)
        detected_ids = [
            event.data["order_id"]
            for event in events_before_release
            if event.type == "order.detected"
        ]
        checked_ids = [
            event.data["order_id"]
            for event in events_before_release
            if event.type == "order.checked"
        ]
        self.assertEqual(detected_ids, ["first"])
        self.assertEqual(checked_ids, ["first"])
        self.assertEqual(
            self.coordinator.get_run(run_id)["orders"]["first"]["status"],
            "passed",
        )

        adapter.release_second_inspection.set()
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)
        self.assertEqual(completed["total_orders"], 2)
        self.assertIn("second", completed["orders"])

    def test_pitstop_order_never_becomes_terminal_green_before_final_result(self):
        adapter = PitStopBlockingAdapter([make_order("pitstop-order")])
        self.adapters["pitstop"] = adapter
        run_id = self.submit("pitstop")["id"]

        self.assertTrue(adapter.processing_started.wait(timeout=2))
        pending = self.coordinator.get_run(run_id)["orders"]["pitstop-order"]
        self.assertEqual(pending["source_status"], "passed")
        self.assertEqual(pending["status"], "processing")
        checked = next(
            event
            for event in self.coordinator.events(run_id)
            if event.type == "order.checked"
        )
        self.assertEqual(checked.data["status"], "processing")

        adapter.release_processing.set()
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)
        final = completed["orders"]["pitstop-order"]
        self.assertEqual(final["status"], "error")
        self.assertFalse(final["passed"])
        event_types = [event.type for event in self.coordinator.events(run_id)]
        self.assertIn("pitstop.check_completed", event_types)

    def test_independent_orders_process_in_parallel(self):
        adapter = ParallelBlockingAdapter([make_order("first"), make_order("second")])
        self.adapters["parallel"] = adapter

        run_id = self.submit("parallel")["id"]

        self.assertTrue(adapter.processing_started.wait(timeout=2))
        adapter.release_processing.set()
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)
        self.assertEqual(completed["processed_orders"], 2)

        event_types = [event.type for event in self.coordinator.events(run_id)]
        self.assertEqual(event_types.count("order.completed"), 2)

    def test_rejection_completes_order_as_error_and_cannot_be_repeated(self):
        self.adapters["reject"] = FakeAdapter(
            [make_order("order-1", waiting=True)]
        )
        run_id = self.submit("reject")["id"]
        self.coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=2)
        self.coordinator.reject_correction(run_id, "order-1")
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)

        self.assertEqual(completed["failed_orders"], 1)
        self.assertEqual(completed["orders"]["order-1"]["status"], "error")
        with self.assertRaises(InvalidRunStateError):
            self.coordinator.reject_correction(run_id, "order-1")

    def test_correction_policy_can_approve_without_waiting(self):
        calls = []
        self.adapters["auto"] = FakeAdapter(
            [make_order("order-1", waiting=True)], calls=calls
        )
        run = self.coordinator.submit(
            ProcessingOptions(
                input_path="auto",
                direction="digital",
                correction_policy="auto",
            )
        )
        completed = self.coordinator.wait_for(run["id"], {"completed"}, timeout=2)
        self.assertEqual(completed["orders"]["order-1"]["status"], "passed")
        self.assertIn(("decide", "order-1", True), calls)

    def test_correction_policy_can_reject_without_waiting(self):
        calls = []
        self.adapters["auto-reject"] = FakeAdapter(
            [make_order("order-1", waiting=True)], calls=calls
        )
        run = self.coordinator.submit(
            ProcessingOptions(
                input_path="auto-reject",
                direction="digital",
                correction_policy="reject",
            )
        )
        completed = self.coordinator.wait_for(run["id"], {"completed"}, timeout=2)
        self.assertEqual(completed["orders"]["order-1"]["status"], "error")
        self.assertIn(("decide", "order-1", False), calls)

    def test_waiting_run_releases_worker_for_next_run(self):
        self.adapters["blocking"] = FakeAdapter(
            [make_order("order-1", waiting=True)]
        )
        self.adapters["next"] = FakeAdapter([make_order("order-2")])
        first_id = self.submit("blocking")["id"]
        self.coordinator.wait_for(first_id, {"waiting_confirmation"}, timeout=2)
        second_id = self.submit("next")["id"]

        second = self.coordinator.wait_for(second_id, {"completed"}, timeout=2)
        self.assertEqual(second["orders"]["order-2"]["status"], "passed")
        self.assertEqual(
            self.coordinator.get_run(first_id)["status"], "waiting_confirmation"
        )
        self.coordinator.confirm_correction(first_id, "order-1")
        self.coordinator.wait_for(first_id, {"completed"}, timeout=2)

    def test_worker_failure_is_persisted_and_next_run_still_runs(self):
        self.adapters["broken"] = FakeAdapter(
            [], scan_error=RuntimeError("ImageMagick unavailable")
        )
        self.adapters["healthy"] = FakeAdapter([make_order("order-2")])
        broken_id = self.submit("broken")["id"]
        healthy_id = self.submit("healthy")["id"]

        broken = self.coordinator.wait_for(broken_id, {"failed"}, timeout=2)
        healthy = self.coordinator.wait_for(healthy_id, {"completed"}, timeout=2)
        self.assertIn("ImageMagick unavailable", broken["error"])
        self.assertEqual(healthy["status"], "completed")
        self.assertEqual(
            [event.type for event in self.coordinator.events(broken_id)][-1],
            "run.failed",
        )

    def test_stale_terminal_active_id_does_not_block_new_run(self):
        self.adapters["first"] = FakeAdapter([])
        first_id = self.submit("first")["id"]
        self.coordinator.wait_for(first_id, {"completed"}, timeout=2)
        # Reproduce a long-running process whose in-memory active pointer was
        # not updated after another process finalized the database row.
        self.coordinator._active_run_id = first_id
        self.adapters["second"] = FakeAdapter([make_order("order-2")])

        second_id = self.submit("second")["id"]
        completed = self.coordinator.wait_for(
            second_id, {"completed"}, timeout=2
        )

        self.assertEqual(completed["orders"]["order-2"]["status"], "passed")

    def test_cancel_queued_run_never_executes_it(self):
        self.adapters["blocking"] = FakeAdapter(
            [make_order("order-1", waiting=True)]
        )
        queued_calls = []
        self.adapters["queued"] = FakeAdapter(
            [make_order("order-2")], calls=queued_calls
        )
        first_id = self.submit("blocking")["id"]
        self.coordinator.wait_for(first_id, {"waiting_confirmation"}, timeout=2)
        queued_id = self.submit("queued")["id"]

        cancelled = self.coordinator.cancel(queued_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(queued_calls, [])

    def test_cancel_waiting_run_releases_next_run(self):
        self.adapters["waiting"] = FakeAdapter(
            [make_order("order-1", waiting=True)]
        )
        self.adapters["after-cancel"] = FakeAdapter([make_order("order-2")])
        first_id = self.submit("waiting")["id"]
        self.coordinator.wait_for(first_id, {"waiting_confirmation"}, timeout=2)
        second_id = self.submit("after-cancel")["id"]

        cancelled = self.coordinator.cancel(first_id)
        completed = self.coordinator.wait_for(second_id, {"completed"}, timeout=2)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(completed["status"], "completed")

    def test_processing_tool_error_is_an_order_error_not_a_run_failure(self):
        self.adapters["tool-error"] = FakeAdapter(
            [make_order("bad-pdf"), make_order("good")],
            processing_errors={"bad-pdf": ["PDF: Ghostscript failed"]},
        )
        run_id = self.submit("tool-error")["id"]
        completed = self.coordinator.wait_for(run_id, {"completed"}, timeout=2)

        self.assertEqual(completed["failed_orders"], 1)
        self.assertEqual(completed["passed_orders"], 1)
        self.assertIn(
            "Ghostscript",
            completed["orders"]["bad-pdf"]["processing_errors"][0],
        )

    def test_cancel_discards_a_confirmation_resume_already_in_queue(self):
        adapter = BlockingAdapter(
            [make_order("waiting", waiting=True), make_order("slow")],
            block_order="slow",
        )
        self.adapters["cancel-race"] = adapter
        run_id = self.submit("cancel-race")["id"]
        self.assertTrue(adapter.processing_started.wait(timeout=2))
        self.coordinator.confirm_correction(run_id, "waiting")
        self.coordinator.cancel(run_id)
        adapter.release_processing.set()

        cancelled = self.coordinator.wait_for(run_id, {"cancelled"}, timeout=2)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn(("process", "waiting"), adapter.calls)

    def test_event_ids_are_replayable_for_sse(self):
        self.adapters["events"] = FakeAdapter([make_order("one")])
        run_id = self.submit("events")["id"]
        self.coordinator.wait_for(run_id, {"completed"}, timeout=2)
        events = self.coordinator.events(run_id)

        self.assertEqual([event.id for event in events], list(range(1, len(events) + 1)))
        replay = self.coordinator.events(run_id, after_id=events[-2].id)
        self.assertEqual([event.id for event in replay], [events[-1].id])
        self.assertIn(f"id: {events[0].id}\n", events[0].as_sse())
        self.assertIn(f"event: {events[0].type}\n", events[0].as_sse())


class DtoTests(unittest.TestCase):
    def test_order_and_file_dto_are_json_safe(self):
        dto = order_check_to_dto(make_order("123", warning=True))
        encoded = json.dumps(dto)
        self.assertIn("/orders/123-face.jpg", encoded)
        self.assertEqual(dto["files"][0]["resample_crop_mm"], [0.0, 0.0])
        self.assertEqual(dto["status"], "warning")


if __name__ == "__main__":
    unittest.main()
