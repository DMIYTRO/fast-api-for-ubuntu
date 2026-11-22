"""«Получить заказ»: перенос файлов заказов из сетевых папок (services/order_fetch.py)."""

from __future__ import annotations

import dataclasses
import errno
import os
from pathlib import Path
import threading
import time

import pytest

from processing.batch_processor import BatchProcessor
from server.settings import Settings
from services import order_fetch
from services.order_fetch import (
    FetchAlreadyRunning,
    FetchConfig,
    FetchConfigError,
    FetchProgress,
    FetchResult,
    FetchRoute,
    OrderFetchService,
    cleanup_stale_temps,
    commit_noreplace,
    fetch_config_from_settings,
    parse_routes,
    run_fetch,
)


OLD = time.time() - 3600


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    monkeypatch.setattr(order_fetch, "RETRY_PAUSES", (0.0,))


def make(path: Path, data: bytes | int = 1000, *, mtime: float | None = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, int):
        data = os.urandom(data)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def layout(tmp_path: Path):
    src_a = tmp_path / "src" / "offset"
    src_b = tmp_path / "src" / "digital"
    dst = tmp_path / "input"
    for folder in (src_a, src_b, dst):
        folder.mkdir(parents=True)
    return src_a, src_b, dst


def config_for(tmp_path: Path, sources, dst: Path, **overrides) -> FetchConfig:
    values = dict(
        routes=(FetchRoute(tuple(sources), dst, "Auto"),),
        workers=2,
        mode="move",
        min_age_seconds=10.0,
        retries=3,
        require_mount_root=None,
        log_path=tmp_path / "logs" / "order-fetch.log",
        stable_seconds=0.0,
    )
    values.update(overrides)
    return FetchConfig(**values)


def names(items) -> list[str]:
    return sorted(item.name for item in items)


def temps(folder: Path) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.startswith(".imfetch-")]


class HookProgress(FetchProgress):
    """Прогресс, вызывающий ``hook`` при каждом скопированном блоке."""

    def __init__(self, hook):
        super().__init__()
        self.hook = hook
        self.snapshots: list[dict] = []

    def add_bytes(self, count: int) -> None:
        super().add_bytes(count)
        self.snapshots.append(self.snapshot())
        if count > 0:
            self.hook()


# --------------------------------------------------------------------------- basic


def test_moves_files_and_verifies(tmp_path):
    src_a, src_b, dst = layout(tmp_path)
    a = make(src_a / "a.pdf", 5000)
    b = make(src_b / "b.tif", 7000)
    payload_a, payload_b = a.read_bytes(), b.read_bytes()

    result = run_fetch(config_for(tmp_path, (src_a, src_b), dst))

    assert names(result.moved) == ["a.pdf", "b.tif"]
    assert (dst / "a.pdf").read_bytes() == payload_a
    assert (dst / "b.tif").read_bytes() == payload_b
    assert not a.exists() and not b.exists()
    assert temps(dst) == []
    assert result.total_bytes == 12000
    data = result.as_dict()
    assert set(data) == {
        "moved", "copied", "skipped", "failed", "source_errors",
        "duration_seconds", "total_bytes", "bytes_per_second",
    }
    assert set(data["moved"][0]) == {"name", "source", "destination", "size", "reason", "code"}


def test_zero_size_is_skipped_and_kept(tmp_path):
    src_a, _, dst = layout(tmp_path)
    empty = make(src_a / "empty.pdf", b"")
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.name, i.code, i.reason) for i in result.skipped] == [
        ("empty.pdf", "zero_size", "Файл пустой (0 байт)")
    ]
    assert empty.exists() and not (dst / "empty.pdf").exists()


def test_still_writing_is_skipped(tmp_path):
    src_a, _, dst = layout(tmp_path)
    fresh = make(src_a / "fresh.pdf", 100, mtime=None)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [i.code for i in result.skipped] == ["still_writing"]
    assert fresh.exists() and not (dst / "fresh.pdf").exists()


