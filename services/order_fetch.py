"""«Получить заказ»: перенос файлов заказов из сетевых папок во входную папку.

Перенос одного файла: копия во временное имя ``.imfetch-<uuid>-<name>.part`` в той же
папке назначения → ``fsync`` → сверка размера и неизменности исходника → переименование
в конечное имя без перезаписи (``renameat2(RENAME_NOREPLACE)``, затем ``link``) → повторный
``stat`` → удаление исходника (только в режиме ``move`` и только если всё сошлось).

Модуль не зависит от FastAPI: веб-ручки (``server/order_fetch_routes.py``) и CLI
(``scripts/fetch_orders.py``) пользуются ``run_fetch`` и ``OrderFetchService``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
import ctypes.util
from dataclasses import dataclass, field
import errno
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat as stat_module
import threading
import time
from typing import Any, Callable
import uuid


logger = logging.getLogger("image_magic.order_fetch")
logger.setLevel(logging.INFO)

MODE_MOVE = "move"
MODE_COPY = "copy"
MODES = (MODE_MOVE, MODE_COPY)

TEMP_PREFIX = ".imfetch-"
TEMP_SUFFIX = ".part"
STALE_TEMP_SECONDS = 3600.0
CHUNK_SIZE = 4 * 1024 * 1024
# Паузы перед повторами временных сетевых ошибок (дальше — последняя).
RETRY_PAUSES: tuple[float, ...] = (0.5, 1.0, 2.0)
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

STILL_WRITING_REASON = "Файл изменился во время переноса — будет взят в следующий раз"

JUNK_NAMES = frozenset({"thumbs.db", "desktop.ini", "ehthumbs.db"})
JUNK_SUFFIXES = (".tmp", ".part", ".crdownload")

TRANSIENT_ERRNOS = frozenset(
    getattr(errno, name)
    for name in (
        "EIO", "ETIMEDOUT", "EHOSTDOWN", "EHOSTUNREACH", "ENETUNREACH", "ENETDOWN",
        "ENETRESET", "ECONNRESET", "ECONNABORTED", "ECONNREFUSED", "ESTALE", "EAGAIN",
        "EBUSY", "ENOTCONN", "EPIPE", "EREMOTEIO", "ECOMM", "EINTR",
    )
    if hasattr(errno, name)
)
NO_SPACE_ERRNOS = frozenset(
    getattr(errno, name) for name in ("ENOSPC", "EDQUOT") if hasattr(errno, name)
)

DEFAULT_ROUTE_SOURCES = (
    ("Auto", ("WORK/offset/ORDER/Auto", "WORK/digital/ORDER/Auto"), ""),
    ("CorelDRAW", ("WORK/digital/ORDER/CorelDRAW", "WORK/offset/ORDER/CorelDRAW"), "CorelDRAW"),
)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class FetchRoute:
    sources: tuple[Path, ...]
    destination: Path
    label: str  # "Auto" / "CorelDRAW"


@dataclass(frozen=True)
class FetchConfig:
    routes: tuple[FetchRoute, ...]
    workers: int = 4
    mode: str = MODE_MOVE  # "move" | "copy"
    min_age_seconds: float = 10.0
    retries: int = 3
    require_mount_root: Path | None = None  # проверять ismount у этого корня
    log_path: Path | None = None
    # Окно стабильности: последний stat исходника — не раньше, чем через столько секунд
    # после stat сканирования (атрибуты CIFS кэшируются, дописываемый файл иначе не виден).
    stable_seconds: float = 2.0


class FetchConfigError(ValueError):
    """Маршруты получения заказов заданы неверно (сообщение — для оператора)."""


class FetchAlreadyRunning(RuntimeError):
    """Получение заказов уже идёт."""

    def __init__(self, snapshot: dict | None = None) -> None:
        super().__init__("Получение заказов уже идёт")
        self.snapshot = snapshot


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def parse_routes(raw: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    """``src1|src2>dst;src3>dst2`` → ``((("src1", "src2"), "dst"), ...)``; пусто → ``()``."""
    routes: list[tuple[tuple[str, ...], str]] = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.count(">") != 1:
            raise FetchConfigError(
                f"Маршрут получения заказов должен иметь вид «источник|источник>назначение»: {chunk!r}"
            )
        left, right = chunk.split(">")
        sources = tuple(item.strip() for item in left.split("|") if item.strip())
        destination = right.strip()
        if not sources or not destination:
            raise FetchConfigError(
                f"В маршруте получения заказов нет источника или назначения: {chunk!r}"
            )
        routes.append((sources, destination))
    return tuple(routes)


def default_routes(source_root: Path, input_dir: Path) -> tuple[FetchRoute, ...]:
    return tuple(
        FetchRoute(
            sources=tuple(Path(source_root) / rel for rel in sources),
            destination=Path(input_dir) / dest if dest else Path(input_dir),
            label=label,
        )
        for label, sources, dest in DEFAULT_ROUTE_SOURCES
    )


def _route_label(sources: tuple[str, ...], destination: str) -> str:
    """Имя последней папки источников (Auto/CorelDRAW), если оно у всех одно, иначе — назначения."""
    names = {Path(item).name for item in sources}
    if len(names) == 1 and next(iter(names)):
        return next(iter(names))
    return Path(destination).name or str(destination)


def fetch_config_from_settings(settings: Any) -> FetchConfig:
    """Конфиг из ``Settings``; назначение вне ``allowed_roots`` — ``FetchConfigError``."""
    routes_error = getattr(settings, "fetch_routes_error", "")
    if routes_error:
        raise FetchConfigError(routes_error)
    source_root = Path(getattr(settings, "fetch_source_root", Path("/mnt/work20")))
    input_dir = Path(settings.input_dir)
    configured = getattr(settings, "fetch_routes", ()) or ()
    if isinstance(configured, str):
        configured = parse_routes(configured)
    if configured:
        routes = tuple(
            FetchRoute(
                sources=tuple(Path(item).expanduser() for item in sources),
                destination=Path(destination).expanduser(),
                label=_route_label(sources, destination),
            )
            for sources, destination in configured
        )
    else:
        routes = default_routes(source_root, input_dir)
    allowed = tuple(Path(root).resolve() for root in getattr(settings, "allowed_roots", ()) or ())
    for route in routes:
        resolved = route.destination.resolve()
        if not allowed or not any(_is_inside(resolved, root) for root in allowed):
            raise FetchConfigError(
                f"Папка назначения «{route.destination}» вне разрешённых папок "
                "(IMAGE_MAGIC_ALLOWED_ROOTS)"
            )
        for source in route.sources:
            if _is_inside(resolved, source.resolve()) or _is_inside(source.resolve(), resolved):
                raise FetchConfigError(
                    f"Источник «{source}» и назначение «{route.destination}» вложены друг в друга"
                )
    log_path = getattr(settings, "fetch_log", None) or (
        Path(settings.log_dir) / "order-fetch.log"
    )
    mode = getattr(settings, "fetch_mode", MODE_MOVE)
    return FetchConfig(
        routes=routes,
        workers=max(1, int(getattr(settings, "fetch_workers", 4))),
        mode=mode if mode in MODES else MODE_COPY,
        min_age_seconds=float(getattr(settings, "fetch_min_age_seconds", 10.0)),
        retries=max(0, int(getattr(settings, "fetch_retries", 3))),
        require_mount_root=source_root if getattr(settings, "fetch_require_mount", True) else None,
        log_path=Path(log_path),
        stable_seconds=max(0.0, float(getattr(settings, "fetch_stable_seconds", 2.0))),
    )


# --------------------------------------------------------------------------- results


@dataclass
class FetchItem:
    name: str
    source: str
    destination: str
    size: int
    reason: str = ""
    code: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "destination": self.destination,
            "size": self.size,
            "reason": self.reason,
            "code": self.code,
        }


@dataclass
class FetchResult:
    moved: list[FetchItem] = field(default_factory=list)
    copied: list[FetchItem] = field(default_factory=list)
    skipped: list[FetchItem] = field(default_factory=list)
    failed: list[FetchItem] = field(default_factory=list)
    source_errors: list[dict] = field(default_factory=list)
    duration_seconds: float = 0.0
    total_bytes: int = 0  # перенесено/скопировано по сети байт

    @property
    def bytes_per_second(self) -> float:
        return self.total_bytes / self.duration_seconds if self.duration_seconds > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "moved": [item.as_dict() for item in self.moved],
            "copied": [item.as_dict() for item in self.copied],
            "skipped": [item.as_dict() for item in self.skipped],
            "failed": [item.as_dict() for item in self.failed],
            "source_errors": [dict(item) for item in self.source_errors],
            "duration_seconds": round(self.duration_seconds, 3),
            "total_bytes": self.total_bytes,
            "bytes_per_second": round(self.bytes_per_second, 1),
        }


class FetchProgress:
    """Потокобезопасный прогресс задачи для UI."""

    def __init__(self, job_id: str | None = None) -> None:
        self.job_id = job_id
        self._lock = threading.Lock()
        self._state = "scanning"
        self._total_files = 0
        self._done_files = 0
        self._total_bytes = 0
        self._done_bytes = 0
        self._current: list[str] = []
        self._started_at = time.time()
        self._finished_at: float | None = None
        self._result: dict | None = None

    def set_state(self, state: str) -> None:
        with self._lock:
            self._state = state

    def set_totals(self, files: int, size: int) -> None:
        with self._lock:
            self._total_files = files
            self._total_bytes = size

    def add_total(self, delta: int) -> None:
        with self._lock:
            self._total_bytes += delta

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self._done_bytes += count

    def begin_file(self, name: str) -> None:
        with self._lock:
            self._current.append(name)

    def end_file(self, name: str, remaining_bytes: int = 0) -> None:
        with self._lock:
            try:
                self._current.remove(name)
            except ValueError:
                pass
            self._done_files += 1
            self._done_bytes += max(0, remaining_bytes)

    def finish(self, result: FetchResult, state: str = "done") -> None:
        with self._lock:
            self._state = state
            self._current = []
            self._finished_at = time.time()
            self._result = result.as_dict()

    def snapshot(self) -> dict:
        with self._lock:
            end = self._finished_at or time.time()
            elapsed = max(end - self._started_at, 1e-6)
            data = {
                "state": self._state,
                "total_files": self._total_files,
                "done_files": self._done_files,
                "total_bytes": self._total_bytes,
                "done_bytes": self._done_bytes,
                "current": list(self._current),
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "bytes_per_second": round(self._done_bytes / elapsed, 1),
                "result": self._result,
            }
            if self.job_id is not None:
                data["job_id"] = self.job_id
            return data


# --------------------------------------------------------------------------- helpers


def is_junk_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        name.startswith(".")
        or name.startswith("~$")
        or lowered in JUNK_NAMES
        or lowered.endswith(JUNK_SUFFIXES)
    )


def is_temp_name(name: str) -> bool:
    return name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)


def _temp_name(name: str) -> str:
    # Имя файла ограничено 255 байтами: длинное (кириллица — 2 байта на букву) обрезаем.
    short = name.encode("utf-8")[:120].decode("utf-8", errors="ignore")
    return f"{TEMP_PREFIX}{uuid.uuid4().hex[:12]}-{short}{TEMP_SUFFIX}"


def _mb(value: float) -> str:
    return f"{value / (1024 * 1024):.1f}MB"


class _SideError(Exception):
    """OSError, привязанная к стороне: ``source`` или ``destination``."""

    def __init__(self, side: str, error: OSError) -> None:
        super().__init__(str(error))
        self.side = side
        self.error = error


class _Retry(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _Cancelled(Exception):
    pass


class _Outcome(Exception):
    """Окончательный итог файла без исключения ОС."""

    def __init__(self, bucket: str, code: str, reason: str) -> None:
        super().__init__(reason)
        self.bucket = bucket
        self.code = code
        self.reason = reason


def _src(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except OSError as exc:
        raise _SideError("source", exc) from exc


def _dst(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except OSError as exc:
        raise _SideError("destination", exc) from exc


_libc = None
try:  # pragma: no branch - платформа
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
    _renameat2 = getattr(_libc, "renameat2", None)
except OSError:  # pragma: no cover
    _renameat2 = None
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_UNSUPPORTED = frozenset(
    getattr(errno, name)
    for name in ("EINVAL", "ENOSYS", "EOPNOTSUPP", "ENOTSUP")
    if hasattr(errno, name)
)


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Атомарное переименование без перезаписи; ``NotImplementedError`` — не поддерживается."""
    if _renameat2 is None:
        raise NotImplementedError
    result = _renameat2(
        _AT_FDCWD, os.fsencode(str(src)), _AT_FDCWD, os.fsencode(str(dst)), _RENAME_NOREPLACE
    )
    if result != 0:
        code = ctypes.get_errno()
        if code in _UNSUPPORTED:
            raise NotImplementedError
        raise OSError(code, os.strerror(code), str(dst))


