"""Схема постпечатки от заказчика (https://sborka.ua/preview/<filename>-preview.png).

Только фейки: ни Sborka, ни сети, ни рабочей базы.
"""
from __future__ import annotations

import os
from email.utils import formatdate
from pathlib import Path
import tempfile
import threading
import time
import unittest

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

os.environ.setdefault("IMAGE_MAGIC_PASSWORD_HASH", PasswordHasher().hash("test-only-password"))

import control_panel  # noqa: E402  (pytest only: conftest isolates the database)
from processing.models import OrderCheck  # noqa: E402
from server.customer_preview_routes import stored_customer_preview  # noqa: E402
from server.settings import Settings  # noqa: E402
from services.batch_adapter import BatchProcessorAdapter, OrderArtifacts, ProcessingOptions  # noqa: E402
from services.customer_preview import (  # noqa: E402
    MAX_PREVIEW_BYTES,
    CustomerPreviewFetcher,
    HttpResponse,
    derive_filename,
    has_postpress,
    preview_url,
    preview_urls,
    public_customer_preview,
    template_filename,
    valid_filename,
)
from services.dto import order_check_to_dto  # noqa: E402
from services.order_journal import build_order_journal  # noqa: E402
from services.postpress import normalize_postpress  # noqa: E402
from tests.test_control_panel import OneOrderAdapter  # noqa: E402
from tests.test_customer_comment import (  # noqa: E402
    LateSborkaAdapter,
    make_coordinator,  # noqa: F401 — pytest fixture
    make_order,
    submit,
    wait_until,
)

NAME = "20_NP(P)_Bezlam_350_mel_(144x101)_4-4_L4_T32_(1733-26250113)_drill1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
LAST_MODIFIED = formatdate(1_700_000_000, usegmt=True)
EVENT_TYPE = "order.customer_preview"


class FakeHttp:
    """HEAD/GET fake keyed by URL; records every call."""

    def __init__(self, responses=None, *, default_status=404):
        self.responses = dict(responses or {})
        self.default_status = default_status
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def add(self, url, body=PNG, content_type="image/png", last_modified=LAST_MODIFIED, status=200,
            length=None):
        headers = {"Content-Type": content_type}
        if last_modified:
            headers["Last-Modified"] = last_modified
        headers["Content-Length"] = str(len(body) if length is None else length)
        self.responses[url] = (status, headers, body)

    def __call__(self, method, url, timeout, max_bytes):
        with self.lock:
            self.calls.append((method, url))
        if url not in self.responses:
            return HttpResponse(self.default_status, {}, None)
        status, headers, body = self.responses[url]
        return HttpResponse(status, headers, body[: max_bytes + 1] if method == "GET" else None)


def fetcher_for(http, **kwargs) -> CustomerPreviewFetcher:
    return CustomerPreviewFetcher(http=http, timeout_seconds=1, **kwargs)


# ------------------------------------------------------------------ eligibility


@pytest.mark.parametrize(
    ("post_text", "expected"),
    [
        ("Сверление 1 шт, диаметр 5 мм", True),
        ("Ламинация глянцевая 1+1", True),
        ("Сгиб: 1 пополам", True),
        ("Комплект 3 шт", False),
        ("", False),
        (None, False),
    ],
)
def test_eligibility_needs_a_fold_or_an_operation(post_text, expected):
    assert has_postpress(normalize_postpress(post_text)) is expected


def test_eligibility_of_odd_values():
    assert has_postpress(None) is False
    assert has_postpress({"raw": "x", "operations": []}) is False
    assert has_postpress({"raw": "x", "fold": {"type": "unknown"}}) is True


# ---------------------------------------------------------- filename handling


@pytest.mark.parametrize(
    "value",
    [NAME, "Візитка_(90x50)_4-0_(1733-26250113)", "a.b_c"],
)
def test_valid_filenames_pass(value):
    assert valid_filename(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, 123, "../etc/passwd", "a/b", "a\\b", "a..b", ".hidden", "a\nb", "a\x00b",
     "x" * 201, "a<b>", "a?b", "a%2fb"],
)
def test_invalid_filenames_are_refused(value):
    assert valid_filename(value) is None


def test_derive_filename_from_sources():
    assert derive_filename([Path(f"/in/{NAME}-back.tif"), Path(f"/in/{NAME}-face.tif")]) == NAME
    assert derive_filename([Path(f"/in/{NAME}-FACE.pdf")]) == NAME
    assert derive_filename([Path(f"/in/{NAME}.pdf")]) == NAME
    assert derive_filename([]) is None
    assert derive_filename([Path("/in/..bad-face.pdf")]) is None


