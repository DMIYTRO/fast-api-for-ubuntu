"""Заказы, отменённые в Sborka (orderinfo status 14), отбрасываются до проверки (26255491)."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

from processing.batch_processor import BatchProcessor, CANCELLED_ORDER_MESSAGE
from services import batch_adapter
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.coordinator import RunCoordinator
from services.domain import validate_operator_transition
from services.dto import order_check_to_dto
from services.print_readiness import print_blockers
from services.repository import InMemoryRunRepository
from services.sborka_cancel import cancelled_orders

CANCELLED = "24_KS(P)_Mlam11_300_mel_(148x210)_4-4_L125_T500_(36864-26255491)_creas2_falc2"
ACTIVE = "24_KS(P)_Mlam11_300_mel_(148x210)_4-0_L125_T500_(36864-26255492)"


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(batch_adapter, "ORDER_INFO_RETRY_PAUSE_SECONDS", 0.0)


def test_only_status_14_counts_as_cancelled():
    records = {
        "1": {"id": "1", "status": "14"},
        "2": {"id": "2", "status": "12", "comment_client": "отменен клиентом"},  # восстановлен
        "3": {"id": "3", "status": 14},
    }
    assert cancelled_orders(records, ["1", "2", "3", "4"]) == {
        "1": {"sborka_status": "14"},
        "3": {"sborka_status": "14"},
    }


class _Processor:
    """Осматривает только то, что не отброшено хуком отмены."""

    def __init__(self, *_args, **_kwargs):
        self.on_orders_grouped = None
        self.cancelled_orders = None
        self.inspected: list[str] = []


def _adapter(tmp_path: Path, fetcher, factory=BatchProcessor) -> BatchProcessorAdapter:
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=factory,
        order_info_fetcher=fetcher,
    )


def _write(tmp_path: Path, stem: str) -> Path:
    path = tmp_path / f"{stem}.pdf"
    path.write_bytes(b"%PDF-1.4 not a real pdf")
    return path


def test_cancelled_order_is_yielded_uninspected_and_its_files_moved(tmp_path, monkeypatch):
    source = _write(tmp_path, CANCELLED)
    adapter = _adapter(
        tmp_path, lambda ids: [{"id": "26255491", "status": "14", "post_text": ""}]
    )
    inspected = []
    monkeypatch.setattr(
        BatchProcessor, "_inspect_pdf_file", lambda self, check: inspected.append(check.path.name)
    )

    orders = list(adapter.iter_inspect_orders())

    assert [o.order_id for o in orders] == ["26255491"]
    order = orders[0]
    assert inspected == []
    assert order.sborka_cancelled == {"sborka_status": "14"}
    assert order.errors == [CANCELLED_ORDER_MESSAGE]

    result = adapter.process_order(order)

    assert result.pdf_path is None and result.preview_paths == [] and result.errors == []
    target = tmp_path / "Cancelled" / "26255491"
    assert (target / source.name).is_file() and not source.exists()
    assert order.sborka_cancelled["moved_to"] == str(target)
    assert order.sborka_cancelled["moved_files"] == [source.name]
    assert order_check_to_dto(order)["sborka_cancelled"]["moved_to"] == str(target)


def test_active_orders_are_inspected_as_before(tmp_path, monkeypatch):
    _write(tmp_path, CANCELLED)
    _write(tmp_path, ACTIVE)
    adapter = _adapter(
        tmp_path,
        lambda ids: [{"id": "26255491", "status": "14"}, {"id": "26255492", "status": "12"}],
    )
    inspected = []
    monkeypatch.setattr(
        BatchProcessor, "_inspect_pdf_file", lambda self, check: inspected.append(check.path.name)
    )

    orders = list(adapter.iter_inspect_orders())

    assert [o.order_id for o in orders] == ["26255491", "26255492"]
    assert orders[1].sborka_cancelled is None
    assert inspected == [f"{ACTIVE}.pdf"]


def test_failed_orderinfo_is_retried_once(tmp_path, monkeypatch):
    _write(tmp_path, CANCELLED)
    calls = []

    def fetcher(ids):
        calls.append(list(ids))
        if len(calls) == 1:
            raise RuntimeError("timeout")
        return [{"id": "26255491", "status": "14"}]

    monkeypatch.setattr(BatchProcessor, "_inspect_pdf_file", lambda self, check: None)
    orders = list(_adapter(tmp_path, fetcher).iter_inspect_orders())

    assert len(calls) == 2
    assert orders[0].sborka_cancelled == {"sborka_status": "14"}


def test_without_sborka_answer_orders_are_checked_normally(tmp_path, monkeypatch):
    _write(tmp_path, CANCELLED)

    def fetcher(_ids):
        raise RuntimeError("sborka down")

    inspected = []
    monkeypatch.setattr(
        BatchProcessor, "_inspect_pdf_file", lambda self, check: inspected.append(check.path.name)
    )
    orders = list(_adapter(tmp_path, fetcher).iter_inspect_orders())

    assert orders[0].sborka_cancelled is None
    assert inspected == [f"{CANCELLED}.pdf"]


def test_move_never_overwrites_an_earlier_cancelled_copy(tmp_path, monkeypatch):
    source = _write(tmp_path, CANCELLED)
    target = tmp_path / "Cancelled" / "26255491"
    target.mkdir(parents=True)
    (target / source.name).write_bytes(b"older")
    monkeypatch.setattr(BatchProcessor, "_inspect_pdf_file", lambda self, check: None)
    adapter = _adapter(tmp_path, lambda ids: [{"id": "26255491", "status": "14"}])

    order = list(adapter.iter_inspect_orders())[0]
    adapter.process_order(order)

    assert (target / source.name).read_bytes() == b"older"
    assert (target / f"{CANCELLED} (2).pdf").is_file()


def test_cancelled_folder_is_not_scanned(tmp_path):
    target = tmp_path / "Cancelled" / "26255491"
    target.mkdir(parents=True)
    _write(target, CANCELLED)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF")
    assert processor.scan() == []


def test_coordinator_marks_the_order_cancelled(tmp_path, monkeypatch):
    _write(tmp_path, CANCELLED)
    monkeypatch.setattr(BatchProcessor, "_inspect_pdf_file", lambda self, check: None)
    adapter = _adapter(tmp_path, lambda ids: [{"id": "26255491", "status": "14"}])
    coordinator = RunCoordinator(InMemoryRunRepository(), adapter_factory=lambda _o: adapter)
    try:
        run_id = coordinator.submit(
            ProcessingOptions(input_path=str(tmp_path), direction="digital")
        )["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = coordinator.get_run(run_id)
            if run["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        order = next(iter(run["orders"].values()))
        assert run["status"] == "completed"
        assert order["status"] == "cancelled"
        assert order["sborka_cancelled"]["moved_files"] == [f"{CANCELLED}.pdf"]
        assert not order.get("pdf_path")
    finally:
        coordinator.shutdown()


def test_cancelled_order_cannot_be_printed_or_returned():
    order = {"status": "cancelled", "sborka_cancelled": {"sborka_status": "14"}}
    assert [b["code"] for b in print_blockers(order)] == ["sborka_cancelled"]
    for target in ("accepted_for_print", "returned_for_rework"):
        with pytest.raises(ValueError, match="заказ отменён"):
            validate_operator_transition("cancelled", target, confirm_failed_processing=True)
