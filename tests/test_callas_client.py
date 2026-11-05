"""Тесты клиента callas на fake HTTP (httpx.MockTransport); живые службы не используются."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from server.settings import Settings
from services.callas import (
    CallasAuthError,
    CallasCancelledError,
    CallasClient,
    CallasConfig,
    CallasConfigError,
    CallasConflictError,
    CallasJobFailedError,
    CallasProtocolError,
    CallasResultError,
    CallasTimeoutError,
    CallasUnavailableError,
    CallasValidationError,
    deterministic_job_id,
)

TOKEN = "super-secret-token-value-123"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeCallas:
    """Минимальная имитация службы; сценарий задаётся списками ответов."""

    def __init__(self, share: Path, *, server_share: str | None = None) -> None:
        self.share = share
        self.server_share = server_share or str(share)
        self.requests: list[httpx.Request] = []
        self.specs: dict[str, dict] = {}
        self.put_script: list = []  # элементы: httpx.Response | Exception | callable
        self.get_script: list = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    # --- помощники сценария
    def out_file(self, job_id: str, name: str, data: bytes) -> dict:
        out = self.share / ".callas" / job_id / "out"
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(data)
        return self.file_entry(job_id, name, data)

    def file_entry(self, job_id: str, name: str, data: bytes) -> dict:
        return {
            "name": name,
            "path": f"{self.server_share}/.callas/{job_id}/out/{name}",
            "size": len(data),
            "sha256": sha(data),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
        job_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "PUT":
            self.specs[job_id] = json.loads(request.content)
            script = self.put_script
            default = httpx.Response(202, json={"job_id": job_id})
        elif request.method == "GET":
            script = self.get_script
            default = httpx.Response(200, json={"status": "running", "files": []})
        else:
            return httpx.Response(200, json={"deleted": True})
        with self.lock:
            item = script.pop(0) if script else default
        if callable(item):
            item = item(job_id)
        if isinstance(item, Exception):
            raise item
        return item

    def methods(self) -> list[str]:
        return [r.method for r in self.requests]


def make_client(tmp_path: Path, fake: FakeCallas, **overrides) -> CallasClient:
    params = dict(
        base_url="http://callas.test:8765",
        token=TOKEN,
        share=tmp_path,
        server_share=fake.server_share,
        request_timeout_seconds=2.0,
        job_timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        poll_max_interval_seconds=0.02,
        max_concurrent_jobs=2,
        max_retries=3,
        backoff_base_seconds=0.0,
        backoff_max_seconds=0.0,
        file_visible_timeout_seconds=0.2,
    )
    params.update(overrides)
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return CallasClient(CallasConfig(**params), http_client=http)


@pytest.fixture()
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


@pytest.fixture()
def fake(share: Path) -> FakeCallas:
    return FakeCallas(share)


@pytest.fixture()
def sources(tmp_path: Path) -> list[Path]:
    paths = []
    for index, body in enumerate((b"%PDF-1.4 first", b"%PDF-1.4 second")):
        path = tmp_path / f"вход {index}.pdf"
        path.write_bytes(body)
        paths.append(path)
    return paths


def done_merge(fake: FakeCallas, job_id: str, name: str = "out.pdf", data: bytes = b"%PDF-merged"):
    return httpx.Response(
        200, json={"status": "done", "files": [fake.out_file(job_id, name, data)], "bad_inputs": []}
    )


# ----------------------------------------------------------------- happy path


def test_merge_stages_inputs_and_returns_verified_result(share, fake, sources):
    fake.get_script = [
        httpx.Response(200, json={"status": "queued"}),
        lambda job_id: done_merge(fake, job_id),
    ]
    client = make_client(share, fake)
    result = client.merge(sources, "out.pdf", job_id="job-1")

    assert result == share / ".callas" / "job-1" / "out" / "out.pdf"
    assert result.read_bytes() == b"%PDF-merged"
    in_dir = share / ".callas" / "job-1" / "in"
    assert sorted(p.name for p in in_dir.iterdir()) == ["0001.pdf", "0002.pdf"]
    spec = fake.specs["job-1"]
    assert spec["type"] == "merge"
    assert spec["output"] == f"{share}/.callas/job-1/out/out.pdf"
    assert spec["skip_corrupt"] is False
    assert [i["path"] for i in spec["inputs"]] == [
        f"{share}/.callas/job-1/in/0001.pdf",
        f"{share}/.callas/job-1/in/0002.pdf",
    ]
    assert spec["inputs"][0]["sha256"] == sha(b"%PDF-1.4 first")
    put = fake.requests[0]
    assert put.headers["authorization"] == f"Bearer {TOKEN}"
    assert put.headers["x-callas-user"] == "image-magic"


def test_server_share_may_differ_from_local_share(share, sources):
    fake = FakeCallas(share, server_share="/srv/callas-mount")
    fake.get_script = [lambda job_id: done_merge(fake, job_id)]
    client = make_client(share, fake, server_share="/srv/callas-mount")
    client.merge(sources[:1], "out.pdf", job_id="job-2")
    assert fake.specs["job-2"]["inputs"][0]["path"] == "/srv/callas-mount/.callas/job-2/in/0001.pdf"


def test_render_sorts_pages_numerically(share, fake, sources):
    def pages(job_id):
        entries = [fake.out_file(job_id, f"page-{n:03d}.png", f"png{n}".encode()) for n in (10, 2, 1, 3, 4, 5, 6, 7, 8, 9)]
        return httpx.Response(200, json={"status": "done", "files": entries})

    fake.get_script = [pages]
    client = make_client(share, fake)
    result = client.render(sources[0], 200, job_id="render-1", expected_pages=10)
    assert [p.name for p in result] == [f"page-{n:03d}.png" for n in range(1, 11)]
    spec = fake.specs["render-1"]
    assert spec["type"] == "render" and spec["dpi"] == 200 and spec["format"] == "png"
    assert spec["out_dir"] == f"{share}/.callas/render-1/out"
    assert spec["input"]["path"].endswith("/render-1/in/0001.pdf")


def test_render_rejects_gaps_and_wrong_page_count(share, fake, sources):
    def gap(job_id):
        entries = [fake.out_file(job_id, n, b"x") for n in ("page-001.png", "page-003.png")]
        return httpx.Response(200, json={"status": "done", "files": entries})

    fake.get_script = [gap]
    with pytest.raises(CallasResultError, match="пропусками"):
        make_client(share, fake).render(sources[0], job_id="render-gap")

    def two(job_id):
        entries = [fake.out_file(job_id, n, b"x") for n in ("page-001.png", "page-002.png")]
        return httpx.Response(200, json={"status": "done", "files": entries})

    fake.get_script = [two]
    with pytest.raises(CallasResultError, match="ожидалось 3"):
        make_client(share, fake).render(sources[0], job_id="render-cnt", expected_pages=3)


def test_repeat_with_same_job_id_reuses_staged_input(share, fake, sources):
    fake.get_script = [lambda j: done_merge(fake, j), lambda j: done_merge(fake, j)]
    client = make_client(share, fake)
    client.merge(sources, "out.pdf", job_id="job-rep")
    staged = share / ".callas" / "job-rep" / "in" / "0001.pdf"
    mtime = staged.stat().st_mtime_ns
    client.merge(sources, "out.pdf", job_id="job-rep")
    assert staged.stat().st_mtime_ns == mtime


def test_deterministic_job_id_is_stable_and_safe():
    a = deterministic_job_id("run 5/order", "sha1", "sha2")
    assert a == deterministic_job_id("run 5/order", "sha1", "sha2")
    assert a != deterministic_job_id("run 5/order", "sha1", "sha3")
    CallasClient._validate_job_id(a)


# ------------------------------------------------------------- job statuses


def test_failed_job_reports_bad_input_by_name(share, fake, sources):
    fake.get_script = [
        httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "не удалось",
                "error_class": "input_corrupt",
                "bad_inputs": [{"index": 1, "path": "/x", "reason": "не PDF"}],
            },
        )
    ]
    with pytest.raises(CallasJobFailedError) as info:
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-f")
    assert info.value.status == "failed"
    assert info.value.error_class == "input_corrupt"
    assert sources[1].name in str(info.value) and "не PDF" in str(info.value)


def test_cancelled_job(share, fake, sources):
    fake.get_script = [httpx.Response(200, json={"status": "cancelled"})]
    with pytest.raises(CallasJobFailedError) as info:
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-c")
    assert info.value.status == "cancelled"
    assert "отменил" in str(info.value)


def test_done_with_skipped_inputs_is_rejected(share, fake, sources):
    def skipped(job_id):
        return httpx.Response(
            200,
            json={
                "status": "done",
                "files": [fake.out_file(job_id, "out.pdf", b"x")],
                "bad_inputs": [{"index": 0, "reason": "битый"}],
            },
        )

    fake.get_script = [skipped]
    with pytest.raises(CallasJobFailedError, match="терять нельзя"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-skip")


# ------------------------------------------------------------- HTTP errors


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_not_retried(share, fake, sources, code):
    fake.put_script = [httpx.Response(code, json={"detail": "Неверный токен"})]
    with pytest.raises(CallasAuthError, match="IMAGE_MAGIC_CALLAS_TOKEN"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-a")
    assert fake.methods().count("PUT") == 1


def test_conflict_on_first_put(share, fake, sources):
    fake.put_script = [httpx.Response(409, json={"detail": "exists"})]
    with pytest.raises(CallasConflictError):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-409")
    assert fake.methods().count("PUT") == 1


def test_validation_error_shows_field_details(share, fake, sources):
    fake.put_script = [
        httpx.Response(422, json={"detail": [{"loc": ["body", "inputs", 0, "path"], "msg": "field required", "type": "x"}]})
    ]
    with pytest.raises(CallasValidationError, match="inputs.0.path: field required"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-422")


def test_retry_after_seconds_is_honoured_on_429(share, fake, sources):
    fake.put_script = [httpx.Response(429, headers={"Retry-After": "0"})]
    fake.get_script = [lambda j: done_merge(fake, j)]
    make_client(share, fake).merge(sources, "out.pdf", job_id="job-429")
    assert fake.methods().count("PUT") == 2


def test_503_retries_then_succeeds(share, fake, sources):
    fake.put_script = [httpx.Response(503), httpx.Response(503)]
    fake.get_script = [lambda j: done_merge(fake, j)]
    make_client(share, fake).merge(sources, "out.pdf", job_id="job-503")
    assert fake.methods().count("PUT") == 3


def test_503_exhausts_retries(share, fake, sources):
    fake.put_script = [httpx.Response(503)] * 10
    with pytest.raises(CallasUnavailableError, match="HTTP 503"):
        make_client(share, fake, max_retries=2).merge(sources, "out.pdf", job_id="job-503x")
    assert fake.methods().count("PUT") == 3


@pytest.mark.parametrize("value", ["tomorrow", "-5", "", "Mon, 99 Foo 0000 99:99:99 GMT", "Mon, 01 Jan 2001 00:00:00 GMT"])
def test_invalid_or_past_retry_after_does_not_break_retry(share, fake, sources, value):
    fake.put_script = [httpx.Response(429, headers={"Retry-After": value})]
    fake.get_script = [lambda j: done_merge(fake, j)]
    make_client(share, fake).merge(sources, "out.pdf", job_id="job-ra")
    assert fake.methods().count("PUT") == 2


def test_retry_after_beyond_deadline_times_out(share, fake, sources):
    fake.put_script = [httpx.Response(503, headers={"Retry-After": "100"})]
    with pytest.raises(CallasTimeoutError, match="дольше общего срока"):
        make_client(share, fake, job_timeout_seconds=2.0).merge(sources, "out.pdf", job_id="job-rad")


def test_lost_put_response_is_adopted_on_409(share, fake, sources):
    fake.put_script = [httpx.ConnectError("обрыв"), httpx.Response(409, json={"detail": "exists"})]
    fake.get_script = [lambda j: done_merge(fake, j)]
    result = make_client(share, fake).merge(sources, "out.pdf", job_id="job-lost")
    assert result.name == "out.pdf"
    assert fake.methods().count("PUT") == 2


def test_bad_json_in_status(share, fake, sources):
    fake.get_script = [httpx.Response(200, content=b"<html>oops")]
    with pytest.raises(CallasProtocolError, match="некорректный JSON"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-json")


def test_non_object_json(share, fake, sources):
    fake.get_script = [httpx.Response(200, json=[1, 2])]
    with pytest.raises(CallasProtocolError):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-arr")


# ------------------------------------------------------ timeout and cancel


def test_job_deadline_cancels_remote_job(share, fake, sources):
    client = make_client(share, fake, job_timeout_seconds=0.3)
    with pytest.raises(CallasTimeoutError):
        client.merge(sources, "out.pdf", job_id="job-slow")
    assert fake.methods()[-1] == "DELETE"


def test_request_timeout_is_retried_within_deadline(share, fake, sources):
    fake.get_script = [httpx.ReadTimeout("slow"), lambda j: done_merge(fake, j)]
    make_client(share, fake).merge(sources, "out.pdf", job_id="job-rt")
    assert fake.methods().count("GET") == 2


def test_cancel_event_stops_polling_and_deletes_remote_job(share, fake, sources):
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    client = make_client(share, fake, job_timeout_seconds=10)
    started = time.monotonic()
    with pytest.raises(CallasCancelledError):
        client.merge(sources, "out.pdf", job_id="job-cancel", cancel=cancel)
    assert time.monotonic() - started < 3
    assert fake.methods()[-1] == "DELETE"


def test_cancel_before_start_sends_nothing(share, fake, sources):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CallasCancelledError):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-pre", cancel=cancel)
    assert fake.requests == []


def test_cancel_during_backoff_is_prompt(share, fake, sources):
    fake.put_script = [httpx.Response(503)] * 5
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    client = make_client(share, fake, backoff_base_seconds=30.0, backoff_max_seconds=30.0, max_retries=5)
    started = time.monotonic()
    with pytest.raises(CallasCancelledError):
        client.merge(sources, "out.pdf", job_id="job-bo", cancel=cancel)
    assert time.monotonic() - started < 3


def test_concurrency_limit(share, fake, sources):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow_done(job_id):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.1)
        response = done_merge(fake, job_id)
        with lock:
            active["now"] -= 1
        return response

    fake.get_script = [slow_done] * 4
    client = make_client(share, fake, max_concurrent_jobs=1)
    threads = [
        threading.Thread(target=client.merge, args=(sources, "out.pdf"), kwargs={"job_id": f"job-par-{i}"})
        for i in range(4)
    ]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert active["max"] == 1
    assert fake.methods().count("PUT") == 4


# ------------------------------------------------------- result verification


def tampered(fake, mutate):
    def build(job_id):
        entry = fake.out_file(job_id, "out.pdf", b"%PDF-real")
        mutate(entry, job_id)
        return httpx.Response(200, json={"status": "done", "files": [entry]})

    return build


def test_result_path_outside_job_directory_is_rejected(share, fake, sources):
    fake.get_script = [tampered(fake, lambda e, j: e.update(path=f"{share}/.callas/other-job/out/out.pdf"))]
    with pytest.raises(CallasResultError, match="вне папки задания"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-p1")


@pytest.mark.parametrize(
    "bad_path",
    ["{share}/.callas/job-p2/out/../in/0001.pdf", "{share}/.callas/job-p2/in/out.pdf", "{share}/other/out.pdf"],
)
def test_result_path_traversal_is_rejected(share, fake, sources, bad_path):
    fake.get_script = [tampered(fake, lambda e, j: e.update(path=bad_path.format(share=share)))]
    with pytest.raises(CallasResultError):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-p2")


def test_result_name_with_separator_is_rejected(share, fake, sources):
    fake.get_script = [tampered(fake, lambda e, j: e.update(name="../out.pdf"))]
    with pytest.raises(CallasResultError, match="недопустимое имя"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-n")


def test_sha_mismatch_is_rejected(share, fake, sources):
    fake.get_script = [tampered(fake, lambda e, j: e.update(sha256="0" * 64))]
    with pytest.raises(CallasResultError, match="Контрольная сумма"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-sha")


def test_size_mismatch_is_rejected(share, fake, sources):
    fake.get_script = [tampered(fake, lambda e, j: e.update(size=3))]
    with pytest.raises(CallasResultError, match="Размер"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-size")


def test_missing_result_file_is_reported(share, fake, sources):
    def ghost(job_id):
        entry = fake.file_entry(job_id, "out.pdf", b"x")
        return httpx.Response(200, json={"status": "done", "files": [entry]})

    fake.get_script = [ghost]
    with pytest.raises(CallasResultError, match="не появился"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-ghost")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="нет symlink")
def test_symlinked_result_is_rejected(share, fake, sources, tmp_path):
    secret = tmp_path / "secret.pdf"
    secret.write_bytes(b"data")

    def link(job_id):
        out = share / ".callas" / job_id / "out"
        out.mkdir(parents=True)
        (out / "out.pdf").symlink_to(secret)
        return httpx.Response(200, json={"status": "done", "files": [fake.file_entry(job_id, "out.pdf", b"data")]})

    fake.get_script = [link]
    with pytest.raises(CallasResultError, match="выходит за пределы"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-link")


def test_empty_files_list_is_rejected(share, fake, sources):
    fake.get_script = [httpx.Response(200, json={"status": "done", "files": []})]
    with pytest.raises(CallasResultError, match="без файлов"):
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-empty")


# ------------------------------------------------------- input validation


def test_invalid_arguments(share, fake, sources):
    client = make_client(share, fake)
    with pytest.raises(CallasConfigError):
        client.merge(sources, "out.pdf", job_id="../evil")
    with pytest.raises(CallasConfigError):
        client.merge(sources, "sub/out.pdf", job_id="ok")
    with pytest.raises(CallasConfigError):
        client.merge([], "out.pdf", job_id="ok")
    with pytest.raises(CallasConfigError):
        client.render(sources[0], 10, job_id="ok")
    with pytest.raises(CallasConfigError, match="не найден"):
        client.merge([share / "нет.pdf"], "out.pdf", job_id="ok")
    assert fake.requests == []


def test_missing_url_or_token():
    with pytest.raises(CallasConfigError, match="URL"):
        CallasClient(CallasConfig(base_url="", token="t"))
    with pytest.raises(CallasConfigError, match="TOKEN"):
        CallasClient(CallasConfig(base_url="http://x", token=""))


def test_unavailable_share_gives_clear_error(tmp_path, fake, sources):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    client = make_client(blocker, fake)
    with pytest.raises(CallasConfigError, match="Общая папка callas недоступна"):
        client.merge(sources, "out.pdf", job_id="job-share")


# ------------------------------------------------------------------ secrets


def test_secret_is_never_logged_or_exposed(share, fake, sources, caplog):
    caplog.set_level(logging.DEBUG)
    fake.put_script = [httpx.Response(503, text=f"Authorization: Bearer {TOKEN}")]
    fake.get_script = [
        httpx.Response(200, json={"status": "failed", "error": f"stderr contains Bearer {TOKEN} and {TOKEN}"})
    ]
    client = make_client(share, fake)
    with pytest.raises(CallasJobFailedError) as info:
        client.merge(sources, "out.pdf", job_id="job-secret")
    assert TOKEN not in str(info.value)
    assert TOKEN not in caplog.text
    assert "Bearer" not in caplog.text
    assert TOKEN not in repr(client._cfg)


def test_auth_failure_message_has_no_token(share, fake, sources, caplog):
    caplog.set_level(logging.DEBUG)
    fake.put_script = [httpx.Response(401, json={"detail": f"bad token {TOKEN}"})]
    with pytest.raises(CallasAuthError) as info:
        make_client(share, fake).merge(sources, "out.pdf", job_id="job-401")
    assert TOKEN not in str(info.value) and TOKEN not in caplog.text


# ----------------------------------------------------------------- settings


def test_callas_settings_defaults_are_disabled(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IMAGE_MAGIC_CALLAS_"):
            monkeypatch.delenv(key)
    settings = Settings.from_env()
    assert settings.callas_merge_enabled is False
    assert settings.callas_render_enabled is False
    assert settings.callas_share == Path("/mnt/shared")
    assert settings.callas_token == ""
    assert settings.callas_url == ""


def test_callas_settings_from_env_and_repr_hides_token(monkeypatch):
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_URL", "http://callas:8765")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_TOKEN", TOKEN)
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_SHARE", "/data/share")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_MERGE_ENABLED", "true")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_MAX_CONCURRENT_JOBS", "5")
    monkeypatch.setenv("IMAGE_MAGIC_CALLAS_JOB_TIMEOUT_SECONDS", "90")
    settings = Settings.from_env()
    assert settings.callas_merge_enabled is True
    assert settings.callas_render_enabled is False
    assert settings.callas_share == Path("/data/share")
    assert settings.callas_max_concurrent_jobs == 5
    assert settings.callas_job_timeout_seconds == 90
    assert TOKEN not in repr(settings)
    config = CallasConfig.from_settings(settings)
    assert config.base_url == "http://callas:8765" and config.max_concurrent_jobs == 5
