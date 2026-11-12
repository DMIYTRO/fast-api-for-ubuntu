from services.order_actions import order_actions
from services.repository import RunEvent


def event(id, kind="order.completed", **data):
    return RunEvent(id, kind, "run", data, "2026-11-12T10:00:00Z")


def test_feed_preserves_revision_and_recorded_transforms_without_paths():
    order = {"order_id": "1", "aggregate_id": "42:1"}
    events = [event(1, aggregate_id="42:1", order={
        "current_pdf_revision": 2, "current_pdf_sha256": "abc", "pdf_path": "/secret/path",
        "files": [{"name": "face.tif", "tiff_flattened": True}],
    }), event(2, "order.callas_fix.accepted", aggregate_id="42:1", decision="accept")]
    actions = order_actions(events, order, legacy_unambiguous=True)
    assert [a["id"] for a in actions] == ["run:1", "run:2"]
    assert actions[0]["pdf_revision"] == 2
    assert actions[0]["details"]["tiff_flattened"][0]["file"] == "face.tif"
    assert "/secret" not in str(actions)
    assert actions[1]["details"]["decision"] == "accept"


def test_feed_isolates_same_number_for_different_customers_and_legacy():
    events = [event(1, aggregate_id="43:1", order_id="1"), event(2, order_id="1"), event(3, aggregate_id="42:1", order_id="1")]
    order = {"order_id": "1", "aggregate_id": "42:1"}
    assert len(order_actions(events, order, legacy_unambiguous=False)) == 1
    assert len(order_actions(events, order, legacy_unambiguous=True)) == 2


def test_planned_or_confirmed_transform_is_not_reported_as_executed():
    order = {"order_id": "1", "aggregate_id": "42:1"}
    snapshot = {"files": [{"name": "planned.tif", "needs_resample": True, "resample_confirmed": True, "rotation_degrees": 90}, {"name": "done.tif", "processing_actions": [{"kind": "size_correction", "target_mm": [94, 54]}]}]}
    actions = order_actions([event(1, aggregate_id="42:1", order=snapshot)], order, legacy_unambiguous=True)
    assert actions[0]["details"]["operations"] == [{"file": "done.tif", "kind": "size_correction", "target_mm": [94, 54]}]


def test_automatic_transform_is_recorded_only_after_page_conversion(tmp_path):
    from unittest.mock import patch
    import pytest
    from processing.models import FileCheck, ParsedFilename
    from processing.batch_processor import BatchProcessor
    from services.dto import file_check_to_dto

    item = FileCheck(path=tmp_path / "face.tif", parsed=ParsedFilename("42", "1", 90, 50, 4, 0, "face"), actual_width_mm=93, actual_height_mm=53, dpi_x=300, dpi_y=300, needs_resample=True, resample_target_mm=(94, 54), resample_decision="auto_correct", rotation_degrees=90)
    processor = BatchProcessor(tmp_path, tmp_path / "PDF")
    refs = processor._build_page_refs([item])
    with patch("processing.batch_processor.resample_image"), patch("processing.batch_processor.convert_image_to_pdf", side_effect=OSError("failed")):
        with pytest.raises(OSError):
            processor._prepare_page_pdfs(refs, tmp_path / "work")
    assert item.processing_actions == []
    with patch("processing.batch_processor.resample_image"), patch("processing.batch_processor.convert_image_to_pdf"):
        processor._prepare_page_pdfs(refs, tmp_path / "work")
    assert file_check_to_dto(item)["processing_actions"] == [{"kind": "size_correction", "target_mm": [94, 54], "target_dpi": 300, "rotation_degrees": 90}]
