"""HTTP-ручки «Получить заказ» (server/order_fetch_routes.py) и их подключение в create_app."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

import control_panel  # noqa: E402  (pytest only: conftest isolates the database)
from server.settings import Settings
from services.order_fetch import (
    FetchAlreadyRunning,
    FetchConfig,
    FetchRoute,
    OrderFetchService,
)

PASSWORD = "correct horse"
PASSWORD_HASH = PasswordHasher().hash(PASSWORD)


class FakeFetchService:
    """Тот же интерфейс, что у OrderFetchService: start()/status()/cancel()/wait()."""

    def __init__(self, *, running: bool = False, error: Exception | None = None) -> None:
        self.running = running
        self.error = error
        self.snapshot: dict | None = None
        self.starts = 0

    def start(self) -> dict:
        if self.error is not None:
            raise self.error
        if self.running:
            raise FetchAlreadyRunning(self.snapshot)
        self.starts += 1
        self.running = True
        self.snapshot = {"job_id": "job1", "state": "scanning", "total_files": 0, "done_files": 0,
                         "total_bytes": 0, "done_bytes": 0, "current": [], "result": None}
        return self.snapshot

    def status(self) -> dict | None:
        return self.snapshot

    def cancel(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return True


def make_settings(root: Path, **overrides) -> Settings:
    return Settings(
        database_url=f"sqlite:///{root / 'state.sqlite3'}",
        password_hash=PASSWORD_HASH,
        login_failure_delay_seconds=0,
        log_dir=root / "logs",
        log_heartbeat_seconds=10,
        **overrides,
    )


@pytest.fixture()
def make_client(tmp_path):
    contexts = []

    def factory(service=None, *, login: bool = True, settings: Settings | None = None) -> TestClient:
        app = control_panel.create_app(
            settings=settings or make_settings(tmp_path),
            allowed_roots=(tmp_path,),
            default_input_dir=tmp_path,
            order_fetch_service=service,
            initialize_schema=True,
        )
        context = TestClient(app)
        client = context.__enter__()
        contexts.append(context)
        if login:
            assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        return client

    yield factory
    for context in reversed(contexts):
        context.__exit__(None, None, None)


def test_requires_session(make_client):
    service = FakeFetchService()
    client = make_client(service, login=False)
    for method in ("get", "post"):
        response = getattr(client, method)("/api/order-fetch")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_required"
    assert service.starts == 0


def test_idle_before_first_fetch(make_client):
    response = make_client(FakeFetchService()).get("/api/order-fetch")
    assert response.status_code == 200
    assert response.json() == {"state": "idle"}


def test_start_returns_202_snapshot_and_status_follows(make_client):
    service = FakeFetchService()
    client = make_client(service)
    response = client.post("/api/order-fetch")
    assert response.status_code == 202
    assert response.json()["job_id"] == "job1"
    assert response.json()["state"] == "scanning"
    assert client.get("/api/order-fetch").json()["job_id"] == "job1"


def test_second_start_is_409_in_russian(make_client):
    service = FakeFetchService()
    client = make_client(service)
    assert client.post("/api/order-fetch").status_code == 202
    response = client.post("/api/order-fetch")
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "order_fetch_running", "message": "Получение заказов уже идёт"}}
    assert service.starts == 1


def test_unexpected_start_error_is_actionable_500(make_client):
    client = make_client(FakeFetchService(error=RuntimeError("диск недоступен")))
    response = client.post("/api/order-fetch")
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "order_fetch_failed"
    assert "диск недоступен" in body["message"]


def test_default_service_is_built_from_settings_and_reads_config_per_start(make_client, tmp_path):
    """Без явного сервиса create_app собирает OrderFetchService из Settings (маршруты по умолчанию)."""
    source_root = tmp_path / "work20"
    auto = source_root / "WORK" / "digital" / "ORDER" / "Auto"
    auto.mkdir(parents=True)
    input_dir = tmp_path / "input"
    (input_dir / "CorelDRAW").mkdir(parents=True)
    settings = make_settings(
        tmp_path,
        input_dir=input_dir,
        allowed_roots=(tmp_path,),
        fetch_source_root=source_root,
        fetch_require_mount=False,
        fetch_min_age_seconds=0,
        fetch_mode="copy",
    )
    client = make_client(settings=settings)
    assert isinstance(client.app.state.order_fetch_service, OrderFetchService)
    # Файл появился после старта приложения: конфиг/сканирование берутся при каждом запуске.
    (auto / "(1-100)_digital-face.pdf").write_bytes(b"%PDF-1.4 test")

    assert client.post("/api/order-fetch").status_code == 202
    assert client.app.state.order_fetch_service.wait(10)
    snapshot = client.get("/api/order-fetch").json()
    assert snapshot["state"] == "done"
    assert [item["name"] for item in snapshot["result"]["copied"]] == ["(1-100)_digital-face.pdf"]
    assert (input_dir / "(1-100)_digital-face.pdf").read_bytes() == b"%PDF-1.4 test"
    assert (auto / "(1-100)_digital-face.pdf").exists()  # режим copy: исходник на месте


def test_real_service_moves_files_and_refuses_parallel_start(make_client, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    destination = tmp_path / "dest"
    destination.mkdir()
    payload = os.urandom(64 * 1024)
    (source / "a.pdf").write_bytes(payload)
    (source / "empty.pdf").write_bytes(b"")
    old = time.time() - 60
    for name in ("a.pdf", "empty.pdf"):
        os.utime(source / name, (old, old))

    gate = threading.Event()
    from services.order_fetch import run_fetch

    def gated_runner(config, progress, cancel):
        gate.wait(10)
        return run_fetch(config, progress, cancel)

    config = FetchConfig(
        routes=(FetchRoute(sources=(source,), destination=destination, label="Auto"),),
        workers=2, min_age_seconds=0, retries=0, log_path=tmp_path / "logs" / "order-fetch.log",
    )
    service = OrderFetchService(lambda: config, runner=gated_runner)
    client = make_client(service)

    first = client.post("/api/order-fetch")
    assert first.status_code == 202
    job_id = first.json()["job_id"]
    second = client.post("/api/order-fetch")
    assert second.status_code == 409
    assert second.json()["error"]["message"] == "Получение заказов уже идёт"

    gate.set()
    assert service.wait(10)
    snapshot = client.get("/api/order-fetch").json()
    assert snapshot["job_id"] == job_id
    assert snapshot["state"] == "done"
    result = snapshot["result"]
    assert [item["name"] for item in result["moved"]] == ["a.pdf"]
    assert [(item["name"], item["code"]) for item in result["skipped"]] == [("empty.pdf", "zero_size")]
    assert (destination / "a.pdf").read_bytes() == payload
    assert not (source / "a.pdf").exists()
    assert (source / "empty.pdf").exists()


def test_shutdown_cancels_running_fetch(tmp_path):
    class Running(FakeFetchService):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def cancel(self) -> bool:
            self.calls.append("cancel")
            return True

        def wait(self, timeout: float | None = None) -> bool:
            self.calls.append(f"wait:{timeout}")
            return True

    service = Running()
    app = control_panel.create_app(
        settings=make_settings(tmp_path), allowed_roots=(tmp_path,), default_input_dir=tmp_path,
        order_fetch_service=service, initialize_schema=True,
    )
    with TestClient(app):
        pass
    assert service.calls == ["cancel", "wait:15"]