def commit_noreplace(tmp: Path, dest: Path) -> str:
    """Переименовать ``tmp`` в ``dest``, не перезаписывая существующий файл.

    ``FileExistsError`` — в назначении уже есть файл. На CIFS обычный ``rename`` поверх
    существующего файла его заменяет (клиент удаляет цель и повторяет), поэтому сначала
    ``renameat2(RENAME_NOREPLACE)`` (проверку делает сервер), затем жёсткая ссылка
    (``link`` не заменяет), и лишь затем проверка ``exists`` + ``rename``.
    Возвращает использованный способ (для лога).
    """
    try:
        _rename_noreplace(tmp, dest)
        return "renameat2"
    except NotImplementedError:
        pass
    try:
        os.link(tmp, dest)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED | {errno.EPERM, errno.EXDEV, errno.EMLINK}:
            raise
    else:
        try:
            os.unlink(tmp)
        except OSError as exc:  # временный файл подчистит finally
            logger.warning("order_fetch.temp_unlink_failed path=%s error=%s", tmp, exc)
        return "link"
    if os.path.lexists(dest):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(dest))
    os.rename(tmp, dest)
    return "rename"


def _sha256(path: Path, cancel: threading.Event) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            if cancel.is_set():
                raise _Cancelled()
            block = handle.read(CHUNK_SIZE)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _error_text(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _classify(side: str, exc: OSError) -> tuple[str, str, bool]:
    """(code, reason, abort) для постоянной ошибки ОС."""
    code = exc.errno
    if code in NO_SPACE_ERRNOS:
        return "no_space", "Нет места в рабочей папке", True
    if code == errno.EROFS:
        return "destination_unavailable", "Рабочая папка доступна только для чтения", side == "destination"
    if code in (errno.EACCES, errno.EPERM):
        where = "к исходному файлу" if side == "source" else "к рабочей папке"
        return "permission_denied", f"Нет доступа {where}: {_error_text(exc)}", False
    if code == errno.ENAMETOOLONG:
        return "name_too_long", "Слишком длинное имя файла", False
    if code in (errno.ENOENT, errno.ENOTDIR):
        if side == "source":
            return "source_missing", "Файл исчез (забрали другим способом)", False
        return "destination_unavailable", f"Рабочая папка недоступна: {_error_text(exc)}", True
    if code in TRANSIENT_ERRNOS:
        if side == "destination":
            return "destination_unavailable", f"Рабочая папка недоступна: {_error_text(exc)}", True
        return "network_error", f"Сетевая ошибка при чтении исходника: {_error_text(exc)}", False
    return "error", f"Ошибка: {_error_text(exc)}", False


# --------------------------------------------------------------------------- scan


@dataclass
class _Task:
    route: FetchRoute
    source: Path
    destination: Path
    size: int
    mtime_ns: int
    scanned_at: float = field(default_factory=time.monotonic)  # monotonic-время stat сканирования

    @property
    def name(self) -> str:
        return self.source.name


@dataclass
class FetchPlan:
    tasks: list[_Task] = field(default_factory=list)
    skipped: list[FetchItem] = field(default_factory=list)
    failed: list[FetchItem] = field(default_factory=list)
    source_errors: list[dict] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(task.size for task in self.tasks)

    def as_dict(self) -> dict:
        return {
            "transfer": [
                {
                    "name": task.name,
                    "source": str(task.source),
                    "destination": str(task.destination),
                    "size": task.size,
                    "label": task.route.label,
                }
                for task in self.tasks
            ],
            "skipped": [item.as_dict() for item in self.skipped],
            "failed": [item.as_dict() for item in self.failed],
            "source_errors": [dict(item) for item in self.source_errors],
            "total_bytes": self.total_bytes,
        }


def _source_error(source: Any, message: str, code: str, severity: str = "error") -> dict:
    """Элемент ``source_errors``: ``severity`` ``error`` или ``warning`` (папки нет — норма)."""
    return {"source": str(source), "message": message, "code": code, "severity": severity}


def _list_dir(path: Path, retries: int, cancel: threading.Event) -> list[os.DirEntry]:
    attempt = 0
    while True:
        try:
            with os.scandir(path) as entries:
                return sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            if exc.errno in TRANSIENT_ERRNOS and attempt < retries and not cancel.is_set():
                cancel.wait(RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)])
                attempt += 1
                continue
            raise


