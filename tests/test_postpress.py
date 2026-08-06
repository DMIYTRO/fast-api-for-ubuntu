from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from processing.models import OrderCheck
from services.batch_adapter import BatchProcessorAdapter, ProcessingOptions
from services.dto import order_check_to_dto
from services.postpress import normalize_postpress
from services.sborka_integration import build_order_info_fetcher


def test_normalizes_supported_fold_aliases_and_unknowns():
    assert normalize_postpress("Сгиб: 1 пополам")["fold"] == {
        "type": "half-fold",
        "count": 1,
        "operation": "Сгиб",
        "needs_confirmation": False,
    }
    assert normalize_postpress("Сгиб: 2 намотка")["fold"]["type"] == "c-fold"
    assert normalize_postpress("Сгиб: 1 poplam")["fold"]["type"] == "half-fold"
    assert normalize_postpress("Сгиб: 2 namotka")["fold"]["type"] == "c-fold"
    assert normalize_postpress("Сгиб: 2 garmoshka")["fold"]["type"] == "z-fold"
    assert normalize_postpress("2 fold z-fold")["fold"]["type"] == "z-fold"
    assert normalize_postpress("Сгиб: 2 окно")["fold"] == {
        "type": "unknown",
        "count": 2,
        "operation": "Сгиб",
        "needs_confirmation": True,
    }
    assert normalize_postpress("Биг: 2 гармошка")["fold"] == {
        "type": "z-fold",
        "count": 2,
        "operation": "Биг",
        "needs_confirmation": False,
    }
    assert normalize_postpress("Сгиб: 1")["fold"] == {
        "type": "half-fold",
        "count": 1,
        "operation": "Сгиб",
        "label": "Сгиб: 1",
        "needs_confirmation": False,
    }


def test_order_dto_includes_postpress_metadata():
    order = OrderCheck("100", "42", postpress=normalize_postpress("Сгиб: 1 half"))
    assert order_check_to_dto(order)["postpress"]["fold"]["type"] == "half-fold"


class _InspectingProcessor:
    def __init__(self, *_args, **_kwargs):
        pass

    def iter_inspect_orders(self):
        yield OrderCheck("100", "42")
        yield OrderCheck("101", "42")


def test_adapter_fetches_orderinfo_in_one_batch_and_enriches_orders(tmp_path: Path):
    requested = []

    def fetcher(order_ids):
        requested.append(list(order_ids))
        return [
            {
                "id": "100",
                "post_text": "Сгиб: 2 намотка",
                "sets_array": [
                    {"id": "101", "post_text": "Сгиб: 1 пополам"},
                ],
            },
        ]

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=_InspectingProcessor,
        order_info_fetcher=fetcher,
    )

    orders = list(adapter.iter_inspect_orders())

    assert requested == [["100", "101"]]
    assert orders[0].postpress["fold"]["type"] == "c-fold"
    assert orders[1].postpress["fold"]["type"] == "half-fold"


def test_orderinfo_fetcher_calls_sborka_api_with_scanned_order_ids(tmp_path: Path):
    (tmp_path / "sborka_api_key.txt").write_text("test-key", encoding="utf-8")
    (tmp_path / "sborka_orderinfo.py").write_text("# loaded through a mock\n", encoding="utf-8")
    fetch_order_info = Mock(
        return_value=(200, '[{"id": "100", "post_text": "Биг: 2 гармошка"}]')
    )

    with patch(
        "services.sborka_integration._load_module",
        return_value=SimpleNamespace(fetch_order_info=fetch_order_info),
    ):
        fetcher = build_order_info_fetcher(tmp_path, timeout=13)
        assert fetcher is not None
        response = fetcher(["100", "101"])

    fetch_order_info.assert_called_once_with(["100", "101"], timeout=13)
    assert response == [{"id": "100", "post_text": "Биг: 2 гармошка"}]