def test_subfolders_skipped_service_folders_silent(tmp_path):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "sub" / "inner.pdf")
    make(src_a / "tmpbxvg30u_" / "x.pdf")
    make(src_a / ".hidden" / "y.pdf")
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.name, i.code) for i in result.skipped] == [("sub", "subfolder")]
    assert (src_a / "sub" / "inner.pdf").exists()
    assert list(dst.iterdir()) == []


def test_junk_is_ignored_silently(tmp_path):
    src_a, _, dst = layout(tmp_path)
    junk = [".DS_Store", "._a.pdf", "Thumbs.db", "desktop.ini", "~$doc.pdf", "x.tmp", "y.part",
            "z.crdownload", ".imfetch-abc-a.pdf.part"]
    for name in junk:
        make(src_a / name)
    make(src_a / "real.pdf")
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert names(result.moved) == ["real.pdf"]
    assert result.skipped == [] and result.failed == []
    assert all((src_a / name).exists() for name in junk)


# --------------------------------------------------------------------------- conflicts


def test_identical_file_in_destination_removes_source(tmp_path):
    src_a, _, dst = layout(tmp_path)
    data = os.urandom(3000)
    source = make(src_a / "a.pdf", data)
    make(dst / "a.pdf", data)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.name, i.code, i.reason) for i in result.moved] == [
        ("a.pdf", "already_present", "Уже был в рабочей папке")
    ]
    assert not source.exists() and (dst / "a.pdf").read_bytes() == data
    assert result.total_bytes == 0


def test_identical_file_in_copy_mode_is_skipped(tmp_path):
    src_a, _, dst = layout(tmp_path)
    data = os.urandom(3000)
    source = make(src_a / "a.pdf", data)
    make(dst / "a.pdf", data)
    result = run_fetch(config_for(tmp_path, (src_a,), dst, mode="copy"))
    assert [(i.name, i.code) for i in result.skipped] == [("a.pdf", "already_present")]
    assert source.exists()


@pytest.mark.parametrize("other", [b"x" * 3000, b"y" * 10])
def test_different_file_in_destination_is_conflict(tmp_path, other):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", b"a" * 3000)
    make(dst / "a.pdf", other)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.name, i.code) for i in result.failed] == [("a.pdf", "name_conflict")]
    assert source.read_bytes() == b"a" * 3000
    assert (dst / "a.pdf").read_bytes() == other
    assert temps(dst) == []


def test_same_name_in_two_sources(tmp_path):
    src_a, src_b, dst = layout(tmp_path)
    first = make(src_a / "a.pdf", b"1" * 100)
    second = make(src_b / "a.pdf", b"2" * 100)
    result = run_fetch(config_for(tmp_path, (src_a, src_b), dst))
    assert [i.source for i in result.moved] == [str(first)]
    assert [(i.source, i.code) for i in result.failed] == [(str(second), "name_conflict")]
    assert second.exists() and (dst / "a.pdf").read_bytes() == b"1" * 100


def test_file_appearing_during_copy_is_not_overwritten(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 50_000)
    monkeypatch.setattr(order_fetch, "CHUNK_SIZE", 10_000)

    def hook():
        if not (dst / "a.pdf").exists():
            (dst / "a.pdf").write_bytes(b"intruder")

    result = run_fetch(config_for(tmp_path, (src_a,), dst, workers=1), HookProgress(hook))
    assert [i.code for i in result.failed] == ["name_conflict"]
    assert (dst / "a.pdf").read_bytes() == b"intruder"
    assert source.exists() and temps(dst) == []


def test_commit_noreplace_fallbacks(tmp_path, monkeypatch):
    def unsupported(*_):
        raise NotImplementedError

    monkeypatch.setattr(order_fetch, "_rename_noreplace", unsupported)
    tmp = make(tmp_path / ".imfetch-1-a.part", b"a")
    assert commit_noreplace(tmp, tmp_path / "a") == "link"
    assert not tmp.exists() and (tmp_path / "a").read_bytes() == b"a"

    def no_link(*_):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_link)
    tmp = make(tmp_path / ".imfetch-2-b.part", b"b")
    assert commit_noreplace(tmp, tmp_path / "b") == "rename"
    tmp = make(tmp_path / ".imfetch-3-b.part", b"new")
    with pytest.raises(FileExistsError):
        commit_noreplace(tmp, tmp_path / "b")
    assert (tmp_path / "b").read_bytes() == b"b"


