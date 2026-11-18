"""Комплекты Sborka: возврат одной операцией через главный заказ (комплект 26234881)."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from server.database import Database
from server.models import CheckRun, OrderAction, OrderResult
from services.file_lifecycle import FileTransition
from services.order_kits import KitResolver, kits_from_order_info
from services.order_workflow import OrderActionCommand, OrderWorkflowService
from services.sborka_integration import SborkaIntegrationError

MAIN, SECOND, THIRD, PLAIN = "26234881", "26234882", "26234883", "26234424"
ORDER_INFO = [
    {"id": MAIN, "status": "5", "sets_array": [{"id": SECOND, "status": "5"}, {"id": THIRD, "status": "5"}]},
    {"id": PLAIN, "status": "5"},
]


def test_kits_from_order_info_maps_every_member_to_main():
    kits = kits_from_order_info(ORDER_INFO)
    assert set(kits) == {MAIN, SECOND, THIRD}
    assert kits[THIRD] == {
        "main_id": MAIN, "member_ids": [MAIN, SECOND, THIRD], "position": 3, "size": 3,
    }
    assert kits[MAIN]["position"] == 1


def test_resolver_prefers_stored_kit_and_caches_live_lookup():
    calls = []
    resolver = KitResolver(lambda ids: calls.append(list(ids)) or ORDER_INFO)
    stored_run = {"id": "r1", "orders": {"a": {"order_id": SECOND, "kit": {"main_id": MAIN}}}}
    assert resolver.for_run(stored_run)[SECOND]["main_id"] == MAIN
    assert calls == []

    run = {"id": "r2", "orders": {MAIN: {"order_id": MAIN}, SECOND: {"order_id": SECOND}}}
    assert resolver.for_run(run)[SECOND]["main_id"] == MAIN
    assert resolver.for_run(run)[MAIN]["position"] == 1
    assert calls == [[MAIN, SECOND]]  # один запрос на запуск
    assert resolver.for_run({"id": "r3", "orders": {"x": {"order_id": MAIN}}}, live=False) == {}


def test_resolver_failure_is_not_cached():
    attempts = []

    def fetcher(ids):
        attempts.append(ids)
        if len(attempts) == 1:
            raise SborkaIntegrationError("timeout")
        return ORDER_INFO

    resolver = KitResolver(fetcher)
    run = {"id": "r", "orders": {MAIN: {"order_id": MAIN}}}
    assert resolver.for_run(run) == {}
    assert resolver.for_run(run)[MAIN]["size"] == 3


def _sborka_orderinfo(ids):
    """Как настоящая Sborka: комплект — только в записи запрошенного главного заказа."""
    return [record for record in ORDER_INFO if record["id"] in ids]


def _kit_file(order_id, position):
    return [{"name": f"23_KS(K)_Bezlam_300_(90x50)_4-4_L212_T300_(36056-{order_id})_{position}.pdf"}]


def test_resolver_finds_main_order_outside_the_run_by_file_name():
    """Проверка 23:42: главный 26234881 уже в Troubles, в запуске только подзаказы."""
    calls = []
    resolver = KitResolver(lambda ids: calls.append(list(ids)) or _sborka_orderinfo(ids))
    run = {"id": "r", "orders": {
        SECOND: {"order_id": SECOND, "files": _kit_file(SECOND, 2)},
        THIRD: {"order_id": THIRD, "files": _kit_file(THIRD, 3)},
    }}

    kits = resolver.for_run(run)

    assert calls == [[SECOND, THIRD], [MAIN]]
    assert kits[SECOND]["main_id"] == MAIN and kits[THIRD]["position"] == 3


def test_resolver_merges_stored_kits_with_live_lookup():
    other = {"id": "9000001", "sets_array": [{"id": "9000002"}]}
    resolver = KitResolver(lambda ids: [r for r in [*ORDER_INFO, other] if r["id"] in ids])
    run = {"id": "r", "orders": {
        MAIN: {"order_id": MAIN, "kit": {"main_id": MAIN, "member_ids": [MAIN, SECOND, THIRD], "position": 1, "size": 3}},
        "9000002": {"order_id": "9000002", "files": _kit_file("9000002", 2)},
    }}
    kits = resolver.for_run(run)
    assert kits[MAIN]["main_id"] == MAIN and kits["9000002"]["main_id"] == "9000001"
    assert resolver.for_run(run, live=False)["9000002"]["main_id"] == "9000001"  # из кэша


def test_resolver_ignores_unconfirmed_candidate():
    resolver = KitResolver(_sborka_orderinfo)
    run = {"id": "r", "orders": {"5000002": {"order_id": "5000002", "files": _kit_file("5000002", 2)}}}
    assert resolver.for_run(run) == {}


class Lifecycle:
    calls: list[str] = []
    guard = Lock()

    def __init__(self, _input_dir: Path) -> None:
        pass

    def return_for_rework(self, order, **_kwargs):
        with self.guard:
            type(self).calls.append(str(order["order_id"]))
        return FileTransition({}, None, [])

    def accept_for_print(self, order, **_kwargs):
        with self.guard:
            type(self).calls.append(f"print:{order['order_id']}")
        return FileTransition({}, None, [])

    def rollback(self, _transition):
        return None


class Coordinator:
    def __init__(self, run):
        self.run = run
        self.guard = Lock()

    def apply_file_transition(self, _run_id, aggregate_id, **values):
        with self.guard:
            self.run["orders"][aggregate_id]["status"] = values["status"]

    def restore_order_snapshot(self, _run_id, aggregate_id, previous):
        with self.guard:
            self.run["orders"][aggregate_id] = dict(previous)


class KitReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Database(f"sqlite:///{Path(self.tmp.name) / 'state.sqlite3'}")
        self.database.create_schema()
        ids = [MAIN, SECOND, THIRD, PLAIN]
        with self.database.session_factory() as session, session.begin():
            session.add(CheckRun(id="run-1", input_path=self.tmp.name, direction="digital", status="completed"))
            for index, order_id in enumerate(ids, start=1):
                session.add(OrderResult(id=index, run_id="run-1", order_id=order_id, status="warning", passed=True))
        self.run = {
            "id": "run-1",
            "options": {"input_path": self.tmp.name},
            "orders": {
                order_id: {
                    "order_id": order_id, "aggregate_id": order_id, "status": "warning", "passed": True,
                    "files": [], "pdf_path": None, "preview_paths": [],
                }
                for order_id in ids
            },
        }
        self.sent: list[tuple[str, str]] = []
        self.orderinfo_calls: list[list[str]] = []
        Lifecycle.calls = []

    def tearDown(self):
        self.database.dispose()
        self.tmp.cleanup()

    def _sender(self, order_id, comment, _preview, _design, _cost):
        self.sent.append((order_id, comment))
        if order_id != MAIN and order_id != PLAIN:
            # Так Sborka отвечает на возврат подзаказа комплекта.
            raise SborkaIntegrationError("Sborka вернула пустой ответ на возврат заказа")
        return {"http_status": 200, "response": {"raw": f"Заказ №{order_id} возвращен"}}

    def _service(self, sender=None, fetcher=None):
        def finder(order_ref, _run_id):
            return self.run, self.run["orders"][order_ref]

        def default_fetcher(ids):
            self.orderinfo_calls.append(list(ids))
            return ORDER_INFO

        return OrderWorkflowService(
            Coordinator(self.run),
            self.database.session_factory,
            finder,
            lifecycle_factory=Lifecycle,
            rework_sender=sender or self._sender,
            kit_resolver=KitResolver(fetcher or default_fetcher),
        )

    def _reject(self, service, *ids, comment="Низька роздільна здатність"):
        return service.prepare(OrderActionCommand(tuple(ids), run_id="run-1", comment=comment), "reject")["items"]

    def _status(self, order_id):
        return self.run["orders"][order_id]["status"]

    def test_main_return_sends_only_main_and_returns_whole_kit_locally(self):
        items = self._reject(self._service(), MAIN)

        self.assertEqual(self.sent, [(MAIN, "Низька роздільна здатність")])
        self.assertEqual({item["order_id"]: item["status"] for item in items},
                         {MAIN: "prepared", SECOND: "prepared", THIRD: "prepared"})
        for order_id in (MAIN, SECOND, THIRD):
            self.assertEqual(self._status(order_id), "returned_for_rework")
        self.assertEqual(self._status(PLAIN), "warning")
        member = next(item for item in items if item["order_id"] == SECOND)
        self.assertTrue(member["prepress"]["returned_with_kit"])
        self.assertEqual(member["prepress"]["kit_main_id"], MAIN)
        self.assertEqual(sorted(Lifecycle.calls), sorted([MAIN, SECOND, THIRD]))

    def test_selecting_only_a_member_returns_kit_through_main(self):
        items = self._reject(self._service(), THIRD)

        self.assertEqual([order_id for order_id, _ in self.sent], [MAIN])
        self.assertEqual(self._status(MAIN), "returned_for_rework")
        self.assertEqual(self._status(SECOND), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        self.assertIn(THIRD, {item["order_id"] for item in items if item["status"] == "prepared"})

    def test_parallel_requests_like_the_ui_call_sborka_once(self):
        service = self._service()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda order_id: self._reject(service, order_id), [SECOND, THIRD, MAIN, PLAIN]))

        self.assertEqual(sorted(order_id for order_id, _ in self.sent), sorted([MAIN, PLAIN]))
        for order_id in (MAIN, SECOND, THIRD, PLAIN):
            self.assertEqual(self._status(order_id), "returned_for_rework")
        flat = [item for result in results for item in result]
        self.assertFalse([item for item in flat if item["status"] not in {"prepared"}], flat)

    def test_desynced_kit_is_caught_up_without_calling_sborka(self):
        """Рассинхрон 18.11: главный уже возвращён, подзаказы остались активными."""
        self.run["orders"][MAIN]["status"] = "returned_for_rework"

        items = self._reject(self._service(), SECOND, THIRD)

        self.assertEqual(self.sent, [])
        self.assertEqual(self._status(SECOND), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        self.assertEqual({item["status"] for item in items}, {"prepared"})
        with self.database.session_factory() as session:
            actions = session.query(OrderAction).all()
            self.assertTrue(all(action.status == "prepared" for action in actions))

    def test_failed_main_return_keeps_kit_untouched(self):
        def failing(order_id, *_args):
            raise SborkaIntegrationError("Sborka не отвечает")

        items = self._reject(self._service(sender=failing), SECOND)

        for order_id in (MAIN, SECOND, THIRD):
            self.assertEqual(self._status(order_id), "warning")
        member = next(item for item in items if item["order_id"] == SECOND)
        self.assertEqual(member["code"], "kit_main_failed")
        self.assertIn("Sborka не отвечает", member["message"])

    def test_main_not_yet_in_run_is_returned_by_its_number(self):
        """Часть комплекта пришла раньше главного: возврат уходит под номером главного."""
        del self.run["orders"][MAIN]

        items = self._reject(self._service(), SECOND)

        self.assertEqual(self.sent, [(MAIN, "Низька роздільна здатність")])
        self.assertEqual(self._status(SECOND), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        carrier = next(item for item in items if item["order_id"] == SECOND)
        self.assertEqual(carrier["prepress"]["kit_main_id"], MAIN)
        self.assertTrue(carrier["prepress"]["sent_as_kit_main"])

    def test_rest_of_kit_in_a_later_run_is_caught_up_without_sborka(self):
        del self.run["orders"][MAIN]
        del self.run["orders"][THIRD]
        self._reject(self._service(), SECOND)
        self.assertEqual([order_id for order_id, _ in self.sent], [MAIN])

        # Следующая проверка: пришли главный и третий заказ комплекта.
        self.run["orders"][MAIN] = {"order_id": MAIN, "aggregate_id": MAIN, "status": "warning", "files": [], "preview_paths": []}
        self.run["orders"][THIRD] = {"order_id": THIRD, "aggregate_id": THIRD, "status": "warning", "files": [], "preview_paths": []}
        items = self._reject(self._service(), MAIN)

        self.assertEqual([order_id for order_id, _ in self.sent], [MAIN])  # второго запроса нет
        self.assertEqual(self._status(MAIN), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        self.assertTrue(all(item.get("prepress", {}).get("returned_with_kit") for item in items))

    def test_orders_still_being_checked_are_not_touched(self):
        """18.11 23:52: возврат забрал заказы посреди проверки, и её итог перезаписал статус."""
        service = self._service()
        busy = {THIRD}
        service.coordinator.order_in_progress = lambda _run, ref: ref in busy

        items = self._reject(service, MAIN)

        self.assertEqual(self._status(MAIN), "returned_for_rework")
        self.assertEqual(self._status(SECOND), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "warning")
        self.assertNotIn(THIRD, Lifecycle.calls)
        busy_item = next(item for item in items if item["order_id"] == THIRD)
        self.assertEqual(busy_item["code"], "order_in_progress")

        busy.clear()  # проверка закончилась — догоняем без Sborka
        self._reject(service, THIRD)
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        self.assertEqual([order_id for order_id, _ in self.sent], [MAIN])

    def test_plain_order_still_being_checked_is_refused(self):
        service = self._service()
        service.coordinator.order_in_progress = lambda _run, ref: ref == PLAIN

        items = self._reject(service, PLAIN)

        self.assertEqual(items[0]["code"], "order_in_progress")
        self.assertEqual(self.sent, [])
        self.assertEqual(self._status(PLAIN), "warning")

    def test_main_returned_in_earlier_run_catches_up_members_locally(self):
        """Повторная проверка без главного заказа: он возвращён в прошлой проверке."""
        del self.run["orders"][MAIN]
        self.run["orders"][SECOND]["files"] = _kit_file(SECOND, 2)
        self.run["orders"][THIRD]["files"] = _kit_file(THIRD, 3)
        with self.database.session_factory() as session, session.begin():
            session.add(CheckRun(id="run-0", input_path=self.tmp.name, direction="digital", status="completed"))
            session.add(OrderResult(id=50, run_id="run-0", order_id=MAIN, status="returned_for_rework", passed=True))

        items = self._reject(self._service(fetcher=_sborka_orderinfo), THIRD)

        self.assertEqual(self.sent, [])
        self.assertEqual(self._status(SECOND), "returned_for_rework")
        self.assertEqual(self._status(THIRD), "returned_for_rework")
        self.assertEqual({item["status"] for item in items}, {"prepared"})

    def test_orderinfo_failure_falls_back_to_plain_return(self):
        def broken(_ids):
            raise SborkaIntegrationError("timeout")

        items = self._reject(self._service(fetcher=broken), PLAIN)

        self.assertEqual(self.sent, [(PLAIN, "Низька роздільна здатність")])
        self.assertEqual(items[0]["status"], "prepared")

    def test_printed_member_is_reported(self):
        self.run["orders"][THIRD]["status"] = "accepted_for_print"

        items = self._reject(self._service(), MAIN)

        printed = next(item for item in items if item["order_id"] == THIRD)
        self.assertEqual(printed["code"], "kit_member_printed")
        self.assertEqual(self._status(THIRD), "accepted_for_print")

    def test_print_of_member_is_blocked_after_kit_return(self):
        self.run["orders"][MAIN]["status"] = "returned_for_rework"
        for order in self.run["orders"].values():
            order["kit"] = kits_from_order_info(ORDER_INFO).get(order["order_id"])
            if order["kit"] is None:
                del order["kit"]
        service = self._service()

        items = service.prepare(OrderActionCommand((SECOND,), run_id="run-1"), "print")["items"]

        self.assertEqual(items[0]["code"], "kit_returned")
        self.assertNotIn(f"print:{SECOND}", Lifecycle.calls)
        self.assertEqual(self.orderinfo_calls, [])  # печать не ходит в Sborka за составом


if __name__ == "__main__":
    unittest.main()


def test_check_stores_kit_on_orders_and_dto(tmp_path):
    from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
    from services.dto import order_check_to_dto
    from tests.test_postpress import _InspectingProcessor

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=_InspectingProcessor,
        order_info_fetcher=lambda _ids: [{"id": "100", "sets_array": [{"id": "101"}]}],
    )

    orders = adapter.scan_and_inspect()

    assert orders[0].kit == {"main_id": "100", "member_ids": ["100", "101"], "position": 1, "size": 2}
    assert orders[1].kit["position"] == 2
    assert order_check_to_dto(orders[1])["kit"]["main_id"] == "100"


def test_coordinator_reports_orders_still_being_processed():
    import threading
    from types import SimpleNamespace

    from services.coordinator import RunCoordinator

    coordinator = object.__new__(RunCoordinator)
    coordinator._lock = threading.RLock()
    run = {"orders": {
        "1": {"order_id": "1", "aggregate_id": "a1", "status": "error"},
        "2": {"order_id": "2", "aggregate_id": "a2", "status": "warning"},
        "3": {"order_id": "3", "aggregate_id": "a3", "status": "waiting_confirmation"},
    }}
    context = SimpleNamespace(run=run, orders={"1": 0, "2": 0, "3": 0}, finished_keys={"2"}, timed_out_keys=set())
    coordinator._contexts = {"run": context}

    assert coordinator.order_in_progress("run", "a1") is True   # «error» после осмотра, PDF ещё строится
    assert coordinator.order_in_progress("run", "a2") is False  # обработка закончена
    assert coordinator.order_in_progress("run", "a3") is False  # ждёт подтверждения оператора
    assert coordinator.order_in_progress("other", "a1") is False


def test_resolver_looks_up_orders_that_appear_later_in_a_running_check():
    calls = []
    resolver = KitResolver(lambda ids: calls.append(list(ids)) or _sborka_orderinfo(ids))
    run = {"id": "r", "orders": {SECOND: {"order_id": SECOND, "files": _kit_file(SECOND, 2)}}}
    assert resolver.for_run(run)[SECOND]["main_id"] == MAIN
    run["orders"][THIRD] = {"order_id": THIRD, "files": _kit_file(THIRD, 3)}
    assert resolver.for_run(run)[THIRD]["main_id"] == MAIN
    assert resolver.for_run(run)[SECOND]["main_id"] == MAIN
    assert calls == [[SECOND], [MAIN], [THIRD], [MAIN]]
