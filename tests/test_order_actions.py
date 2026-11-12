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