def test_commit_noreplace_native_refuses_existing(tmp_path):
    tmp = make(tmp_path / ".imfetch-1-a.part", b"new")
    make(tmp_path / "a", b"old")
    with pytest.raises(FileExistsError):
        commit_noreplace(tmp, tmp_path / "a")
    assert (tmp_path / "a").read_bytes() == b"old" and tmp.exists()


# --------------------------------------------------------------------------- integrity


def test_truncated_copy_is_size_mismatch(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 20_000)
    payload = source.read_bytes()
    real_fdopen = os.fdopen

    class Truncating:
        def __init__(self, handle):
            self.handle = handle

        def write(self, data):
            self.handle.write(bytes(data)[: len(data) // 2])  # «записалось» меньше
            return len(data)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: Truncating(real_fdopen(*a, **k)))
    result = run_fetch(config_for(tmp_path, (src_a,), dst, retries=2))
    assert [(i.name, i.code) for i in result.failed] == [("a.pdf", "size_mismatch")]
    assert "после 3 попыток" in result.failed[0].reason
    assert source.read_bytes() == payload
    assert list(dst.iterdir()) == []


def test_source_changed_during_copy(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 30_000)
    monkeypatch.setattr(order_fetch, "CHUNK_SIZE", 10_000)
    stamp = [OLD]

    def hook():
        stamp[0] += 1
        os.utime(source, (stamp[0], stamp[0]))

    result = run_fetch(config_for(tmp_path, (src_a,), dst, workers=1, retries=1), HookProgress(hook))
    assert [i.code for i in result.skipped] == ["still_writing"] and result.failed == []
    assert source.exists() and list(dst.iterdir()) == []


def test_transient_eio_is_retried(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 5000)
    payload = source.read_bytes()
    real_fsync = os.fsync
    calls = []

    def flaky(fd):
        calls.append(fd)
        if len(calls) <= 2:
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", flaky)
    progress = FetchProgress()
    result = run_fetch(config_for(tmp_path, (src_a,), dst), progress)
    assert names(result.moved) == ["a.pdf"]
    assert len(calls) == 3
    assert (dst / "a.pdf").read_bytes() == payload and temps(dst) == []
    snap = progress.snapshot()
    assert snap["done_bytes"] == snap["total_bytes"] == 5000
    log = (tmp_path / "logs" / "order-fetch.log").read_text(encoding="utf-8")
    assert "RETRY" in log


def _fail_temp_open(monkeypatch, code):
    real_open = os.open
    calls = []

    def guarded(path, *args, **kwargs):
        if Path(path).name.startswith(".imfetch-"):
            calls.append(path)
            raise OSError(code, os.strerror(code), str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded)
    return calls


def test_permission_denied_fails_immediately(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf")
    calls = _fail_temp_open(monkeypatch, errno.EACCES)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.code, i.reason.split(":")[0]) for i in result.failed] == [
        ("permission_denied", "Нет доступа к рабочей папке")
    ]
    assert len(calls) == 1 and source.exists()