def scan_fetch(
    config: FetchConfig,
    *,
    job: str = "-",
    cancel: threading.Event | None = None,
    now: float | None = None,
) -> FetchPlan:
    """Что будет перенесено, не трогая файлы (используется и для ``--dry-run``)."""
    cancel = cancel or threading.Event()
    plan = FetchPlan()
    now = time.time() if now is None else now
    mount_root = config.require_mount_root
    mount_ok = None if mount_root is None else os.path.ismount(mount_root)
    for route in config.routes:
        # Отсутствующую подпапку назначения (CorelDRAW) run_fetch создаёт сам.
        if not route.destination.is_dir() and not (
            not route.destination.exists() and route.destination.parent.is_dir()
        ):
            message = f"Папка назначения недоступна: {route.destination}"
            for source in route.sources:
                plan.source_errors.append(_source_error(source, message, "destination_unavailable"))
            logger.error("job=%s SOURCE_ERROR %s: %s", job, route.label, message)
            continue
        seen: dict[str, Path] = {}
        readable = 0
        route_errors: list[dict] = []
        for source in route.sources:
            if cancel.is_set():
                return plan
            if mount_root is not None and _is_inside(Path(source), Path(mount_root)) and not mount_ok:
                message = (
                    f"Сетевая папка не смонтирована: {mount_root} — "
                    "выполните один раз: sudo bash scripts/setup_work20_mount.sh"
                )
                route_errors.append(_source_error(source, message, "unmounted"))
                logger.error("job=%s SOURCE_ERROR %s: %s", job, source, message)
                continue
            try:
                if not source.is_dir():
                    raise FileNotFoundError(errno.ENOENT, "нет такой папки", str(source))
                entries = _list_dir(source, config.retries, cancel)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    # Корень доступен, а папки нет (например, offset/ORDER/CorelDRAW) — норма.
                    message = f"Папка-источник не найдена: {source}"
                    route_errors.append(_source_error(source, message, "missing", "warning"))
                    logger.warning("job=%s SOURCE_MISSING %s", job, source)
                else:
                    message = f"Не удалось прочитать папку {source}: {_error_text(exc)}"
                    route_errors.append(_source_error(source, message, "unreadable"))
                    logger.error("job=%s SOURCE_ERROR %s: %s", job, source, message)
                continue
            readable += 1
            for entry in entries:
                _plan_entry(plan, route, source, entry, seen, config, job, now)
        if not readable:
            # Ни один источник маршрута не прочитан — это не «папки нет», а сбой маршрута.
            for item in route_errors:
                item["severity"] = "error"
            if route_errors:
                logger.error("job=%s ROUTE_UNAVAILABLE %s: ни один источник не прочитан", job, route.label)
        plan.source_errors.extend(route_errors)
    _warn_cross_destination(plan, config, job)
    return plan