def test_preview_url_quotes_the_name():
    assert preview_url(NAME) == (
        "https://sborka.ua/preview/20_NP%28P%29_Bezlam_350_mel_%28144x101%29_4-4_L4_T32_"
        "%281733-26250113%29_drill1-preview.png"
    )
    assert preview_url("Візитка a") == (
        "https://sborka.ua/preview/%D0%92%D1%96%D0%B7%D0%B8%D1%82%D0%BA%D0%B0%20a-preview.png"
    )


# ----------------------------------------------------------------- fetching


def test_fetch_success_saves_atomically_with_last_modified(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME))
    result = fetcher_for(http).fetch("26250113", NAME, tmp_path)

    target = tmp_path / "Previews" / "Customer" / "26250113" / f"{NAME}-preview.png"
    assert result == {
        "file": str(target),
        "source_url": preview_url(NAME),
        "last_modified": LAST_MODIFIED,
        "size": len(PNG),
    }
    assert target.read_bytes() == PNG
    assert int(target.stat().st_mtime) == 1_700_000_000
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # no temp leftovers
    assert [method for method, _ in http.calls] == ["HEAD", "GET"]


def test_fetch_jpeg_gets_jpg_extension(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME), body=JPEG, content_type="image/jpeg; charset=binary")
    result = fetcher_for(http).fetch("1", NAME, tmp_path)
    assert result is not None and result["file"].endswith("-preview.jpg")


def test_unchanged_last_modified_is_not_downloaded_again(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME))
    fetcher = fetcher_for(http)
    first = fetcher.fetch("1", NAME, tmp_path)
    http.calls.clear()

    again = fetcher.fetch("1", NAME, tmp_path)

    assert again == first
    assert [method for method, _ in http.calls] == ["HEAD"]


def test_changed_last_modified_is_downloaded_again(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME))
    fetcher = fetcher_for(http)
    fetcher.fetch("1", NAME, tmp_path)
    newer = formatdate(1_700_100_000, usegmt=True)
    http.add(preview_url(NAME), body=PNG + b"new", last_modified=newer)
    http.calls.clear()

    result = fetcher.fetch("1", NAME, tmp_path)

    assert [method for method, _ in http.calls] == ["HEAD", "GET"]
    assert result["last_modified"] == newer
    assert Path(result["file"]).read_bytes() == PNG + b"new"


def test_404_means_no_preview_and_no_file(tmp_path):
    http = FakeHttp()
    assert fetcher_for(http).fetch("1", NAME, tmp_path) is None
    assert http.calls == [("HEAD", url) for url in preview_urls(NAME)]
    assert not (tmp_path / "Previews").exists()


def test_candidate_urls_try_jpg_and_the_unfilled_name_template():
    templated = "{dateD}_{delivery}(P)_Bezlam_350_mel_(144x101)_4-4_L4_T32_(1733-26250113)_drill1"
    assert template_filename(NAME) == templated
    assert template_filename("29_KS(P)_x") == "{dateD}_{delivery}(P)_x"
    assert template_filename("Візитка_(90x50)") is None
    assert preview_urls(NAME) == [
        preview_url(NAME), preview_url(NAME, extension=".jpg"),
        preview_url(templated), preview_url(templated, extension=".jpg"),
    ]
    assert preview_urls("from-record") == [
        preview_url("from-record"), preview_url("from-record", extension=".jpg"),
    ]


def test_scheme_saved_under_unfilled_template_as_jpg_is_found(tmp_path):
    # order 26286105: Sborka kept the scheme as «{dateD}_{delivery}(P)_…-preview.jpg»
    name = "30_NP(P)_Mlam10_250_whiteBack_250_(217x60)_4-0_L39_T230_(3655-26286105)_creas4"
    url = (
        "https://sborka.ua/preview/%7BdateD%7D_%7Bdelivery%7D%28P%29_Mlam10_250_whiteBack_250_"
        "%28217x60%29_4-0_L39_T230_%283655-26286105%29_creas4-preview.jpg"
    )
    http = FakeHttp()
    http.add(url, body=JPEG, content_type="image/jpeg")
    result = fetcher_for(http).fetch("26286105", name, tmp_path)

    target = tmp_path / "Previews" / "Customer" / "26286105" / f"{name}-preview.jpg"
    assert result is not None and result["file"] == str(target)
    assert result["source_url"] == url
    assert target.read_bytes() == JPEG
    assert [method for method, _ in http.calls] == ["HEAD"] * 4 + ["GET"]


