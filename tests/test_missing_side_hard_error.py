"""Нет лица или оборота в двустороннем заказе — жёсткая ошибка (заказ 26262655 был жёлтым)."""

from processing.batch_processor import BatchProcessor
from processing.models import has_page_rule_violation
from tests.test_callas_source_fix import make_pdf


def _order(tmp_path, *names):
    for name in names:
        make_pdf(tmp_path / name, 94, 54)
    return BatchProcessor(tmp_path, tmp_path / "PDF").inspect_orders()[0]


def test_missing_face_is_hard_error(tmp_path):
    order = _order(tmp_path, "T_(90x50)_4-4_(5-1)-back.pdf")
    assert "не найден обязательный файл face" in order.errors
    assert has_page_rule_violation(order)


def test_missing_back_is_hard_error(tmp_path):
    order = _order(tmp_path, "T_(90x50)_4-4_(5-1)-face.pdf")
    assert "для двусторонней печати не найден файл back" in order.errors
    assert has_page_rule_violation(order)


def test_complete_duplex_order_is_not_flagged(tmp_path):
    order = _order(tmp_path, "T_(90x50)_4-4_(5-1)-face.pdf", "T_(90x50)_4-4_(5-1)-back.pdf")
    assert not has_page_rule_violation(order)