def _plan_entry(
    plan: FetchPlan,
    route: FetchRoute,
    source: Path,
    entry: os.DirEntry,
    seen: dict[str, Path],
    config: FetchConfig,
    job: str,
    now: float,
) -> None:
    name = entry.name
    path = source / name
    if is_junk_name(name):
        logger.debug("job=%s SKIP junk %s", job, path)
        return
    destination = route.destination / name
    try:
        if entry.is_dir(follow_symlinks=False):
            if name.casefold().startswith("tmp"):
                # Служебные папки (tmpbxvg30u_ и т.п.) — молча.
                logger.debug("job=%s SKIP service_dir %s", job, path)
                return
            logger.info("job=%s SKIP dir %s", job, path)
            plan.skipped.append(
                FetchItem(name, str(path), str(destination), 0, "Подпапка — не переносится", "subfolder")
            )
            return
        info = os.stat(path, follow_symlinks=False)
        scanned_at = time.monotonic()
    except OSError as exc:
        reason = f"Не удалось прочитать сведения о файле: {_error_text(exc)}"
        plan.failed.append(FetchItem(name, str(path), str(destination), 0, reason, "error"))
        logger.error("job=%s FAILED %s code=error: %s", job, path, reason)
        return
    if not stat_module.S_ISREG(info.st_mode):
        logger.info("job=%s SKIP not_regular %s", job, path)
        plan.skipped.append(
            FetchItem(name, str(path), str(destination), 0, "Не обычный файл (ссылка или устройство)", "not_regular")
        )
        return
    if info.st_size == 0:
        logger.warning("job=%s SKIP_ZERO %s", job, path)
        plan.skipped.append(
            FetchItem(name, str(path), str(destination), 0, "Файл пустой (0 байт)", "zero_size")
        )
        return
    age = now - info.st_mtime
    if age < config.min_age_seconds:
        logger.info("job=%s SKIP_WRITING %s age=%.1fs", job, path, age)
        plan.skipped.append(
            FetchItem(
                name, str(path), str(destination), info.st_size,
                f"Файл ещё записывается (изменён {max(age, 0):.0f} с назад)", "still_writing",
            )
        )
        return
    key = name.casefold()  # SMB-шары нечувствительны к регистру
    if key in seen:
        reason = f"Файл с таким именем уже есть в другой папке-источнике: {seen[key]}"
        plan.failed.append(FetchItem(name, str(path), str(destination), info.st_size, reason, "name_conflict"))
        logger.error("job=%s FAILED %s code=name_conflict: %s", job, path, reason)
        return
    seen[key] = path
    plan.tasks.append(_Task(route, path, destination, info.st_size, info.st_mtime_ns, scanned_at))


