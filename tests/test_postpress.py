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
        self.on_orders_grouped = None

    def iter_inspect_orders(self):
        if self.on_orders_grouped is not None:
            self.on_orders_grouped(["100", "101"])
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

    orders = adapter.scan_and_inspect()

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


def test_postpress_operations_parse_sborka_text():
    from services.postpress import postpress_operations

    ops = postpress_operations(
        "Комплект : 50;Рулонна ламінація Глянцева ламінація з двох сторін 25 мікрон; "
        "скругление углов : 4; Сверление : 1, &empty; - 5 мм; доработка макета: 100.00; "
        "биговка : 1; Сгиб : 1; кашировка : 2; Вертикально;Скоба : 2; Склейка : 45 x 100шт., сверху"
    )
    assert [op["kind"] for op in ops] == [
        "lamination", "rounding", "drilling", "mounting", "staple", "gluing",
    ]
    assert ops[0]["detail"] == "глянец, 2 стороны, 25 мкм"
    assert ops[1]["count"] == 4
    assert ops[2]["count"] == 1 and ops[2]["diameter_mm"] == 5.0
    assert ops[3]["count"] == 2
    assert ops[4] == {"kind": "staple", "label": "Скоба", "text": "Скоба : 2", "count": 2, "detail": "вертикально"}
    assert "count" not in ops[5] and ops[5]["detail"] == "45 x 100шт., сверху"


def test_postpress_operations_merge_pouch_lamination_and_grommets():
    from services.postpress import postpress_operations

    ops = postpress_operations("Матова ламінація з двох боків 75 мікрон A3; Ламінація в конверт")
    assert len(ops) == 1 and ops[0]["detail"] == "мат, 2 стороны, 75 мкм, в конверт"
    grommets = postpress_operations("Люверсовка: По периметру , Диаметр люверсов : 10 мм, Шаг люверсов : 300 мм")
    assert grommets[0]["kind"] == "grommet" and grommets[0]["diameter_mm"] == 10.0
    assert postpress_operations("Комплект : 16; Сгиб : 2 намотка") == []


def test_dto_backfills_operations_for_stored_postpress():
    order = OrderCheck("100", "42", postpress={"raw": "скругление углов : 4"})
    dto = order_check_to_dto(order)
    assert dto["postpress"]["operations"][0] == {
        "kind": "rounding", "label": "Скругление углов", "text": "скругление углов : 4", "count": 4,
    }
    assert normalize_postpress("скругление углов : 4")["operations"] == dto["postpress"]["operations"]