def test_no_space_stops_new_files(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    sources = [make(src_a / f"f{i}.pdf") for i in range(4)]
    calls = _fail_temp_open(monkeypatch, errno.ENOSPC)
    result = run_fetch(config_for(tmp_path, (src_a,), dst, workers=1))
    assert len(calls) == 1
    assert [i.code for i in result.failed] == ["no_space"] * 4
    assert {i.reason for i in result.failed} == {"Нет места в рабочей папке"}
    assert all(path.exists() for path in sources)


def test_unknown_exception_does_not_stop_others(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "bad.pdf")
    make(src_a / "good.pdf")
    real = order_fetch._Runner._copy_once

    def boom(self, task, counted):
        if task.name == "bad.pdf":
            raise RuntimeError("неожиданно")
        return real(self, task, counted)

    monkeypatch.setattr(order_fetch._Runner, "_copy_once", boom)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert names(result.moved) == ["good.pdf"]
    assert [(i.name, i.code) for i in result.failed] == [("bad.pdf", "error")]
    assert (src_a / "bad.pdf").exists()


# --------------------------------------------------------------------------- sources


def test_missing_and_unreadable_sources(tmp_path):
    src_a, src_b, dst = layout(tmp_path)
    missing = tmp_path / "src" / "nope"
    locked = tmp_path / "src" / "locked"
    locked.mkdir()
    make(src_b / "ok.pdf")
    locked.chmod(0)
    try:
        result = run_fetch(config_for(tmp_path, (missing, locked, src_b), dst))
    finally:
        locked.chmod(0o755)
    assert names(result.moved) == ["ok.pdf"]
    errors = {Path(e["source"]).name: e for e in result.source_errors}
    assert errors["nope"]["code"] == "missing" and errors["nope"]["severity"] == "warning"
    assert errors["nope"]["message"] == f"Папка-источник не найдена: {missing}"
    if os.geteuid() != 0:
        assert errors["locked"]["code"] == "unreadable" and errors["locked"]["severity"] == "error"
    assert all({"source", "message", "code", "severity"} <= set(e) for e in result.source_errors)


def test_require_mount_root(tmp_path):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf")
    result = run_fetch(config_for(tmp_path, (src_a,), dst, require_mount_root=tmp_path / "src"))
    assert result.moved == [] and source.exists()
    assert [(e["code"], e["severity"]) for e in result.source_errors] == [("unmounted", "error")]
    assert result.source_errors[0]["message"].startswith(f"Сетевая папка не смонтирована: {tmp_path / 'src'} — ")
    assert "setup_work20_mount.sh" in result.source_errors[0]["message"]


def test_require_mount_root_accepts_mount_point(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "a.pdf")
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == tmp_path / "src")
    result = run_fetch(config_for(tmp_path, (src_a,), dst, require_mount_root=tmp_path / "src"))
    assert names(result.moved) == ["a.pdf"] and result.source_errors == []


def test_missing_destination_subfolder_is_created(tmp_path):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "a.cdr.pdf")
    result = run_fetch(config_for(tmp_path, (src_a,), dst / "CorelDRAW"))
    assert names(result.moved) == ["a.cdr.pdf"] and (dst / "CorelDRAW" / "a.cdr.pdf").is_file()


# --------------------------------------------------------------------------- delete / copy


def test_delete_failure_then_rerun(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 4000)
    real_unlink = os.unlink

    def guarded(path, *args, **kwargs):
        if Path(path) == source:
            raise OSError(errno.EACCES, "Permission denied", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", guarded)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.code, i.reason.startswith("Не удалось удалить исходник")) for i in result.copied] == [
        ("delete_failed", True)
    ]
    assert source.exists() and (dst / "a.pdf").exists()

    monkeypatch.setattr(os, "unlink", real_unlink)
    again = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.name, i.code) for i in again.moved] == [("a.pdf", "already_present")]
    assert not source.exists()


def test_copy_mode_keeps_sources(tmp_path):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 2000)
    result = run_fetch(config_for(tmp_path, (src_a,), dst, mode="copy"))
    assert names(result.copied) == ["a.pdf"] and result.moved == []
    assert source.exists() and (dst / "a.pdf").read_bytes() == source.read_bytes()
    log = (tmp_path / "logs" / "order-fetch.log").read_text(encoding="utf-8")
    assert "DELETED" not in log


def test_cancel_removes_temp_and_skips_rest(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    sources = [make(src_a / f"f{i}.pdf", 50_000) for i in range(3)]
    monkeypatch.setattr(order_fetch, "CHUNK_SIZE", 10_000)
    cancel = threading.Event()
    result = run_fetch(
        config_for(tmp_path, (src_a,), dst, workers=1), HookProgress(cancel.set), cancel
    )
    assert [i.code for i in result.skipped] == ["cancelled"] * 3
    assert all(path.exists() for path in sources)
    assert list(dst.iterdir()) == []
    log = (tmp_path / "logs" / "order-fetch.log").read_text(encoding="utf-8")
    assert log.count("CANCELLED") == 3 and "state=cancelled" in log and "cancelled=3" in log


def test_cancelled_job_state(tmp_path):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "a.pdf")
    cancel = threading.Event()
    cancel.set()
    progress = FetchProgress()
    run_fetch(config_for(tmp_path, (src_a,), dst), progress, cancel)
    assert progress.snapshot()["state"] == "cancelled"
    assert (src_a / "a.pdf").exists()