def _warn_cross_destination(plan: FetchPlan, config: FetchConfig, job: str) -> None:
    destinations = list(dict.fromkeys(route.destination for route in config.routes))
    if len(destinations) < 2:
        return
    # Имена в каждом назначении: уже лежащие там (один listdir) и те, что туда переносятся.
    present: dict[Path, set[str]] = {}
    for folder in destinations:
        try:
            present[folder] = {name.casefold() for name in os.listdir(folder)}
        except OSError:
            present[folder] = set()
    for task in plan.tasks:
        present[task.destination.parent].add(task.name.casefold())
    warned: set[str] = set()
    for task in plan.tasks:
        key = task.name.casefold()
        if key in warned:
            continue
        others = [
            folder for folder in destinations
            if folder != task.destination.parent and key in present[folder]
        ]
        if others:
            warned.add(key)
            logger.warning(
                "job=%s NAME_IN_OTHER_DESTINATION %s in %s and %s "
                "(проверка сочтёт оба файла ошибкой)",
                job, task.name, task.destination.parent, ", ".join(map(str, others)),
            )


def cleanup_stale_temps(destinations: list[Path], *, job: str = "-", now: float | None = None) -> int:
    """Удалить чужие ``.imfetch-*.part`` старше часа (оставленные упавшим процессом)."""
    now = time.time() if now is None else now
    removed = 0
    for folder in destinations:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if not is_temp_name(entry.name):
                continue
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime < STALE_TEMP_SECONDS:
                    continue
                os.unlink(entry.path)
                removed += 1
                logger.info("job=%s CLEANUP %s", job, entry.path)
            except OSError as exc:
                logger.warning("job=%s CLEANUP_FAILED %s: %s", job, entry.path, exc)
    return removed


# --------------------------------------------------------------------------- transfer