def test_real_name_png_wins_without_trying_the_others(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME))
    assert fetcher_for(http).fetch("1", NAME, tmp_path) is not None
    assert http.calls == [("HEAD", preview_url(NAME)), ("GET", preview_url(NAME))]


@pytest.mark.parametrize("content_type", ["text/html", "image/gif", ""])
def test_wrong_content_type_is_refused(tmp_path, content_type):
    http = FakeHttp()
    http.add(preview_url(NAME), body=b"<html>", content_type=content_type)
    assert fetcher_for(http).fetch("1", NAME, tmp_path) is None
    assert not (tmp_path / "Previews").exists()


def test_body_that_is_not_really_png_is_refused(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME), body=b"<html>not an image</html>")
    assert fetcher_for(http).fetch("1", NAME, tmp_path) is None


def test_oversize_by_header_and_by_body_is_refused(tmp_path):
    http = FakeHttp()
    http.add(preview_url(NAME), length=MAX_PREVIEW_BYTES + 1)
    assert fetcher_for(http).fetch("1", NAME, tmp_path) is None
    assert [method for method, _ in http.calls] == ["HEAD"]

    lying = FakeHttp()
    lying.add(preview_url(NAME), body=PNG + b"\x00" * 100, length=10)
    assert fetcher_for(lying, max_bytes=50).fetch("1", NAME, tmp_path) is None
    assert not (tmp_path / "Previews").exists()


def test_network_error_and_bad_names_never_raise(tmp_path):
    def broken(*_args):
        raise OSError("network down")

    assert fetcher_for(broken).fetch("1", NAME, tmp_path) is None
    http = FakeHttp()
    assert fetcher_for(http).fetch("1", "../x", tmp_path) is None
    assert fetcher_for(http).fetch("../1", NAME, tmp_path) is None
    assert http.calls == []


def test_concurrency_is_bounded(tmp_path):
    active, peak = [0], [0]
    lock = threading.Lock()
    release = threading.Event()

    def slow(method, url, timeout, max_bytes):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        release.wait(timeout=2)
        with lock:
            active[0] -= 1
        return HttpResponse(404, {}, None)

    fetcher = fetcher_for(slow, max_concurrent=2)
    done = []
    threads = [
        fetcher.submit(str(index), f"name{index}", tmp_path, done.append) for index in range(5)
    ]
    time.sleep(0.2)
    release.set()
    for thread in threads:
        thread.join(timeout=5)
    assert peak[0] == 2
    assert done == [None] * 5


# ------------------------------------------------------------------- DTO shape


def test_dto_and_public_shape():
    stored = {"file": "/in/Previews/Customer/1/x-preview.png", "source_url": "https://s/x",
              "last_modified": LAST_MODIFIED, "size": 10}
    order = OrderCheck("1", "42", customer_preview=stored)
    assert order_check_to_dto(order)["customer_preview"] == stored
    assert "customer_preview" not in order_check_to_dto(OrderCheck("1", "42"))

    public = public_customer_preview(stored, "run 1", "42:1")
    assert public["url"] == "/api/checks/run%201/orders/42%3A1/customer-preview"
    assert public["last_modified"] == LAST_MODIFIED
    assert public["source_url"] == "https://s/x"


# --------------------------------------------------------------- adapter wiring


class _Processor:
    def __init__(self, input_dir, *_args, **_kwargs):
        self.input_dir = Path(input_dir)
        self.on_orders_grouped = None

    def iter_inspect_orders(self):
        if self.on_orders_grouped is not None:
            self.on_orders_grouped(["100", "101", "102", "103"])
        for order_id in ("100", "101", "102", "103"):
            order = make_order(order_id)
            order.files[0].path = self.input_dir / f"{NAME}_{order_id}-face.tif"
            yield order


def make_adapter(tmp_path, fetcher, http):
    return BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=_Processor,
        order_info_fetcher=fetcher,
        customer_preview_fetcher=fetcher_for(http) if http is not None else None,
    )