# --------------------------------------------------------------------------- progress / log / threads


def test_progress_grows_and_finishes(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    for i in range(3):
        make(src_a / f"f{i}.pdf", 30_000)
    make(src_a / "zero.pdf", b"")
    monkeypatch.setattr(order_fetch, "CHUNK_SIZE", 10_000)
    progress = HookProgress(lambda: None)
    run_fetch(config_for(tmp_path, (src_a,), dst, workers=1), progress)
    done = [snap["done_bytes"] for snap in progress.snapshots]
    assert done == sorted(done) and done[-1] == 90_000
    assert all(snap["state"] == "copying" and snap["total_bytes"] == 90_000 for snap in progress.snapshots)
    assert any(snap["current"] for snap in progress.snapshots)
    final = progress.snapshot()
    assert final["state"] == "done"
    assert final["done_files"] == final["total_files"] == 3
    assert final["finished_at"] is not None and final["current"] == []
    assert names(FetchResult(**{}).moved) == []
    assert len(final["result"]["moved"]) == 3 and len(final["result"]["skipped"]) == 1


def test_log_file_lines(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "a.pdf")
    make(src_a / "zero.pdf", b"")
    make(src_a / "b.pdf", b"b" * 10)
    make(dst / "b.pdf", b"c" * 10)
    run_fetch(config_for(tmp_path, (src_a,), dst))
    log = (tmp_path / "logs" / "order-fetch.log").read_text(encoding="utf-8")
    for tag in ("START mode=move", "COPIED", "VERIFIED", "DELETED", "MOVED", "SKIP_ZERO",
                "FAILED", "code=name_conflict", "DONE state=done moved=1", "cancelled=0"):
        assert tag in log, tag
    assert all(" job=" in line for line in log.splitlines())


def test_many_files_many_workers(tmp_path):
    src_a, src_b, dst = layout(tmp_path)
    expected = {}
    for i in range(50):
        folder = src_a if i % 2 else src_b
        path = make(folder / f"order_{i:02d}.pdf", 1000 + i)
        expected[path.name] = path.read_bytes()
    result = run_fetch(config_for(tmp_path, (src_a, src_b), dst, workers=4))
    assert len(result.moved) == 50 and len({i.name for i in result.moved}) == 50
    assert result.failed == [] and temps(dst) == []
    assert {p.name: p.read_bytes() for p in dst.iterdir()} == expected
    assert list(src_a.iterdir()) == [] and list(src_b.iterdir()) == []


def test_stale_temp_cleanup(tmp_path):
    old = make(tmp_path / ".imfetch-abc-a.pdf.part", b"x", mtime=time.time() - 7200)
    fresh = make(tmp_path / ".imfetch-def-b.pdf.part", b"x", mtime=None)
    other = make(tmp_path / ".other.part", b"x", mtime=time.time() - 7200)
    assert cleanup_stale_temps([tmp_path]) == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_scan_ignores_temp_and_dot_files(tmp_path):
    for name in (".imfetch-0123abcd-a.pdf.part", "._a.pdf", ".hidden.pdf", "a.pdf"):
        make(tmp_path / name)
    found = BatchProcessor(tmp_path, tmp_path / "PDF").scan()
    assert [path.name for path in found] == ["a.pdf"]


# --------------------------------------------------------------------------- config / service


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        database_url="sqlite://",
        password_hash=None,
        log_dir=tmp_path / "logs",
        input_dir=tmp_path / "input",
        allowed_roots=(tmp_path,),
        fetch_source_root=tmp_path / "work20",
    )
    values.update(overrides)
    return Settings(**values)


