"""PDF заказа — 1 или 2 страницы; нарушение — жёсткая ошибка (заказ 26234973, 15 страниц).

Обычные замечания к макету при собранном «как есть» диагностическом PDF дают
предупреждение, а нарушение числа страниц оставляет заказ красным при любом PitStop.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck, ParsedFilename, has_page_rule_violation
from services.batch_adapter import OrderArtifacts
from services.coordinator import _completed_order_status
from services.issue_groups import attach_issue_groups

PT = 72 / 25.4
PITSTOP_ERROR = {"execution_status": "completed", "verdict": "error"}


def _pdf(path: Path, pages: int, size=(94.0, 54.0)) -> None:
    document = pymupdf.open()
    for _ in range(pages):
        document.new_page(width=size[0] * PT, height=size[1] * PT)
    document.save(str(path))
    document.close()


def _inspect(root: Path, name: str, pages: int) -> OrderCheck:
    _pdf(root / name, pages)
    return BatchProcessor(root, root / "PDF").inspect_orders()[0]


def test_pdf_with_more_than_two_pages_is_a_hard_error(tmp_path):
    order = _inspect(tmp_path, "22_NP(K)_(90x50)_4-0_L15_T1_(35236-26234973)_1-face.pdf", 15)

    item = order.files[0]
    assert item.page_rule_violation
    assert any(error.startswith("PDF содержит 15 страниц; допускается не более 2") for error in item.errors)
    assert has_page_rule_violation(order)
    # Диагностический PDF + ошибка PitStop: раньше итог был «предупреждение» и заказ ушёл в печать.
    status = _completed_order_status(order, OrderArtifacts(pdf_kind="diagnostic", pitstop=PITSTOP_ERROR))
    assert status == "error"


def test_one_and_two_page_pdfs_are_allowed(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    single = _inspect(one, "job_(90x50)_4-0_(1-100).pdf", 1)
    duplex = _inspect(two, "job_(90x50)_4-4_(1-200).pdf", 2)

    assert not has_page_rule_violation(single) and single.passed
    assert not has_page_rule_violation(duplex) and duplex.passed


def test_two_page_pdf_for_one_sided_order_is_a_hard_error(tmp_path):
    order = _inspect(tmp_path, "job_(90x50)_4-0_(1-100).pdf", 2)

    assert "двухстраничный PDF недопустим для односторонней печати" in order.errors
    assert has_page_rule_violation(order)


def test_soft_findings_with_diagnostic_pdf_still_give_a_warning():
    """Низкое DPI и т. п. — по-прежнему решение оператора (жёлтый)."""
    order = OrderCheck("1", "2", files=[FileCheck(
        path=Path("/x/job_(90x50)_4-0_(2-1).pdf"),
        parsed=ParsedFilename("2", "1", 90, 50, 4, 0, ""),
        errors=["PDF страница 1: effective DPI 212.8; требуется не менее 250"],
        page_count=1,
    )])
    assert not order.passed and not has_page_rule_violation(order)
    status = _completed_order_status(order, OrderArtifacts(pdf_kind="diagnostic", pitstop=PITSTOP_ERROR))
    assert status == "warning"


def test_issue_group_names_the_page_limit_for_new_and_stored_messages():
    for message in (
        "PDF содержит 15 страниц; допускается не более 2 (лицо и оборот)",
        "PDF contains 15 pages; maximum supported is 2",
    ):
        dto = {"errors": [], "warnings": [], "files": [{"name": "a.pdf", "errors": [message], "warnings": []}]}
        attach_issue_groups(dto)
        assert any(group.get("code") == "pdf_too_many_pages" for group in dto["issue_groups"]), dto


def test_two_page_pdf_next_to_extra_side_is_a_warning_built_from_the_pdf_alone(tmp_path):
    """Заказ 26237111: «…_1.pdf» на 2 страницы и старый «…_1-back.tif» — 3 страницы в PDF.

    Это несоответствие техтребованиям, а не жёсткая ошибка: диагностический PDF — только
    двухстраничный PDF, лишний файл перечислен как не вошедший, итог — предупреждение.
    """
    from tests.fake_callas import FakeCallasMerger

    stem = "21_NP(K)_(90x50)_4-4_L34_(214-26237111)_1"
    _pdf(tmp_path / f"{stem}.pdf", 2)
    _pdf(tmp_path / f"{stem}-back.pdf", 1)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF", pdf_merger=FakeCallasMerger())
    order = processor.inspect_orders()[0]

    assert not order.passed
    assert not has_page_rule_violation(order)
    message = next(error for error in order.errors if "Несоответствие техтребованиям" in error)
    assert f"«{stem}-back.pdf»" in message and f"«{stem}.pdf»" in message

    path, note = processor.create_diagnostic_pdf(order, tmp_path / "PDF" / "Failed")

    assert path is not None and path.name == f"{stem}.pdf"
    with pymupdf.open(path) as document:
        assert document.page_count == 2
    assert note and f"{stem}-back.pdf: лишний файл стороны при двухстраничном PDF" in note
    status = _completed_order_status(order, OrderArtifacts(pdf_kind="diagnostic", pitstop=PITSTOP_ERROR))
    assert status == "warning"

    dto = {"errors": list(order.errors), "warnings": [], "files": []}
    attach_issue_groups(dto)
    assert any(group.get("code") == "two_page_pdf_extra_sides" for group in dto["issue_groups"])


def test_several_two_page_pdfs_stay_a_hard_error(tmp_path):
    _pdf(tmp_path / "job_(90x50)_4-4_(1-300)_a.pdf", 2)
    _pdf(tmp_path / "job_(90x50)_4-4_(1-300)_b.pdf", 2)
    order = BatchProcessor(tmp_path, tmp_path / "PDF").inspect_orders()[0]

    assert has_page_rule_violation(order)
    status = _completed_order_status(order, OrderArtifacts(pdf_kind="diagnostic", pitstop=PITSTOP_ERROR))
    assert status == "error"
