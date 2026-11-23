"""Customer comment from Sborka ``orderinfo`` (``comment_client``) on the order card.

Fakes only: no Sborka, no production database.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import time

import pytest

from processing.models import FileCheck, OrderCheck, ParsedFilename
from server.database import Database
from services.batch_adapter import BatchProcessorAdapter, OrderArtifacts, ProcessingOptions
from services.coordinator import RunCoordinator
from services.dto import order_check_to_dto
from services.postpress import normalize_customer_comment
from services.repository import InMemoryRunRepository
from services.sql_repository import SqlRunRepository

MULTILINE = "друк з лицьової сторони\n\nколір не міняти\n  ВЕЛИКЕСЛОВО_" + "Ж" * 80
EVENT_TYPE = "order.customer_comment"


# --------------------------------------------------------------------------- helpers


def make_order(order_id: str, *, waiting: bool = False) -> OrderCheck:
    parsed = ParsedFilename(
        customer_id="42", order_id=order_id, width_mm=90, height_mm=50,
        front_colors=4, back_colors=0, side="face",
    )
    item = FileCheck(
        path=Path(f"/orders/{order_id}-face.jpg"), parsed=parsed,
        actual_width_mm=92, actual_height_mm=52, dpi=300, dpi_x=300, dpi_y=300,
        actual_format="JPEG", colorspace="CMYK",
        resample_decision="ask_confirmation" if waiting else "accept",
    )
    return OrderCheck(order_id=order_id, customer_id="42", files=[item])


class _Processor:
    def __init__(self, *_args, **_kwargs):
        self.on_orders_grouped = None

    def iter_inspect_orders(self):
        if self.on_orders_grouped is not None:
            self.on_orders_grouped(["100", "101"])
        yield OrderCheck("100", "42")
        yield OrderCheck("101", "42")


def make_adapter(tmp_path: Path, fetcher) -> BatchProcessorAdapter:
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=_Processor,
        order_info_fetcher=fetcher,
    )


class LateSborkaAdapter:
    """Fake adapter: the Sborka answer arrives when the test calls ``deliver``.

    Mirrors the BatchProcessorAdapter contract the coordinator relies on:
    ``set_order_info_listener`` + non-blocking ``customer_comment_for``.
    """

    pitstop_enabled = False

    def __init__(self, orders, comments):
        self.orders = orders
        self.comments = comments
        self.arrived = False
        self.listener = None
        self.process_started = threading.Event()
        self.release_processing = threading.Event()
        self.release_processing.set()
        self.shown = threading.Event()
        self.lookups = 0

    # protocol
    def set_order_info_listener(self, listener):
        self.listener = listener

    def customer_comment_for(self, order):
        self.lookups += 1
        return self.comments.get(order.order_id) if self.arrived else None

    def iter_inspect_orders(self):
        yield from self.orders
        self.shown.set()

    def pending_files(self, order):
        return [i for i in order.files if i.resample_decision == "ask_confirmation"]

    def decide(self, order, approved):
        for item in self.pending_files(order):
            item.resample_decision = "auto_correct" if approved else "reject"

    def process_order(self, order):
        self.process_started.set()
        self.release_processing.wait(timeout=5)
        return OrderArtifacts(
            pdf_path=Path(f"/out/{order.order_id}.pdf"),
            preview_paths=[Path(f"/out/{order.order_id}.png")],
        )

    def scan_and_inspect(self):
        return list(self.orders)

    # test control
    def deliver(self):
        self.arrived = True
        assert self.listener is not None
        self.listener()


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.fixture
def make_coordinator():
    created = []

    def build(adapter, repository=None):
        coordinator = RunCoordinator(
            repository or InMemoryRunRepository(), adapter_factory=lambda _options: adapter
        )
        created.append(coordinator)
        return coordinator

    yield build
    for coordinator in created:
        coordinator.shutdown()


def submit(coordinator):
    return coordinator.submit(ProcessingOptions(input_path="x", direction="digital"))["id"]


def event_types(coordinator, run_id):
    return [event.type for event in coordinator.events(run_id)]


# --------------------------------------------------------------- normalize / DTO


def test_normalize_keeps_every_line_and_unifies_line_breaks():
    assert normalize_customer_comment("а\r\n\r\nб\rв\n") == "а\n\nб\nв"
    assert normalize_customer_comment("  друк з лицьової сторони  ") == "друк з лицьової сторони"
    assert normalize_customer_comment(MULTILINE) == MULTILINE


@pytest.mark.parametrize("value", [None, "", "   \n \r\n\t", 0, 123, ["x"], {"a": 1}])
def test_normalize_treats_empty_and_non_text_as_no_comment(value):
    assert normalize_customer_comment(value) is None


def test_dto_carries_non_empty_comment_only():
    order = OrderCheck("100", "42", customer_comment=MULTILINE)
    assert order_check_to_dto(order)["customer_comment"] == MULTILINE
    assert "customer_comment" not in order_check_to_dto(OrderCheck("100", "42"))
    assert "customer_comment" not in order_check_to_dto(OrderCheck("100", "42", customer_comment=""))


# ----------------------------------------------------------------------- adapter


def test_adapter_takes_only_comment_client_for_main_and_sub_orders(tmp_path):
    requested = []

    def fetcher(order_ids):
        requested.append(list(order_ids))
        return [{
            "id": "100",
            "comment_client": "друк з лицьової сторони",
            "comment": "внутрішній коментар",
            "comment_manager": "менеджер",
            "sets_array": [{"id": "101", "comment_client": MULTILINE}],
        }]

    orders = make_adapter(tmp_path, fetcher).scan_and_inspect()

    assert requested == [["100", "101"]]  # the existing single batch request
    assert orders[0].customer_comment == "друк з лицьової сторони"
    assert orders[1].customer_comment == MULTILINE


def test_adapter_without_or_with_blank_comment_leaves_it_empty(tmp_path):
    orders = make_adapter(
        tmp_path,
        lambda _ids: [
            {"id": "100", "comment_client": "   "},
            {"id": "101", "post_text": "Сгиб: 1 пополам"},
        ],
    ).scan_and_inspect()

    assert [o.customer_comment for o in orders] == [None, None]
    assert orders[1].postpress["fold"]["type"] == "half-fold"  # postpress path unchanged


def test_sborka_failure_does_not_break_the_check(tmp_path):
    def fetcher(_ids):
        raise RuntimeError("sborka down")

    adapter = make_adapter(tmp_path, fetcher)
    orders = adapter.scan_and_inspect()

    assert [o.order_id for o in orders] == ["100", "101"]
    assert [o.customer_comment for o in orders] == [None, None]
    assert adapter.customer_comment_for(orders[0]) is None


def test_no_fetcher_means_no_comment_and_no_lookup(tmp_path):
    adapter = make_adapter(tmp_path, None)
    orders = adapter.scan_and_inspect()
    assert orders[0].customer_comment is None
    assert adapter.customer_comment_for(orders[0]) is None


def test_comment_lookup_never_waits_or_calls_sborka_again(tmp_path):
    release = threading.Event()
    calls = []

    def fetcher(ids):
        calls.append(list(ids))
        release.wait(timeout=5)
        return [{"id": "100", "comment_client": "пізня відповідь"}]

    adapter = make_adapter(tmp_path, fetcher)
    iterator = adapter.iter_inspect_orders()
    first = next(iterator)

    started = time.monotonic()
    assert adapter.customer_comment_for(first) is None  # answer not here yet: no wait
    assert time.monotonic() - started < 1
    assert first.customer_comment is None

    release.set()
    wait_until(lambda: adapter._order_info_future.done())
    assert adapter.customer_comment_for(first) == "пізня відповідь"
    assert calls == [["100", "101"]]  # still one request in total
    list(iterator)


def test_adapter_notifies_listener_once_when_answer_arrives(tmp_path):
    release = threading.Event()
    notified = threading.Event()

    def fetcher(_ids):
        release.wait(timeout=5)
        return [{"id": "100", "comment_client": "x"}]

    adapter = make_adapter(tmp_path, fetcher)
    adapter.set_order_info_listener(notified.set)
    iterator = adapter.iter_inspect_orders()
    next(iterator)
    assert not notified.is_set()
    release.set()
    assert notified.wait(timeout=5)
    list(iterator)


def test_failing_listener_and_failed_fetch_do_not_notify_or_raise(tmp_path):
    adapter = make_adapter(tmp_path, lambda _ids: [{"id": "100", "comment_client": "x"}])
    adapter.set_order_info_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert [o.order_id for o in adapter.scan_and_inspect()] == ["100", "101"]

    notified = threading.Event()

    def broken(_ids):
        raise RuntimeError("down")

    failing = make_adapter(tmp_path, broken)
    failing.set_order_info_listener(notified.set)
    failing.scan_and_inspect()
    assert not notified.wait(timeout=0.2)


# ------------------------------------------------------------ late answer (coordinator)


def test_late_answer_updates_card_waiting_for_confirmation(make_coordinator):
    adapter = LateSborkaAdapter(
        [make_order("200", waiting=True), make_order("201")],
        {"200": MULTILINE, "201": "друк з лицьової сторони"},
    )
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    run = coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=5)

    assert run["orders"]["200"]["status"] == "waiting_confirmation"
    assert "customer_comment" not in run["orders"]["200"]
    assert "customer_comment" not in run["orders"]["201"]  # passed, shown without it

    adapter.deliver()

    run = coordinator.get_run(run_id)
    assert run["orders"]["200"]["customer_comment"] == MULTILINE
    assert run["orders"]["200"]["status"] == "waiting_confirmation"
    assert run["orders"]["201"]["customer_comment"] == "друк з лицьової сторони"
    assert run["status"] == "waiting_confirmation"
    updates = [e for e in coordinator.events(run_id) if e.type == EVENT_TYPE]
    assert {e.data["order_id"] for e in updates} == {"200", "201"}
    waiting = next(e for e in updates if e.data["order_id"] == "200")
    assert waiting.data["order"]["customer_comment"] == MULTILINE  # the SSE card payload
    assert waiting.data["order"]["status"] == "waiting_confirmation"

    # a second delivery changes nothing and emits nothing
    adapter.deliver()
    assert len([e for e in coordinator.events(run_id) if e.type == EVENT_TYPE]) == 2


def test_comment_survives_correction_decision_and_completion(make_coordinator):
    adapter = LateSborkaAdapter([make_order("200", waiting=True)], {"200": MULTILINE})
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=5)
    adapter.deliver()

    after_decision = coordinator.confirm_correction(run_id, "200")
    assert after_decision["orders"]["200"]["customer_comment"] == MULTILINE

    completed = coordinator.wait_for(run_id, {"completed"}, timeout=5)
    assert completed["orders"]["200"]["status"] == "passed"
    assert completed["orders"]["200"]["customer_comment"] == MULTILINE
    done = next(e for e in coordinator.events(run_id) if e.type == "order.completed")
    assert done.data["order"]["customer_comment"] == MULTILINE


def test_answer_arriving_while_order_is_processed_is_not_lost_on_completion(make_coordinator):
    adapter = LateSborkaAdapter([make_order("300")], {"300": MULTILINE})
    adapter.release_processing.clear()
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    assert adapter.process_started.wait(timeout=5)

    adapter.deliver()  # order is mid-processing: card patched, OrderCheck updated
    assert coordinator.get_run(run_id)["orders"]["300"]["customer_comment"] == MULTILINE
    adapter.release_processing.set()  # completion rebuilds the card from the OrderCheck

    completed = coordinator.wait_for(run_id, {"completed"}, timeout=5)
    assert completed["orders"]["300"]["status"] == "passed"
    assert completed["orders"]["300"]["customer_comment"] == MULTILINE


def test_answer_after_completion_updates_the_finished_order_still_in_active_run(make_coordinator):
    adapter = LateSborkaAdapter(
        [make_order("400"), make_order("401", waiting=True)], {"400": "після завершення"}
    )
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=5)
    wait_until(lambda: coordinator.get_run(run_id)["orders"]["400"]["status"] == "passed")

    adapter.deliver()

    run = coordinator.get_run(run_id)
    assert run["orders"]["400"]["customer_comment"] == "після завершення"
    assert run["orders"]["400"]["status"] == "passed"
    assert "customer_comment" not in run["orders"]["401"]  # no comment in the record


def test_answer_before_order_is_registered_still_reaches_its_card(make_coordinator):
    """The Sborka answer lands between inspection of an order and its registration."""
    adapter = LateSborkaAdapter([make_order("500")], {"500": MULTILINE})
    original = adapter.iter_inspect_orders

    def iterate():
        for order in original():
            adapter.arrived = True  # answer is in, the listener runs on an empty context
            adapter.listener()
            yield order

    adapter.iter_inspect_orders = iterate
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    run = coordinator.wait_for(run_id, {"completed"}, timeout=5)

    assert run["orders"]["500"]["customer_comment"] == MULTILINE
    detected = next(e for e in coordinator.events(run_id) if e.type == "order.detected")
    assert detected.data["order"]["customer_comment"] == MULTILINE


def test_late_answer_for_a_released_run_is_ignored(make_coordinator):
    adapter = LateSborkaAdapter([make_order("600")], {"600": "пізно"})
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    coordinator.wait_for(run_id, {"completed"}, timeout=5)

    adapter.deliver()  # run is finished: nothing is rewritten

    assert "customer_comment" not in coordinator.get_run(run_id)["orders"]["600"]
    assert EVENT_TYPE not in event_types(coordinator, run_id)


def test_broken_comment_lookup_never_breaks_the_check(make_coordinator):
    adapter = LateSborkaAdapter([make_order("700")], {})

    def broken(_order):
        raise RuntimeError("sborka exploded")

    adapter.customer_comment_for = broken
    coordinator = make_coordinator(adapter)
    run = coordinator.wait_for(submit(coordinator), {"completed"}, timeout=5)

    assert run["orders"]["700"]["status"] == "passed"
    assert "customer_comment" not in run["orders"]["700"]


def test_adapter_without_comment_support_is_unaffected(make_coordinator):
    class Plain(LateSborkaAdapter):
        set_order_info_listener = None
        customer_comment_for = None

    adapter = Plain([make_order("800")], {})
    coordinator = make_coordinator(adapter)
    run = coordinator.wait_for(submit(coordinator), {"completed"}, timeout=5)
    assert run["orders"]["800"]["status"] == "passed"


# ------------------------------------------------------- persistence (SQL, no migration)


@pytest.fixture
def sql_database():
    with tempfile.TemporaryDirectory() as directory:
        database = Database(f"sqlite:///{Path(directory) / 'runs.sqlite3'}")
        database.create_schema()
        yield database
        database.dispose()


def test_comment_is_persisted_and_read_back_after_reload(make_coordinator, sql_database):
    adapter = LateSborkaAdapter(
        [make_order("200", waiting=True), make_order("201")],
        {"200": MULTILINE, "201": "друк з лицьової сторони"},
    )
    repository = SqlRunRepository(sql_database.session_factory, recover_interrupted=False)
    coordinator = make_coordinator(adapter, repository)
    run_id = submit(coordinator)
    coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=5)
    adapter.deliver()

    # "page reload" = a fresh repository reading only what reached the database
    reopened = SqlRunRepository(sql_database.session_factory, recover_interrupted=False)
    stored = reopened.get_run(run_id)
    assert stored["orders"]["200"]["customer_comment"] == MULTILINE
    assert stored["orders"]["201"]["customer_comment"] == "друк з лицьової сторони"

    page = reopened.list_orders_page(
        run_id, page=1, page_size=50, status="all", search="", active_only=True
    )
    by_id = {item["order_id"]: item for item in page["items"]}
    assert by_id["200"]["customer_comment"] == MULTILINE
    assert by_id["201"]["customer_comment"] == "друк з лицьової сторони"


def test_comment_persisted_when_answer_is_early_and_run_completes(make_coordinator, sql_database):
    adapter = LateSborkaAdapter([make_order("300")], {"300": MULTILINE})
    adapter.arrived = True  # answer already here when the order is first built
    repository = SqlRunRepository(sql_database.session_factory, recover_interrupted=False)
    coordinator = make_coordinator(adapter, repository)
    run_id = submit(coordinator)
    coordinator.wait_for(run_id, {"completed"}, timeout=5)

    reopened = SqlRunRepository(sql_database.session_factory, recover_interrupted=False)
    assert reopened.get_run(run_id)["orders"]["300"]["customer_comment"] == MULTILINE


def test_historic_run_without_comment_reads_as_before(sql_database):
    repository = SqlRunRepository(sql_database.session_factory, recover_interrupted=False)
    from tests.test_sql_run_repository import sample_run

    repository.create_run(sample_run())
    order = repository.get_run("run-1")["orders"]["25506185"]
    assert "customer_comment" not in order