def test_config_from_settings_default_routes(tmp_path):
    config = fetch_config_from_settings(_settings(tmp_path))
    root = tmp_path / "work20"
    assert config.routes == (
        FetchRoute((root / "WORK/offset/ORDER/Auto", root / "WORK/digital/ORDER/Auto"), tmp_path / "input", "Auto"),
        FetchRoute(
            (root / "WORK/digital/ORDER/CorelDRAW", root / "WORK/offset/ORDER/CorelDRAW"),
            tmp_path / "input" / "CorelDRAW",
            "CorelDRAW",
        ),
    )
    assert config.require_mount_root == root
    assert config.log_path == tmp_path / "logs" / "order-fetch.log"
    assert (config.workers, config.mode, config.min_age_seconds, config.retries) == (4, "move", 10.0, 3)
    assert fetch_config_from_settings(_settings(tmp_path, fetch_require_mount=False)).require_mount_root is None


def test_config_rejects_destination_outside_allowed_roots(tmp_path):
    with pytest.raises(FetchConfigError, match="вне разрешённых"):
        fetch_config_from_settings(_settings(tmp_path, allowed_roots=(tmp_path / "elsewhere",)))
    with pytest.raises(FetchConfigError):
        fetch_config_from_settings(_settings(tmp_path, fetch_routes_error="IMAGE_MAGIC_FETCH_ROUTES: плохо"))


def test_parse_routes():
    assert parse_routes("") == ()
    assert parse_routes(" /a | /b > /dst ; /c>/dst2 ") == ((("/a", "/b"), "/dst"), (("/c",), "/dst2"))
    with pytest.raises(FetchConfigError):
        parse_routes("/a")
    with pytest.raises(FetchConfigError):
        parse_routes(">/dst")


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_MAGIC_DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_SOURCE_ROOT", str(tmp_path / "w"))
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_ROUTES", f"{tmp_path}/s1|{tmp_path}/s2>{tmp_path}/d")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_REQUIRE_MOUNT", "0")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_WORKERS", "6")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_MODE", "COPY")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_MIN_AGE_SECONDS", "2.5")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_RETRIES", "1")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_LOG", str(tmp_path / "f.log"))
    monkeypatch.setenv("IMAGE_MAGIC_ALLOWED_ROOTS", str(tmp_path))
    settings = Settings.from_env()
    config = fetch_config_from_settings(settings)
    assert config.routes == (FetchRoute((tmp_path / "s1", tmp_path / "s2"), tmp_path / "d", "d"),)
    assert (config.workers, config.mode, config.min_age_seconds, config.retries) == (6, "copy", 2.5, 1)
    assert config.require_mount_root is None and config.log_path == tmp_path / "f.log"

    monkeypatch.setenv("IMAGE_MAGIC_FETCH_MODE", "mvoe")  # опечатка не удаляет исходники
    assert Settings.from_env().fetch_mode == "copy"
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_ROUTES", "broken")
    with pytest.raises(FetchConfigError):
        fetch_config_from_settings(Settings.from_env())


def test_service_single_job_and_config_per_start(tmp_path):
    release = threading.Event()
    started = threading.Event()
    seen: list[FetchConfig] = []
    counter = iter(range(100))

    def factory():
        return FetchConfig(routes=(), workers=next(counter) + 1)

    def runner(config, progress, cancel):
        seen.append(config)
        started.set()
        release.wait(5)
        result = FetchResult()
        progress.finish(result)
        return result

    service = OrderFetchService(factory, runner=runner)
    assert service.status() is None
    first = service.start()
    assert first["job_id"] and first["state"] == "scanning"
    assert started.wait(5)
    with pytest.raises(FetchAlreadyRunning):
        service.start()
    assert service.status()["job_id"] == first["job_id"]
    release.set()
    assert service.wait(5)
    assert service.status()["state"] == "done"

    second = service.start()
    assert service.wait(5)
    assert second["job_id"] != first["job_id"]
    assert [config.workers for config in seen] == [1, 2]


def test_service_config_error_is_failed_job():
    def factory():
        raise FetchConfigError("Папка назначения вне разрешённых папок")

    service = OrderFetchService(factory)
    snap = service.start()
    assert snap["state"] == "failed"
    assert snap["result"]["source_errors"][0]["message"] == "Папка назначения вне разрешённых папок"
    assert snap["result"]["source_errors"][0]["severity"] == "error"
    assert service.status()["job_id"] == snap["job_id"]