class _Runner:
    def __init__(
        self, config: FetchConfig, progress: FetchProgress, cancel: threading.Event, job: str
    ) -> None:
        self.config = config
        self.progress = progress
        self.cancel = cancel
        self.job = job
        self.lock = threading.Lock()
        self.result = FetchResult()
        self.abort_reason: tuple[str, str] | None = None
        self.vanished: set[Path] = set()  # папки-источники, пропавшие во время переноса

    # ----- bookkeeping

    def _add(self, bucket: str, task: _Task, reason: str = "", code: str = "") -> None:
        item = FetchItem(task.name, str(task.source), str(task.destination), task.size, reason, code)
        with self.lock:
            getattr(self.result, bucket).append(item)

    def _abort(self, code: str, reason: str) -> None:
        with self.lock:
            if self.abort_reason is None:
                self.abort_reason = (code, reason)
                logger.error("job=%s ABORT code=%s: %s (новые файлы не начинаются)", self.job, code, reason)

    def _source_vanished(self, task: _Task) -> bool:
        """Исходника нет: пропала ли вся папка-источник (тогда — одна запись в source_errors)."""
        folder = task.source.parent
        try:
            if folder.is_dir():
                return False
        except OSError:
            pass
        with self.lock:
            if folder in self.vanished:
                return True
            self.vanished.add(folder)
            self.result.source_errors.append(
                _source_error(folder, "Папка-источник пропала во время переноса", "vanished")
            )
        logger.error("job=%s SOURCE_VANISHED %s", self.job, folder)
        return True

    def _pause(self, attempt: int) -> None:
        self.cancel.wait(RETRY_PAUSES[min(attempt, len(RETRY_PAUSES) - 1)])

    # ----- one file

    def process(self, task: _Task) -> None:
        with self.lock:
            aborted = self.abort_reason
        if aborted is not None:
            self._add("failed", task, aborted[1], aborted[0])
            logger.error("job=%s FAILED %s code=%s: %s", self.job, task.source, aborted[0], aborted[1])
            self.progress.end_file("", task.size)
            return
        if self.cancel.is_set():
            self._add("skipped", task, "Перенос отменён", "cancelled")
            logger.info("job=%s CANCELLED %s (не начат)", self.job, task.source)
            self.progress.end_file("", task.size)
            return
        with self.lock:
            vanished = task.source.parent in self.vanished
        if vanished:
            self._add("skipped", task, "Папка-источник пропала во время переноса", "source_vanished")
            logger.info("job=%s SKIP source_vanished %s", self.job, task.source)
            self.progress.end_file("", task.size)
            return
        self.progress.begin_file(task.name)
        counted = [0]
        try:
            self._transfer(task, counted)
        except _Outcome as outcome:
            self._add(outcome.bucket, task, outcome.reason, outcome.code)
            level = logging.ERROR if outcome.bucket == "failed" else logging.INFO
            tag = "FAILED" if outcome.bucket == "failed" else "SKIP"
            logger.log(level, "job=%s %s %s code=%s: %s", self.job, tag, task.source, outcome.code, outcome.reason)
        except _Cancelled:
            self._add("skipped", task, "Перенос отменён", "cancelled")
            logger.info("job=%s CANCELLED %s", self.job, task.source)
        except Exception as exc:  # noqa: BLE001 - один файл не роняет остальные
            reason = f"Сбой переноса: {exc}"
            self._add("failed", task, reason, "error")
            logger.exception("job=%s FAILED %s code=error: %s", self.job, task.source, reason)
        finally:
            self.progress.end_file(task.name, task.size - counted[0])

    def _transfer(self, task: _Task, counted: list[int]) -> None:
        attempts = self.config.retries + 1
        last: tuple[str, str] = ("error", "Неизвестная ошибка")
        for attempt in range(attempts):
            if self.cancel.is_set():
                raise _Cancelled()
            if attempt:
                self._pause(attempt - 1)
                if self.cancel.is_set():
                    raise _Cancelled()
                logger.info("job=%s RETRY %s attempt=%d after %s", self.job, task.source, attempt + 1, last[0])
            try:
                if os.path.lexists(task.destination):
                    self._handle_existing(task, counted)
                    return
                self._copy_once(task, counted)
                return
            except _Retry as exc:
                self._rollback(counted)
                last = (exc.code, exc.reason)
            except _Outcome:
                self._rollback(counted)
                raise
            except _SideError as exc:
                self._rollback(counted)
                if exc.error.errno in TRANSIENT_ERRNOS:
                    last = _classify(exc.side, exc.error)[:2]
                    if exc.side == "destination":
                        last = ("network_error", f"Сетевая ошибка рабочей папки: {_error_text(exc.error)}")
                    continue
                code, reason, abort = _classify(exc.side, exc.error)
                if code == "source_missing" and self._source_vanished(task):
                    raise _Outcome(
                        "skipped", "source_vanished", "Папка-источник пропала во время переноса"
                    ) from exc
                if abort:
                    self._abort(code, reason)
                raise _Outcome("failed", code, reason) from exc
        code, reason = last
        if code == "network_error" and "рабочей папки" in reason:
            self._abort("destination_unavailable", reason)
        raise _Outcome("failed", code, f"{reason} (после {attempts} попыток)")

    def _copy_once(self, task: _Task, counted: list[int]) -> None:
        before = _src(os.stat, task.source)
        if (before.st_size, before.st_mtime_ns) != (task.size, task.mtime_ns):
            # Любое отличие от снимка сканирования — файл дописывается (атрибуты CIFS
            # отстают, возраст по mtime здесь не довод): заберём в следующий раз.
            logger.info(
                "job=%s SOURCE_UPDATED %s size=%d->%d", self.job, task.source, task.size, before.st_size
            )
            raise _Outcome("skipped", "still_writing", STILL_WRITING_REASON)
        tmp = task.destination.parent / _temp_name(task.name)
        started = time.monotonic()
        written = 0
        try:
            src_handle = _src(open, task.source, "rb", buffering=0)
            try:
                fd = _dst(os.open, tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(fd, "wb", buffering=0) as out:
                        fd = -1
                        while True:
                            if self.cancel.is_set():
                                raise _Cancelled()
                            block = _src(src_handle.read, CHUNK_SIZE)
                            if not block:
                                break
                            view = memoryview(block)
                            while view:
                                n = _dst(out.write, view)
                                if not n:
                                    raise _SideError("destination", OSError(errno.EIO, "запись не удалась"))
                                view = view[n:]
                            written += len(block)
                            counted[0] += len(block)
                            self.progress.add_bytes(len(block))
                        _dst(out.flush)
                        _dst(os.fsync, out.fileno())
                finally:
                    if fd >= 0:
                        os.close(fd)
                elapsed = time.monotonic() - started
                if written != before.st_size:
                    # Прочитано до EOF больше/меньше снимка — файл дописывается.
                    logger.info(
                        "job=%s SOURCE_GROWING %s read=%d snapshot=%d", self.job, task.source, written, before.st_size
                    )
                    raise _Outcome("skipped", "still_writing", STILL_WRITING_REASON)
                self._wait_stable(task)
                if self._source_grew(src_handle, written):
                    raise _Outcome("skipped", "still_writing", STILL_WRITING_REASON)
            finally:
                src_handle.close()
            tmp_size = _dst(os.stat, tmp).st_size
            after = _src(os.stat, task.source)
            if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
                logger.info("job=%s SOURCE_CHANGED %s after copy", self.job, task.source)
                raise _Outcome("skipped", "still_writing", STILL_WRITING_REASON)
            if tmp_size != written:
                self._rollback(counted)
                raise _Retry(
                    "size_mismatch",
                    f"Размер копии не совпал с исходником ({tmp_size} из {before.st_size} байт)",
                )
            logger.info(
                "job=%s COPIED %s -> %s size=%d %.1fs %s/s",
                self.job, task.source, task.destination, before.st_size, elapsed,
                _mb(before.st_size / elapsed if elapsed > 0 else 0),
            )
            try:
                method = _dst(commit_noreplace, tmp, task.destination)
            except _SideError as exc:
                if isinstance(exc.error, FileExistsError):
                    logger.warning("job=%s APPEARED %s during copy", self.job, task.destination)
                    self._handle_existing(task, counted)
                    return
                raise
            final = _dst(os.stat, task.destination)
            if final.st_size != before.st_size:
                # Файл наш (только что переименован), но неверен: убираем и сообщаем.
                try:
                    os.unlink(task.destination)
                except OSError:
                    pass
                self._rollback(counted)
                raise _Retry(
                    "size_mismatch",
                    f"Размер файла в рабочей папке не совпал ({final.st_size} из {before.st_size} байт)",
                )
            logger.info("job=%s VERIFIED %s size=%d via=%s", self.job, task.name, final.st_size, method)
        finally:
            self._discard(tmp)
        self._finish_transfer(task, snapshot=before, created=True)

    def _wait_stable(self, task: _Task) -> None:
        """Дождаться конца окна стабильности, отсчитанного от stat сканирования."""
        remaining = task.scanned_at + self.config.stable_seconds - time.monotonic()
        if remaining > 0:
            if self.cancel.wait(remaining):
                raise _Cancelled()

    def _source_grew(self, handle: Any, written: int) -> bool:
        """Проверка по открытому дескриптору, мимо кэша атрибутов ``stat``.

        На CIFS ``lseek(SEEK_END)`` перечитывает размер с сервера; чтение за прочитанным
        концом ловит дописанное, даже если размер в кэше ещё старый.
        """
        fd = handle.fileno()
        try:
            end = os.lseek(fd, 0, os.SEEK_END)
            extra = os.pread(fd, 1, written)
        except OSError as exc:
            raise _SideError("source", exc) from exc
        if end != written or extra:
            logger.info("job=%s SOURCE_GROWING %s end=%d read=%d", self.job, handle.name, end, written)
            return True
        return False

    def _rollback(self, counted: list[int]) -> None:
        if counted[0]:
            self.progress.add_bytes(-counted[0])
            counted[0] = 0

    def _discard(self, tmp: Path) -> None:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("job=%s TEMP_LEFT %s: %s", self.job, tmp, exc)

    def _finish_transfer(
        self,
        task: _Task,
        *,
        already: bool = False,
        snapshot: os.stat_result | None = None,
        created: bool = False,
    ) -> None:
        if self.config.mode != MODE_MOVE:
            if already:
                raise _Outcome("skipped", "already_present", "Уже есть в рабочей папке")
            self._add("copied", task)
            with self.lock:
                self.result.total_bytes += task.size
            logger.info("job=%s COPY_ONLY %s", self.job, task.name)
            return
        if self._changed_before_delete(task, snapshot, created):
            raise _Outcome("skipped", "still_writing", STILL_WRITING_REASON)
        error = self._delete_source(task)
        if error is not None:
            reason = f"Не удалось удалить исходник: {error}"
            self._add("copied", task, reason, "delete_failed")
            if not already:
                with self.lock:
                    self.result.total_bytes += task.size
            logger.error("job=%s DELETE_FAILED %s: %s", self.job, task.source, error)
            return
        if already:
            self._add("moved", task, "Уже был в рабочей папке", "already_present")
            logger.info("job=%s MOVED %s (уже был в рабочей папке)", self.job, task.name)
        else:
            self._add("moved", task)
            with self.lock:
                self.result.total_bytes += task.size
            logger.info("job=%s MOVED %s", self.job, task.name)

    def _changed_before_delete(
        self, task: _Task, snapshot: os.stat_result | None, created: bool
    ) -> bool:
        """Последний stat исходника перед удалением; изменился — удалить только свою копию."""
        expected = (
            (snapshot.st_size, snapshot.st_mtime_ns) if snapshot is not None else (task.size, task.mtime_ns)
        )
        try:
            current = _src(os.stat, task.source)
        except _SideError as exc:
            if isinstance(exc.error, FileNotFoundError):
                return False  # исходник уже забрали: копия проверена, _delete_source это учтёт
            raise
        if (current.st_size, current.st_mtime_ns) == expected:
            return False
        logger.warning(
            "job=%s SOURCE_CHANGED_BEFORE_DELETE %s size=%d->%d", self.job, task.source, expected[0], current.st_size
        )
        if created:  # файл в назначении создан этой задачей — убираем, исходник не трогаем
            try:
                os.unlink(task.destination)
                logger.info("job=%s REMOVED_COPY %s", self.job, task.destination)
            except OSError as exc:
                logger.error("job=%s REMOVE_COPY_FAILED %s: %s", self.job, task.destination, exc)
        return True

    def _delete_source(self, task: _Task) -> str | None:
        for attempt in range(self.config.retries + 1):
            if attempt:
                self._pause(attempt - 1)
            try:
                os.unlink(task.source)
                logger.info("job=%s DELETED %s", self.job, task.source)
                return None
            except FileNotFoundError:
                logger.warning("job=%s DELETED %s (исходника уже нет)", self.job, task.source)
                return None
            except OSError as exc:
                if exc.errno in TRANSIENT_ERRNOS and attempt < self.config.retries:
                    continue
                return _error_text(exc)
        return "неизвестная ошибка"  # pragma: no cover

    def _handle_existing(self, task: _Task, counted: list[int]) -> None:
        existing = _dst(os.stat, task.destination)
        if stat_module.S_ISREG(existing.st_mode) and existing.st_size == task.size:
            source_hash = _src(_sha256, task.source, self.cancel)
            existing_hash = _dst(_sha256, task.destination, self.cancel)
            if source_hash == existing_hash:
                logger.info("job=%s ALREADY_PRESENT %s sha256=%s", self.job, task.name, source_hash[:16])
                self._finish_transfer(task, already=True)
                return
        raise _Outcome(
            "failed", "name_conflict",
            "В рабочей папке уже есть другой файл с таким именем — ничего не тронуто",
        )


def _ensure_log_handler(path: Path | None) -> logging.Handler | None:
    if path is None:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logging.getLogger("image_magic").warning("order_fetch.log_unavailable path=%s error=%s", path, exc)
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def run_fetch(
    config: FetchConfig,
    progress: FetchProgress | None = None,
    cancel: threading.Event | None = None,
) -> FetchResult:
    """Перенести файлы по всем маршрутам; одна ошибка не останавливает остальные файлы."""
    progress = progress or FetchProgress()
    cancel = cancel or threading.Event()
    job = progress.job_id or uuid.uuid4().hex[:8]
    handler = _ensure_log_handler(config.log_path)
    started = time.monotonic()
    runner = _Runner(config, progress, cancel, job)
    state = "done"
    try:
        logger.info(
            "job=%s START mode=%s workers=%d routes=%d", job, config.mode, config.workers, len(config.routes)
        )
        if config.mode not in MODES:
            raise FetchConfigError(f"Неизвестный режим получения заказов: {config.mode!r}")
        progress.set_state("scanning")
        for route in config.routes:
            # Подпапку назначения (CorelDRAW) создаём, как и scan() входной папки;
            # сам корень назначения не создаём: его отсутствие — ошибка маршрута.
            if not route.destination.exists() and route.destination.parent.is_dir():
                try:
                    route.destination.mkdir(exist_ok=True)
                except OSError as exc:
                    logger.warning("job=%s MKDIR_FAILED %s: %s", job, route.destination, exc)
        cleanup_stale_temps(
            [route.destination for route in config.routes if route.destination.is_dir()], job=job
        )
        plan = scan_fetch(config, job=job, cancel=cancel)
        runner.result.skipped.extend(plan.skipped)
        runner.result.failed.extend(plan.failed)
        runner.result.source_errors.extend(plan.source_errors)
        progress.set_totals(len(plan.tasks), plan.total_bytes)
        progress.set_state("copying")
        if plan.tasks:
            with ThreadPoolExecutor(
                max_workers=max(1, config.workers), thread_name_prefix="order-fetch"
            ) as pool:
                list(pool.map(runner.process, plan.tasks))
    except Exception as exc:  # noqa: BLE001 - задача заканчивается состоянием failed
        state = "failed"
        runner.result.source_errors.append(_source_error("", f"Сбой получения заказов: {exc}", "job_failed"))
        logger.exception("job=%s FAILED_JOB %s", job, exc)
    finally:
        result = runner.result
        result.duration_seconds = time.monotonic() - started
        cancelled = sum(1 for item in result.skipped if item.code == "cancelled")
        if state == "done" and cancel.is_set():
            state = "cancelled"
        logger.info(
            "job=%s DONE state=%s moved=%d copied=%d skipped=%d failed=%d cancelled=%d "
            "source_errors=%d bytes=%d %.1fs %s/s",
            job, state, len(result.moved), len(result.copied), len(result.skipped), len(result.failed),
            cancelled, len(result.source_errors), result.total_bytes, result.duration_seconds,
            _mb(result.bytes_per_second),
        )
        progress.finish(result, state)
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
    return result


# --------------------------------------------------------------------------- service


class OrderFetchService:
    """Одна задача получения заказов за раз, в фоновом потоке."""

    def __init__(
        self,
        config_factory: Callable[[], FetchConfig],
        *,
        runner: Callable[..., FetchResult] = run_fetch,
    ) -> None:
        self._config_factory = config_factory
        self._runner = runner
        self._lock = threading.Lock()
        self._progress: FetchProgress | None = None
        self._thread: threading.Thread | None = None
        self._cancel: threading.Event | None = None

    def start(self) -> dict:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise FetchAlreadyRunning(self._progress.snapshot() if self._progress else None)
            job_id = uuid.uuid4().hex[:12]
            progress = FetchProgress(job_id)
            cancel = threading.Event()
            self._progress = progress
            self._cancel = cancel
            try:
                config = self._config_factory()
            except Exception as exc:  # noqa: BLE001 - ошибка конфигурации — итог задачи
                logger.error("job=%s CONFIG_ERROR %s", job_id, exc)
                result = FetchResult(source_errors=[_source_error("", str(exc), "config")])
                progress.finish(result, "failed")
                self._thread = None
                return progress.snapshot()
            thread = threading.Thread(
                target=self._run, args=(config, progress, cancel), name=f"order-fetch-{job_id}", daemon=True
            )
            self._thread = thread
            thread.start()
            return progress.snapshot()

    def _run(self, config: FetchConfig, progress: FetchProgress, cancel: threading.Event) -> None:
        try:
            self._runner(config, progress, cancel)
        except Exception as exc:  # noqa: BLE001
            logger.exception("job=%s RUNNER_CRASH %s", progress.job_id, exc)
            progress.finish(
                FetchResult(source_errors=[_source_error("", f"Сбой получения заказов: {exc}", "job_failed")]),
                "failed",
            )

    def status(self) -> dict | None:
        with self._lock:
            return self._progress.snapshot() if self._progress is not None else None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> bool:
        with self._lock:
            if self._cancel is None or self._thread is None or not self._thread.is_alive():
                return False
            self._cancel.set()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()
