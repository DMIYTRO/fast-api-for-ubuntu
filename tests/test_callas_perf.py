"""Регрессии производительности клиента callas: опрос, область слота, хеширование, prepress.

Живые службы не используются: HTTP — ``httpx.MockTransport`` (см. test_callas_client),
callas для prepress — ``FakeCallasProcessClient``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx
import pytest

import services.callas.client as client_module
from server.settings import Settings
from services.callas import (
    CallasCancelledError,
    CallasClient,
    CallasConfig,
    CallasConfigError,
    CallasJobFailedError,
    CallasResultError,
    CallasTimeoutError,
    CallasValidationError,
    make_callas_pdf_merger,
)
from services.callas.prepress import PPI_PROFILE, WARN_FONTS_PROFILE, PrepressAnalyzer
from tests.fake_callas import (
    FakeCallasProcessClient,
    fonts_report_xml,
    make_separations,
    ppi_report_xml,
)
from tests.test_callas_client import FakeCallas, done_merge, make_client, sha


@pytest.fixture()
def share(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


@pytest.fixture()
def fake(share: Path) -> FakeCallas:
    return FakeCallas(share)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    path = tmp_path / "in.pdf"
    path.write_bytes(b"%PDF-1.4 perf source" * 50)
    return path


def running() -> httpx.Response:
    return httpx.Response(200, json={"status": "running"})


def slot_is_free(client: CallasClient) -> bool:
    """Слот свободен и не освобождён лишний раз (BoundedSemaphore выбросил бы ValueError)."""
    if not client._slots.acquire(blocking=False):
        return False
    client._slots.release()
    return True


def all_slots_free(client: CallasClient, count: int) -> bool:
    taken = 0
    try:
        while client._slots.acquire(blocking=False):
            taken += 1
        return taken == count
    finally:
        for _ in range(taken):
            client._slots.release()


# --------------------------------------------------------------------- polling


def record_waits(client: CallasClient) -> list[float]:
    waits: list[float] = []
    client._wait = lambda ctx, seconds: waits.append(seconds)  # type: ignore[method-assign]
    return waits


def test_job_done_on_first_poll_does_not_wait(share, fake, source):
    fake.get_script = [lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=0.2, poll_max_interval_seconds=2.0)
    waits = record_waits(client)
    client.merge([source], "out.pdf", job_id="poll-1")
    assert waits == []
    assert fake.methods().count("GET") == 1


def test_job_done_on_second_poll_waits_one_short_interval(share, fake, source):
    fake.get_script = [lambda j: running(), lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=0.2, poll_max_interval_seconds=2.0)
    waits = record_waits(client)
    client.merge([source], "out.pdf", job_id="poll-2")
    assert waits == [0.2]


def test_quick_job_finishes_well_under_a_second_in_real_time(share, fake, source):
    fake.get_script = [lambda j: running(), lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=0.2, poll_max_interval_seconds=2.0)
    started = time.monotonic()
    client.merge([source], "out.pdf", job_id="poll-rt")
    assert time.monotonic() - started < 0.8


def test_poll_schedule_grows_gradually_and_respects_maximum(share, fake, source):
    fake.get_script = [lambda j: running()] * 14 + [lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=0.2, poll_max_interval_seconds=2.0)
    waits = record_waits(client)
    client.merge([source], "out.pdf", job_id="poll-sched")
    assert len(waits) == 14
    assert waits[0] == pytest.approx(0.2)
    assert max(waits) == pytest.approx(2.0)
    assert all(later >= earlier for earlier, later in zip(waits, waits[1:]))
    assert all(later <= earlier * 1.5 + 1e-9 for earlier, later in zip(waits, waits[1:]))
    # Быстрые задания (≈1 с) опрашиваются несколько раз до потолка, а не раз в 1–5 с.
    assert sum(1 for total in _cumulative(waits) if total <= 1.5) >= 4


def _cumulative(values: list[float]) -> list[float]:
    total, result = 0.0, []
    for value in values:
        total += value
        result.append(total)
    return result


def test_poll_schedule_respects_a_custom_small_maximum(share, fake, source):
    fake.get_script = [lambda j: running()] * 8 + [lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=0.2, poll_max_interval_seconds=0.5)
    waits = record_waits(client)
    client.merge([source], "out.pdf", job_id="poll-cap")
    assert max(waits) == pytest.approx(0.5)


def test_maximum_below_initial_interval_is_raised_to_it(share, fake, source):
    fake.get_script = [lambda j: running()] * 3 + [lambda j: done_merge(fake, j)]
    client = make_client(share, fake, poll_interval_seconds=3.0, poll_max_interval_seconds=1.0)
    waits = record_waits(client)
    client.merge([source], "out.pdf", job_id="poll-clamp")
    assert waits == [3.0, 3.0, 3.0]


def test_config_defaults_match_server_capacity():
    config = CallasConfig(base_url="http://x", token="t")
    assert config.poll_interval_seconds == 0.2
    assert config.poll_max_interval_seconds == 2.0
    assert config.max_concurrent_jobs == 6


# -------------------------------------------------------------------- settings


@pytest.fixture()
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("IMAGE_MAGIC_CALLAS_"):
            monkeypatch.delenv(key)
    return monkeypatch


def test_settings_defaults_for_polling_and_concurrency(clean_env):
    settings = Settings.from_env()
    assert settings.callas_poll_interval_seconds == 0.2
    assert settings.callas_poll_max_interval_seconds == 2.0
    assert settings.callas_max_concurrent_jobs == 6
    config = CallasConfig.from_settings(settings)
    assert (config.poll_interval_seconds, config.poll_max_interval_seconds) == (0.2, 2.0)
    assert config.max_concurrent_jobs == 6


def test_settings_polling_env_overrides(clean_env):
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_INTERVAL_SECONDS", "0.5")
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_MAX_INTERVAL_SECONDS", "3")
    clean_env.setenv("IMAGE_MAGIC_CALLAS_MAX_CONCURRENT_JOBS", "3")
    settings = Settings.from_env()
    assert settings.callas_poll_interval_seconds == 0.5
    assert settings.callas_poll_max_interval_seconds == 3.0
    assert settings.callas_max_concurrent_jobs == 3
    config = CallasConfig.from_settings(settings)
    assert (config.poll_interval_seconds, config.poll_max_interval_seconds) == (0.5, 3.0)


def test_settings_old_interval_variable_alone_keeps_working(clean_env):
    # Старая конфигурация задаёт только интервал: максимум не должен стать меньше него.
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_INTERVAL_SECONDS", "4")
    settings = Settings.from_env()
    assert settings.callas_poll_interval_seconds == 4.0
    assert settings.callas_poll_max_interval_seconds == 4.0


def test_settings_maximum_below_interval_is_raised_and_interval_has_floor(clean_env):
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_INTERVAL_SECONDS", "1.5")
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_MAX_INTERVAL_SECONDS", "0.5")
    settings = Settings.from_env()
    assert settings.callas_poll_max_interval_seconds >= settings.callas_poll_interval_seconds
    clean_env.setenv("IMAGE_MAGIC_CALLAS_POLL_INTERVAL_SECONDS", "0.001")
    clean_env.delenv("IMAGE_MAGIC_CALLAS_POLL_MAX_INTERVAL_SECONDS")
    assert Settings.from_env().callas_poll_interval_seconds == 0.1


# ------------------------------------------------------------------ slot scope


def dispatch_by_job(fake: FakeCallas, handlers: dict):
    """Один скрипт GET на все задания: ответ выбирает обработчик по job_id."""

    def respond(job_id: str):
        return handlers[job_id](job_id)

    fake.get_script = [respond] * 50


def test_slot_is_free_while_another_job_is_staging(share, fake, source, monkeypatch):
    staging_a = threading.Event()
    release_a = threading.Event()
    original = client_module._copy_with_sha256

    def slow_copy(src, dst):
        if "job-a" in str(dst):
            staging_a.set()
            assert release_a.wait(5)
        return original(src, dst)

    monkeypatch.setattr(client_module, "_copy_with_sha256", slow_copy)
    dispatch_by_job(fake, {"job-a": lambda j: done_merge(fake, j), "job-b": lambda j: done_merge(fake, j)})
    client = make_client(share, fake, max_concurrent_jobs=1)

    thread_a = threading.Thread(target=lambda: client.merge([source], "out.pdf", job_id="job-a"))
    thread_a.start()
    try:
        assert staging_a.wait(5)
        # Слот единственный, а A ещё копирует вход: B обязан пройти целиком.
        started = time.monotonic()
        client.merge([source], "out.pdf", job_id="job-b")
        assert time.monotonic() - started < 3
        assert thread_a.is_alive()
        assert "job-a" not in [r.url.path.rsplit("/", 1)[-1] for r in fake.requests]
    finally:
        release_a.set()
        thread_a.join(5)
    assert not thread_a.is_alive()
    assert (share / ".callas" / "job-a" / "out" / "out.pdf").is_file()
    assert slot_is_free(client)


def test_slot_is_free_while_another_job_is_verifying_result(share, fake, source):
    verifying_a = threading.Event()
    release_a = threading.Event()
    client = make_client(share, fake, max_concurrent_jobs=1)
    original = client._check_local

    def slow_check(ctx, local, local_out, size, sha_value):
        if ctx.job_id == "job-a":
            verifying_a.set()
            assert release_a.wait(5)
        return original(ctx, local, local_out, size, sha_value)

    client._check_local = slow_check  # type: ignore[method-assign]
    dispatch_by_job(fake, {"job-a": lambda j: done_merge(fake, j), "job-b": lambda j: done_merge(fake, j)})

    thread_a = threading.Thread(target=lambda: client.merge([source], "out.pdf", job_id="job-a"))
    thread_a.start()
    try:
        assert verifying_a.wait(5)
        started = time.monotonic()
        client.merge([source], "out.pdf", job_id="job-b")
        assert time.monotonic() - started < 3
        assert thread_a.is_alive()
    finally:
        release_a.set()
        thread_a.join(5)
    assert not thread_a.is_alive()
    assert slot_is_free(client)


def test_slot_is_held_while_job_is_pending_on_server(share, fake, source):
    release_a = threading.Event()
    polled_a = threading.Event()

    def pending_a(job_id):
        polled_a.set()
        assert release_a.wait(5)
        return done_merge(fake, job_id)

    dispatch_by_job(fake, {"job-a": pending_a, "job-b": lambda j: done_merge(fake, j)})
    client = make_client(share, fake, max_concurrent_jobs=1)
    thread_a = threading.Thread(target=lambda: client.merge([source], "out.pdf", job_id="job-a"))
    thread_b = threading.Thread(target=lambda: client.merge([source], "out.pdf", job_id="job-b"))
    thread_a.start()
    try:
        assert polled_a.wait(5)
        thread_b.start()
        time.sleep(0.3)
        puts = [r.url.path for r in fake.requests if r.method == "PUT"]
        assert puts == ["/jobs/job-a"]  # B ждёт слот и ничего не отправил
    finally:
        release_a.set()
    thread_a.join(5)
    thread_b.join(5)
    assert [r.url.path for r in fake.requests if r.method == "PUT"] == ["/jobs/job-a", "/jobs/job-b"]
    assert slot_is_free(client)


def _failure_cases(fake: FakeCallas, share: Path, source: Path):
    def put_422():
        fake.put_script = [httpx.Response(422, json={"detail": "bad"})]
        return CallasValidationError, {}

    def job_failed():
        fake.get_script = [httpx.Response(200, json={"status": "failed", "error": "x"})]
        return CallasJobFailedError, {}

    def sha_mismatch():
        def build(job_id):
            entry = fake.out_file(job_id, "out.pdf", b"%PDF-real")
            entry["sha256"] = "0" * 64
            return httpx.Response(200, json={"status": "done", "files": [entry]})

        fake.get_script = [build]
        return CallasResultError, {}

    def deadline():
        return CallasTimeoutError, {"job_timeout_seconds": 0.3}

    return {
        "put_rejected": put_422,
        "job_failed": job_failed,
        "result_verification_failed": sha_mismatch,
        "deadline": deadline,
    }


@pytest.mark.parametrize("case", ["put_rejected", "job_failed", "result_verification_failed", "deadline"])
def test_slot_is_released_on_every_failure_and_job_still_runs_after(share, fake, source, case):
    error, overrides = _failure_cases(fake, share, source)[case]()
    client = make_client(share, fake, max_concurrent_jobs=1, **overrides)
    with pytest.raises(error):
        client.merge([source], "out.pdf", job_id="fail-1")
    assert slot_is_free(client)
    # Следующее задание не зависает на утёкшем слоте.
    fake.put_script, fake.get_script = [], [lambda j: done_merge(fake, j)]
    client2_started = time.monotonic()
    client.merge([source], "out.pdf", job_id="ok-after")
    assert time.monotonic() - client2_started < 2
    assert slot_is_free(client)


def test_slot_is_released_after_cancellation_during_polling(share, fake, source):
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()
    client = make_client(share, fake, max_concurrent_jobs=1, job_timeout_seconds=10)
    with pytest.raises(CallasCancelledError):
        client.merge([source], "out.pdf", job_id="cancel-1", cancel=cancel)
    assert slot_is_free(client)


def test_cancel_while_waiting_for_slot_does_not_leak_or_double_release(share, fake, source):
    release_a = threading.Event()
    polled_a = threading.Event()

    def pending_a(job_id):
        polled_a.set()
        assert release_a.wait(5)
        return done_merge(fake, job_id)

    dispatch_by_job(fake, {"job-a": pending_a})
    client = make_client(share, fake, max_concurrent_jobs=1, job_timeout_seconds=10)
    thread_a = threading.Thread(target=lambda: client.merge([source], "out.pdf", job_id="job-a"))
    thread_a.start()
    assert polled_a.wait(5)
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    with pytest.raises(CallasCancelledError):
        client.merge([source], "out.pdf", job_id="job-b", cancel=cancel)
    assert [r.url.path for r in fake.requests if r.method == "PUT"] == ["/jobs/job-a"]
    release_a.set()
    thread_a.join(5)
    assert all_slots_free(client, 1)  # ровно один слот: ни утечки, ни лишнего освобождения


def test_staging_failure_never_takes_a_slot(share, fake, tmp_path):
    client = make_client(share, fake, max_concurrent_jobs=1)
    with pytest.raises(CallasConfigError, match="не найден"):
        client.merge([tmp_path / "нет.pdf"], "out.pdf", job_id="stage-1")
    assert fake.requests == []
    assert all_slots_free(client, 1)


def test_slot_guard_releases_only_once(share, fake):
    client = make_client(share, fake, max_concurrent_jobs=1)
    ctx = client._new_ctx("guard-1", None)
    guard = client._slot(ctx)
    with guard:
        pass
    guard._release()  # повторное освобождение не должно ничего менять
    assert all_slots_free(client, 1)


# ------------------------------------------------------------------- hashing


def test_no_source_hash_when_job_id_is_given(share, fake, source, monkeypatch):
    hashed: list[str] = []
    original = client_module._sha256_file
    monkeypatch.setattr(client_module, "_sha256_file", lambda path: (hashed.append(Path(path).name), original(path))[1])
    fake.get_script = [
        lambda j: httpx.Response(
            200,
            json={
                "status": "done",
                "exit_code": 0,
                "files": [fake.out_file(j, "report.xml", b"<r/>")],
            },
        )
    ]
    client = make_client(share, fake)
    client.process(source, "Profile", "analyze", job_id="nohash-1")
    # Источник не хешируется отдельно: хеш считается при копировании.
    assert source.name not in hashed
    # Результат callas по-прежнему перепроверяется по SHA-256 из ответа службы.
    assert "report.xml" in hashed
    spec = fake.specs["nohash-1"]
    assert spec["input"]["sha256"] == sha(source.read_bytes())


def test_known_digest_replaces_hashing_for_job_id(share, fake, source):
    digest = sha(source.read_bytes())
    fake.get_script = [lambda j: httpx.Response(200, json={"status": "done", "pages": 3})]
    client = make_client(share, fake)
    assert client.info(source, source_sha256=digest.upper()) == 3
    (put,) = [r for r in fake.requests if r.method == "PUT"]
    assert put.url.path == f"/jobs/{client_module.deterministic_job_id('callas-info', digest)}"


def test_invalid_known_digest_is_rejected_before_any_request(share, fake, source):
    client = make_client(share, fake)
    with pytest.raises(CallasConfigError, match="SHA-256"):
        client.info(source, source_sha256="abc")
    assert fake.requests == []


def test_wrong_known_digest_is_detected_at_copy(share, fake, source):
    client = make_client(share, fake)
    with pytest.raises(CallasConfigError, match="контрольной суммой"):
        client.render(source, job_id="wrong-digest", source_sha256="0" * 64)
    assert fake.requests == []
    in_dir = share / ".callas" / "wrong-digest" / "in"
    assert list(in_dir.iterdir()) == []  # ни целого файла, ни .part


def test_merge_known_digests_must_match_inputs_count(share, fake, source):
    with pytest.raises(CallasConfigError, match="Число известных"):
        make_client(share, fake).merge([source], "out.pdf", job_id="m-1", input_sha256=["0" * 64] * 2)


def test_known_digest_matching_source_is_sent_to_server(share, fake, source):
    digest = sha(source.read_bytes())
    fake.get_script = [lambda j: done_merge(fake, j)]
    make_client(share, fake).merge([source], "out.pdf", job_id="m-2", input_sha256=[digest])
    assert fake.specs["m-2"]["inputs"][0]["sha256"] == digest


def _corrupting_copy(mutate):
    original = client_module._copy_with_sha256

    def copy(src, dst):
        digest, total = original(src, dst)  # честный хеш прочитанного источника
        data = Path(dst).read_bytes()
        Path(dst).write_bytes(mutate(data))  # ...но на общей папке оказалось другое
        return digest, total

    return copy


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data[:-1] + bytes([data[-1] ^ 0xFF]),  # порча байта при том же размере
        lambda data: data[: len(data) // 2],  # обрезанная передача
        lambda data: data + b"x",  # лишние байты
    ],
    ids=["same-size-corruption", "truncated", "extended"],
)
def test_corrupted_transfer_to_share_is_still_detected(share, fake, source, monkeypatch, mutate):
    monkeypatch.setattr(client_module, "_copy_with_sha256", _corrupting_copy(mutate))
    client = make_client(share, fake)
    with pytest.raises(CallasConfigError, match="не совпала с исходником"):
        client.render(source, job_id="corrupt-1")
    assert fake.requests == []  # задание на службу не отправлено
    in_dir = share / ".callas" / "corrupt-1" / "in"
    assert list(in_dir.iterdir()) == []  # испорченная копия не опубликована и .part убран


def test_source_changing_during_copy_is_detected(share, fake, source, monkeypatch):
    original = client_module._copy_with_sha256

    def copy(src, dst):
        result = original(src, dst)
        return result[0], result[1] - 1  # прочитано меньше, чем размер источника

    monkeypatch.setattr(client_module, "_copy_with_sha256", copy)
    with pytest.raises(CallasConfigError, match="изменился во время копирования"):
        make_client(share, fake).render(source, job_id="changed-1")
    assert fake.requests == []


def test_result_verification_against_server_digest_is_unchanged(share, fake, source):
    def build(job_id):
        entry = fake.out_file(job_id, "out.pdf", b"%PDF-real")
        (share / ".callas" / job_id / "out" / "out.pdf").write_bytes(b"%PDF-REAL")  # тот же размер
        return httpx.Response(200, json={"status": "done", "files": [entry]})

    fake.get_script = [build]
    with pytest.raises(CallasResultError, match="Контрольная сумма"):
        make_client(share, fake).merge([source], "out.pdf", job_id="res-1")


def test_reused_stage_still_requires_matching_content(share, fake, source):
    fake.get_script = [lambda j: done_merge(fake, j), lambda j: done_merge(fake, j)]
    client = make_client(share, fake)
    client.merge([source], "out.pdf", job_id="reuse-1")
    staged = share / ".callas" / "reuse-1" / "in" / "0001.pdf"
    staged.write_bytes(b"X" * source.stat().st_size)  # тот же размер, другое содержимое
    client.merge([source], "out.pdf", job_id="reuse-1")
    assert staged.read_bytes() == source.read_bytes()


def test_merger_does_not_read_input_files_to_build_job_id(tmp_path):
    ids: list[str] = []

    class RecordingClient:
        def merge(self, parts, name, *, job_id):
            ids.append(job_id)
            result = tmp_path / "merged.pdf"
            result.write_bytes(b"%PDF")
            return result

        def delete(self, job_id):
            pass

        def job_dir(self, job_id):
            return tmp_path / "jobs" / job_id

    merger = make_callas_pdf_merger(RecordingClient())  # type: ignore[arg-type]
    missing = [tmp_path / "нет-1.pdf", tmp_path / "нет-2.pdf"]  # прежний код упал бы на хешировании
    output = tmp_path / "out" / "result.pdf"
    output.parent.mkdir()
    merger(missing, output, "order")
    merger(missing, output, "order")
    assert output.read_bytes() == b"%PDF"
    assert len(set(ids)) == 2  # id уникален на каждую операцию


# ------------------------------------------------------------ prepress parallel


FONTS = [("AAAAAA+DejaVuSans", "TrueType", True), ("Helvetica", "Type1", False)]
PPI_IMAGES = [(1, 600.0, 8), (2, 300.0, 8)]


def prepress_client(tmp_path: Path, *, barrier: threading.Barrier | None = None, ppi=None, record=None):
    def gate(name):
        if record is not None:
            record.append((name, "start"))
        if barrier is not None:
            barrier.wait(timeout=3)  # пройдёт, только если все три проверки идут одновременно

    def fonts(pdf, mode):
        gate("fonts")
        return {"xml": fonts_report_xml(FONTS)}

    def ppi_ok(pdf, mode):
        gate("ppi")
        return {"xml": ppi_report_xml(PPI_IMAGES)}

    def separations(pdf, out):
        gate("sep")
        return make_separations(out, [{"k100": 0.6}, {"k100": 0.0}])

    return FakeCallasProcessClient(
        tmp_path / "jobs",
        {WARN_FONTS_PROFILE: fonts, PPI_PROFILE: ppi or ppi_ok},
        separations,
    )


@pytest.fixture()
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "order.pdf"
    path.write_bytes(b"%PDF-1.4 order")
    return path


def test_prepress_analyses_overlap_in_time(tmp_path, pdf):
    barrier = threading.Barrier(3)
    analyzer = PrepressAnalyzer(prepress_client(tmp_path, barrier=barrier))
    result = analyzer(pdf, "o1")
    # Барьер на 3 участника не пропустил бы последовательный запуск (BrokenBarrierError → failed).
    assert result["fonts"]["status"] == "ok"
    assert result["high_ppi"]["status"] == "ok"
    assert result["black_solid"]["status"] == "ok"


def test_prepress_parallel_result_equals_sequential_result(tmp_path, pdf):
    previous = {"fonts": {"count": 2}}
    parallel = PrepressAnalyzer(prepress_client(tmp_path / "p"))(pdf, "o1", previous)
    sequential_analyzer = PrepressAnalyzer(prepress_client(tmp_path / "s"))
    sequential = {
        "fonts": sequential_analyzer.check_fonts(pdf, "o1", previous["fonts"]),
        "high_ppi": sequential_analyzer.check_high_ppi(pdf, "o1"),
        "black_solid": sequential_analyzer.check_black_solid(pdf, "o1"),
    }
    assert parallel == sequential
    assert list(parallel) == ["fonts", "high_ppi", "black_solid"]
    assert parallel["fonts"]["count"] == 2 and parallel["high_ppi"]["count"] == 1
    assert parallel["black_solid"]["detected"] is True and parallel["black_solid"]["pages"] == [1]


def test_prepress_one_failure_is_reported_as_before_and_does_not_hide_others(tmp_path, pdf):
    record: list = []

    def ppi_broken(_pdf, _mode):
        record.append(("ppi", "start"))
        raise RuntimeError("callas недоступен")

    analyzer = PrepressAnalyzer(prepress_client(tmp_path / "p", ppi=ppi_broken, record=record))
    result = analyzer(pdf, "o2")
    assert result["high_ppi"] == {"status": "failed", "error": "callas недоступен"}
    assert result["fonts"]["status"] == "ok" and result["fonts"]["count"] == 2
    assert result["black_solid"]["status"] == "ok" and result["black_solid"]["detected"] is True
    assert {name for name, _ in record} == {"fonts", "ppi", "sep"}

    sequential_analyzer = PrepressAnalyzer(prepress_client(tmp_path / "s", ppi=ppi_broken))
    assert result == {
        "fonts": sequential_analyzer.check_fonts(pdf, "o2", None),
        "high_ppi": sequential_analyzer.check_high_ppi(pdf, "o2"),
        "black_solid": sequential_analyzer.check_black_solid(pdf, "o2"),
    }


def test_prepress_separations_failure_keeps_fonts_and_ppi(tmp_path, pdf):
    client = prepress_client(tmp_path)

    def broken(_pdf, _out):
        raise RuntimeError("нет цветоделений")

    client.separations_factory = broken
    result = PrepressAnalyzer(client)(pdf, "o3")
    assert result["black_solid"] == {"status": "failed", "error": "нет цветоделений"}
    assert result["fonts"]["status"] == "ok" and result["high_ppi"]["status"] == "ok"


def test_prepress_disabled_black_solid_is_skipped_without_callas_call(tmp_path, pdf):
    client = prepress_client(tmp_path)
    result = PrepressAnalyzer(client, black_solid_threshold=0)(pdf, "o4")
    assert result["black_solid"] == {"status": "skipped"}
    assert ("separations", "sep_process") not in client.calls