def test_service_runs_real_fetch(tmp_path):
    src_a, _, dst = layout(tmp_path)
    make(src_a / "a.pdf")
    service = OrderFetchService(lambda: config_for(tmp_path, (src_a,), dst))
    service.start()
    assert service.wait(10)
    snap = service.status()
    assert snap["state"] == "done" and len(snap["result"]["moved"]) == 1
    log = (tmp_path / "logs" / "order-fetch.log").read_text(encoding="utf-8")
    assert f"job={snap['job_id']} DONE" in log


def test_config_is_frozen():
    config = FetchConfig(routes=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.workers = 3  # type: ignore[misc]


# --------------------------------------------------------------------------- growing source (D1)


def _stale_stat(monkeypatch, path: Path):
    """os.stat для ``path`` всегда отдаёт снимок, снятый сейчас (имитация кэша атрибутов CIFS)."""
    real_stat = os.stat
    frozen = real_stat(path, follow_symlinks=False)
    calls: list[float] = []

    def fake(target, *args, **kwargs):
        if Path(target) == path:
            calls.append(time.monotonic())
            return frozen
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake)
    return calls


def test_growing_file_with_stale_stat_is_not_moved(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "growing.pdf", 200_000)
    _stale_stat(monkeypatch, source)
    stop = threading.Event()

    def writer():
        while not stop.wait(0.3):
            with open(source, "ab") as handle:
                handle.write(os.urandom(50_000))

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.35)
        result = run_fetch(config_for(tmp_path, (src_a,), dst, stable_seconds=1.0))
    finally:
        stop.set()
        thread.join()
    assert [(i.name, i.code) for i in result.skipped] == [("growing.pdf", "still_writing")]
    assert result.moved == [] and result.copied == []
    assert source.exists() and list(dst.iterdir()) == []


def test_read_past_snapshot_is_still_writing(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 10_000)
    _stale_stat(monkeypatch, source)
    with open(source, "ab") as handle:  # дописано, а stat этого не видит
        handle.write(b"x" * 5000)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [(i.code, i.reason) for i in result.skipped] == [
        ("still_writing", "Файл изменился во время переноса — будет взят в следующий раз")
    ]
    assert len(source.read_bytes()) == 15_000 and list(dst.iterdir()) == []


def test_growth_during_stable_window_is_detected(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 10_000)
    _stale_stat(monkeypatch, source)

    class AppendOnWait(threading.Event):
        def wait(self, timeout=None):
            with open(source, "ab") as handle:
                handle.write(b"late")
            return False

    result = run_fetch(
        config_for(tmp_path, (src_a,), dst, stable_seconds=60.0), FetchProgress(), AppendOnWait()
    )
    assert [i.code for i in result.skipped] == ["still_writing"]
    assert source.exists() and list(dst.iterdir()) == []


def test_final_stat_waits_for_stable_window(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 1000)
    real_stat = os.stat
    calls: list[float] = []

    def tracking(target, *args, **kwargs):
        if Path(target) == source:
            calls.append(time.monotonic())
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", tracking)
    result = run_fetch(config_for(tmp_path, (src_a,), dst, stable_seconds=0.5))
    assert names(result.moved) == ["a.pdf"]
    # stat сканирования, stat перед копией, stat после окна, stat перед удалением
    assert len(calls) >= 3 and calls[2] - calls[0] >= 0.5