def test_adapter_fetches_only_orders_with_postpress(tmp_path):
    http = FakeHttp()
    http.add(preview_url("from-record"))
    records = [
        {"id": "100", "post_text": "Сверление 1 шт", "filename": "from-record",
         # sub-order without filename: the name is derived from its source file
         "sets_array": [{"id": "101", "post_text": "Ламинация матовая"}]},
        {"id": "102", "post_text": "", "filename": "no-postpress"},
        # 103: no record at all
    ]
    adapter = make_adapter(tmp_path, lambda _ids: records, http)
    notified = threading.Event()
    adapter.set_customer_preview_listener(notified.set)
    orders = adapter.scan_and_inspect()
    assert notified.wait(timeout=5)
    adapter.wait_customer_previews(5)

    requested = sorted(url for method, url in http.calls if method == "HEAD")
    assert requested == sorted([preview_url("from-record"), *preview_urls(f"{NAME}_101")])
    assert orders[0].sborka_filename == "from-record"
    preview = adapter.customer_preview_for(orders[0])
    assert preview["source_url"] == preview_url("from-record")
    assert Path(preview["file"]).is_file()
    assert adapter.customer_preview_for(orders[1]) is None  # 404
    assert adapter.customer_preview_for(orders[2]) is None
    assert adapter.customer_preview_for(orders[3]) is None
    # once per order: asking again does not start new requests
    calls = len(http.calls)
    for order in orders:
        adapter.customer_preview_for(order)
    adapter.wait_customer_previews(5)
    assert len(http.calls) == calls


def test_adapter_without_fetcher_or_sborka_does_nothing(tmp_path):
    http = FakeHttp()
    adapter = make_adapter(tmp_path, lambda _ids: [{"id": "100", "post_text": "Сверление"}], None)
    orders = adapter.scan_and_inspect()
    assert adapter.customer_preview_for(orders[0]) is None

    no_sborka = make_adapter(tmp_path, None, http)
    orders = no_sborka.scan_and_inspect()
    assert no_sborka.customer_preview_for(orders[0]) is None
    assert http.calls == []


def test_preview_fetch_does_not_delay_inspection(tmp_path):
    release = threading.Event()

    def slow(method, url, timeout, max_bytes):
        release.wait(timeout=5)
        return HttpResponse(404, {}, None)

    adapter = BatchProcessorAdapter(
        ProcessingOptions(input_path=str(tmp_path), direction="digital"),
        processor_factory=_Processor,
        order_info_fetcher=lambda _ids: [{"id": "100", "post_text": "Сверление", "filename": "x"}],
        customer_preview_fetcher=fetcher_for(slow),
    )
    started = time.monotonic()
    adapter.scan_and_inspect()
    assert time.monotonic() - started < 2
    release.set()
    adapter.wait_customer_previews(5)


# ------------------------------------------------- late arrival (coordinator)


class LatePreviewAdapter(LateSborkaAdapter):
    """The scheme 'arrives' when the test calls ``deliver_preview``."""

    def __init__(self, orders, previews):
        super().__init__(orders, {})
        self.previews = previews
        self.preview_arrived = False
        self.preview_listener = None

    def set_customer_preview_listener(self, listener):
        self.preview_listener = listener

    def customer_preview_for(self, order):
        return self.previews.get(order.order_id) if self.preview_arrived else None

    def deliver_preview(self):
        self.preview_arrived = True
        self.preview_listener()


def preview_for(order_id):
    return {"file": f"/in/Previews/Customer/{order_id}/x-preview.png",
            "source_url": preview_url("x"), "last_modified": LAST_MODIFIED, "size": 10}


def test_late_preview_updates_waiting_and_finished_cards(make_coordinator):  # noqa: F811
    adapter = LatePreviewAdapter(
        [make_order("200", waiting=True), make_order("201")],
        {"200": preview_for("200"), "201": preview_for("201")},
    )
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    run = coordinator.wait_for(run_id, {"waiting_confirmation"}, timeout=5)
    assert "customer_preview" not in run["orders"]["200"]

    adapter.deliver_preview()

    run = coordinator.get_run(run_id)
    for order_id in ("200", "201"):
        card = run["orders"][order_id]["customer_preview"]
        assert card["url"] == f"/api/checks/{run_id}/orders/42%3A{order_id}/customer-preview"
        assert card["source_url"] == preview_url("x")
        assert card["last_modified"] == LAST_MODIFIED
    assert run["orders"]["200"]["status"] == "waiting_confirmation"
    events = [e for e in coordinator.events(run_id) if e.type == EVENT_TYPE]
    assert {e.data["order_id"] for e in events} == {"200", "201"}
    waiting = next(e for e in events if e.data["order_id"] == "200")
    assert waiting.data["order"]["customer_preview"]["url"].endswith("/customer-preview")
    assert waiting.data["customer_preview"]["file"] == "x-preview.png"

    adapter.deliver_preview()  # unchanged: no new events
    assert len([e for e in coordinator.events(run_id) if e.type == EVENT_TYPE]) == 2

    # survives the decision and the rebuild on completion
    coordinator.confirm_correction(run_id, "200")
    completed = coordinator.wait_for(run_id, {"completed"}, timeout=5)
    assert completed["orders"]["200"]["customer_preview"]["url"].endswith("/customer-preview")
    done = next(
        e for e in coordinator.events(run_id)
        if e.type == "order.completed" and e.data["order_id"] == "200"
    )
    assert done.data["order"]["customer_preview"]["url"].endswith("/customer-preview")