def test_change_before_delete_removes_own_copy(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    source = make(src_a / "a.pdf", 10_000)
    real_commit = order_fetch.commit_noreplace

    def commit_then_append(tmp, dest):
        method = real_commit(tmp, dest)
        with open(source, "ab") as handle:
            handle.write(b"more")
        return method

    monkeypatch.setattr(order_fetch, "commit_noreplace", commit_then_append)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [i.code for i in result.skipped] == ["still_writing"] and result.moved == []
    assert source.stat().st_size == 10_004 and list(dst.iterdir()) == []


def test_change_before_delete_keeps_foreign_destination(tmp_path, monkeypatch):
    src_a, _, dst = layout(tmp_path)
    data = os.urandom(3000)
    source = make(src_a / "a.pdf", data)
    make(dst / "a.pdf", data)
    real_hash = order_fetch._sha256
    calls = []

    def hash_then_touch(path, cancel):
        digest = real_hash(path, cancel)
        calls.append(path)
        if len(calls) == 2:
            os.utime(source, (OLD + 5, OLD + 5))
        return digest

    monkeypatch.setattr(order_fetch, "_sha256", hash_then_touch)
    result = run_fetch(config_for(tmp_path, (src_a,), dst))
    assert [i.code for i in result.skipped] == ["still_writing"]
    assert source.exists() and (dst / "a.pdf").read_bytes() == data


# --------------------------------------------------------------------------- D2 / D3 / D7


def test_all_sources_missing_is_error(tmp_path):
    _, _, dst = layout(tmp_path)
    result = run_fetch(config_for(tmp_path, (tmp_path / "x", tmp_path / "y"), dst))
    assert [(e["code"], e["severity"]) for e in result.source_errors] == [("missing", "error")] * 2


def test_one_source_missing_is_warning(tmp_path):
    src_a, _, dst = layout(tmp_path)
    result = run_fetch(config_for(tmp_path, (src_a, tmp_path / "y"), dst))
    assert [(e["code"], e["severity"]) for e in result.source_errors] == [("missing", "warning")]


def test_source_folder_vanishing_mid_run(tmp_path, monkeypatch):
    src_a, src_b, dst = layout(tmp_path)
    for i in range(5):
        make(src_a / f"f{i}.pdf", 30_000)
    make(src_b / "other.pdf", 1000)
    monkeypatch.setattr(order_fetch, "CHUNK_SIZE", 10_000)
    removed = []

    def hook():
        if not removed:
            removed.append(True)
            for path in src_a.iterdir():
                path.unlink()
            src_a.rmdir()

    result = run_fetch(config_for(tmp_path, (src_a, src_b), dst, workers=1), HookProgress(hook))
    vanished = [e for e in result.source_errors if e["code"] == "vanished"]
    assert len(vanished) == 1 and vanished[0]["severity"] == "error"
    assert vanished[0]["message"] == "Папка-источник пропала во время переноса"
    assert [i.code for i in result.skipped] == ["source_vanished"] * 5
    assert result.failed == [] and names(result.moved) == ["other.pdf"]
    assert temps(dst) == []


def test_route_labels_from_env_routes(tmp_path):
    routes = (
        (("/w/WORK/offset/ORDER/Auto", "/w/WORK/digital/ORDER/Auto"), str(tmp_path / "in")),
        (("/w/a/CorelDRAW", "/w/b/Other"), str(tmp_path / "in" / "Corel")),
    )
    config = fetch_config_from_settings(_settings(tmp_path, fetch_routes=routes))
    assert [route.label for route in config.routes] == ["Auto", "Corel"]


def test_cross_destination_warns_once(tmp_path, caplog):
    src_a, src_b, dst = layout(tmp_path)
    corel = dst / "CorelDRAW"
    corel.mkdir()
    make(src_a / "same.pdf")
    make(src_b / "same.pdf")
    make(dst / "old.pdf")
    make(src_b / "old.pdf")
    config = dataclasses.replace(
        config_for(tmp_path, (src_a,), dst),
        routes=(FetchRoute((src_a,), dst, "Auto"), FetchRoute((src_b,), corel, "CorelDRAW")),
    )
    with caplog.at_level("WARNING", logger="image_magic.order_fetch"):
        order_fetch.scan_fetch(config)
    warnings = [r.getMessage() for r in caplog.records if "NAME_IN_OTHER_DESTINATION" in r.getMessage()]
    assert len(warnings) == 2
    assert sum("same.pdf" in w for w in warnings) == 1 and sum("old.pdf" in w for w in warnings) == 1


def test_stable_seconds_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("IMAGE_MAGIC_DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    monkeypatch.setenv("IMAGE_MAGIC_FETCH_STABLE_SECONDS", "3.5")
    monkeypatch.setenv("IMAGE_MAGIC_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("IMAGE_MAGIC_INPUT_DIR", str(tmp_path / "in"))
    assert fetch_config_from_settings(Settings.from_env()).stable_seconds == 3.5
    assert FetchConfig(routes=()).stable_seconds == 2.0