def test_preview_known_at_registration_is_in_the_first_card(make_coordinator):  # noqa: F811
    adapter = LatePreviewAdapter([make_order("300")], {"300": preview_for("300")})
    adapter.preview_arrived = True
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    run = coordinator.wait_for(run_id, {"completed"}, timeout=5)
    assert run["orders"]["300"]["customer_preview"]["url"].endswith("/customer-preview")
    detected = next(e for e in coordinator.events(run_id) if e.type == "order.detected")
    assert "url" in detected.data["order"]["customer_preview"]


def test_run_waits_briefly_for_running_fetches(make_coordinator):  # noqa: F811
    adapter = LatePreviewAdapter([make_order("400")], {"400": preview_for("400")})
    waited = []

    def wait_customer_previews(timeout):
        waited.append(timeout)
        adapter.deliver_preview()  # the fetch finishes during the final wait

    adapter.wait_customer_previews = wait_customer_previews
    coordinator = make_coordinator(adapter)
    run_id = submit(coordinator)
    run = coordinator.wait_for(run_id, {"completed"}, timeout=5)
    assert waited and waited[0] <= 10
    assert run["orders"]["400"]["customer_preview"]["source_url"] == preview_url("x")


def test_broken_preview_lookup_never_breaks_the_check(make_coordinator):  # noqa: F811
    adapter = LatePreviewAdapter([make_order("500")], {})

    def broken(_order):
        raise RuntimeError("boom")

    adapter.customer_preview_for = broken
    coordinator = make_coordinator(adapter)
    run = coordinator.wait_for(submit(coordinator), {"completed"}, timeout=5)
    assert run["orders"]["500"]["status"] == "passed"
    assert "customer_preview" not in run["orders"]["500"]


def test_adapter_and_coordinator_end_to_end(make_coordinator, tmp_path):  # noqa: F811
    http = FakeHttp()
    http.add(preview_url("from-record"))
    adapter = make_adapter(
        tmp_path, lambda _ids: [{"id": "100", "post_text": "Сверление", "filename": "from-record"}], http,
    )
    adapter.process_order = lambda order: OrderArtifacts()
    coordinator = make_coordinator(adapter)
    run_id = coordinator.submit(ProcessingOptions(input_path=str(tmp_path), direction="digital"))["id"]
    run = coordinator.wait_for(run_id, {"completed"}, timeout=10)
    card = next(o for o in run["orders"].values() if o["order_id"] == "100")["customer_preview"]
    assert card["source_url"] == preview_url("from-record")
    assert card["url"].endswith("/customer-preview")
    for order in run["orders"].values():
        if order["order_id"] != "100":
            assert "customer_preview" not in order


# --------------------------------------------------------------------- journal


def test_journal_entry_for_found_preview():
    order = {"order_id": "1", "customer_id": "42", "aggregate_id": "42:1", "status": "passed"}
    event = {"id": 7, "type": EVENT_TYPE, "created_at": "2026-11-25T10:00:00+00:00", "data": {
        "order_id": "1",
        "customer_preview": {"file": "x-preview.png", "source_url": preview_url("x"),
                             "last_modified": LAST_MODIFIED, "size": 10},
    }}
    journal = build_order_journal(order, [event])
    entry = next(item for item in journal["entries"] if item["code"] == "customer_preview_found")
    assert entry["kind"] == "issue"
    assert entry["severity"] == "info"
    assert entry["file"] == "x-preview.png"
    assert entry["data"]["source_url"] == preview_url("x")  # the URL is not scrubbed as a path
    assert entry["data"]["size"] == 10
    assert "customer_preview_found" not in {
        item["code"] for item in build_order_journal(order, [])["entries"]
    }


# ----------------------------------------------------------------------- route


def test_stored_preview_is_confined(tmp_path):
    root = tmp_path / "in"
    inside = root / "Previews" / "Customer" / "1" / "x-preview.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    other = root / "PDF" / "a.png"
    other.parent.mkdir()
    other.write_bytes(PNG)

    assert stored_customer_preview({"customer_preview": {"file": str(inside)}}, root) == inside.resolve()
    assert stored_customer_preview({"customer_preview": {"file": str(outside)}}, root) is None
    assert stored_customer_preview({"customer_preview": {"file": str(other)}}, root) is None
    escaped = root / "Previews" / "Customer" / ".." / ".." / ".." / "secret.png"
    assert stored_customer_preview({"customer_preview": {"file": str(escaped)}}, root) is None
    assert stored_customer_preview({}, root) is None


class PreviewOrderAdapter(OneOrderAdapter):
    preview_file: str | None = "inside"

    def process_order(self, order):
        target = self.input_dir / "Previews" / "Customer" / "1001" / "sample-preview.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG)
        if self.preview_file == "inside":
            path = target
        else:
            path = self.input_dir.parent / f".{self.input_dir.name}-outside.png"
            path.write_bytes(PNG)
        order.customer_preview = {"file": str(path), "source_url": preview_url("sample"),
                                  "last_modified": LAST_MODIFIED, "size": len(PNG)}
        return OrderArtifacts()


class CustomerPreviewRouteTests(unittest.TestCase):
    adapter_class = PreviewOrderAdapter

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(dir=control_panel.PROJECT_DIR)
        self.root = Path(self.temporary_directory.name)
        settings = Settings(
            database_url=f"sqlite:///{self.root / 'state.sqlite3'}",
            password_hash=PasswordHasher().hash("correct horse"),
            login_failure_delay_seconds=0,
            log_dir=self.root / "logs",
            log_heartbeat_seconds=10,
        )
        app = control_panel.create_app(
            settings=settings,
            allowed_roots=(self.root,),
            default_input_dir=self.root,
            adapter_factory=lambda options: self.adapter_class(options),
            initialize_schema=True,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.client.post("/api/auth/login", json={"password": "correct horse"})
        response = self.client.post("/api/checks", json={
            "input_path": str(self.root), "direction": "digital",
            "create_pdfs": True, "generate_previews": False, "copy_failures": False,
        })
        self.run_id = response.json()["id"]
        self.client.app.state.coordinator.wait_for(self.run_id, timeout=3)

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)
        outside = self.root.parent / f".{self.root.name}-outside.png"
        outside.unlink(missing_ok=True)
        self.temporary_directory.cleanup()

    def order(self) -> dict:
        items = self.client.get(f"/api/checks/{self.run_id}/orders").json()["items"]
        return next(item for item in items if item["order_id"] == "1001")

    def test_card_has_url_and_route_serves_the_image(self):
        card = self.order()["customer_preview"]
        self.assertEqual(card["url"], f"/api/checks/{self.run_id}/orders/42%3A1001/customer-preview")
        self.assertEqual(card["source_url"], preview_url("sample"))
        self.assertEqual(card["last_modified"], LAST_MODIFIED)
        response = self.client.get(card["url"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.content, PNG)
        # the bare order number works too
        bare = self.client.get(f"/api/checks/{self.run_id}/orders/1001/customer-preview")
        self.assertEqual(bare.status_code, 200)

    def test_requires_login(self):
        self.client.post("/api/auth/logout")
        response = self.client.get(f"/api/checks/{self.run_id}/orders/1001/customer-preview")
        self.assertIn(response.status_code, {401, 403})


class CustomerPreviewOutsideRouteTests(CustomerPreviewRouteTests):
    class adapter_class(PreviewOrderAdapter):
        preview_file = "outside"

    def test_card_has_url_and_route_serves_the_image(self):
        response = self.client.get(f"/api/checks/{self.run_id}/orders/1001/customer-preview")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "customer_preview_not_found")


class CustomerPreviewAbsentRouteTests(CustomerPreviewRouteTests):
    class adapter_class(OneOrderAdapter):
        def process_order(self, _order):
            return OrderArtifacts()

    def test_card_has_url_and_route_serves_the_image(self):
        self.assertNotIn("customer_preview", self.order())
        response = self.client.get(f"/api/checks/{self.run_id}/orders/1001/customer-preview")
        self.assertEqual(response.status_code, 404)
