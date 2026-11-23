"""Временные рабочие папки на общем SMB-диске (``/mnt/shared``).

На CIFS удаление папки иногда падает с ``[Errno 39] Directory not empty``, хотя все
файлы в ней уже удалены: файл, который ещё держит открытым другой клиент общего
диска (сервер callas, PitStop, превью), остаётся «удаляемым» на сервере, пока тот
не закроет его. Через секунду-другую папка пустеет.

Уборка временной папки не должна ронять результат: итоговый PDF к этому моменту уже
опубликован. Поэтому ``share_temp_dir`` повторяет удаление с паузами, а если папка так
и не удалилась, пишет предупреждение и не бросает исключение. Остатки потом убирает
``sweep_stale_temp_dirs``.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

logger = logging.getLogger("image_magic.share_tempdir")

#: Паузы между повторными попытками удаления, с (всего попыток — на одну больше).
CLEANUP_PAUSES_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)
#: Брошенная временная папка старше этого возраста удаляется при следующей сборке PDF.
STALE_AFTER_SECONDS = 3600.0
#: Скрытые временные папки Image Magic в папках PDF: ``.<заказ>_…``, ``.<заказ>_diag_…``,
#: ``.raster-pdf-…``, ``.callas-render-…``, ``.pdf-route-…``, ``.revision-…``.
STALE_NAME_RE = re.compile(r"^\.(?:\d+_|raster-pdf-|callas-render-|pdf-route-|revision-|preview_|forced-build-)")


def _rmtree(path: Path) -> None:
    """Точка подмены в тестах (без подмены ``shutil.rmtree`` для всего процесса)."""
    shutil.rmtree(path)


def remove_tree_quietly(
    path: Path | str, pauses: Sequence[float] = CLEANUP_PAUSES_SECONDS
) -> bool:
    """Удалить папку с повторами; ``False`` — не удалось (записано предупреждение)."""
    target = Path(path)
    last_error: OSError | None = None
    for attempt in range(len(pauses) + 1):
        try:
            _rmtree(target)
            if attempt:
                logger.info("share_tempdir.cleanup_retried path=%s attempts=%d", target, attempt + 1)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            last_error = exc
            if attempt < len(pauses):
                time.sleep(pauses[attempt])
    logger.warning(
        "share_tempdir.cleanup_deferred path=%s attempts=%d error=%s",
        target, len(pauses) + 1, last_error,
    )
    return False


@contextmanager
def share_temp_dir(
    prefix: str, dir: Path | str, pauses: Sequence[float] = CLEANUP_PAUSES_SECONDS
) -> Iterator[str]:
    """Как ``tempfile.TemporaryDirectory``, но ошибка уборки не выходит наружу.

    Исключение из тела блока по-прежнему пробрасывается.
    """
    path = tempfile.mkdtemp(prefix=prefix, dir=dir)
    try:
        yield path
    finally:
        remove_tree_quietly(path, pauses)


def sweep_stale_temp_dirs(
    root: Path | str,
    max_age_seconds: float = STALE_AFTER_SECONDS,
    now: float | None = None,
) -> list[Path]:
    """Удалить брошенные временные папки Image Magic в ``root`` (без вложенных папок).

    Трогает только скрытые папки с известными префиксами (``STALE_NAME_RE``) и
    старше ``max_age_seconds``; файлы и чужие папки не трогает. Ошибки не бросает.
    """
    base = Path(root)
    current = time.time() if now is None else now
    removed: list[Path] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return removed
    for entry in entries:
        try:
            if not STALE_NAME_RE.match(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            if current - entry.stat().st_mtime < max_age_seconds:
                continue
        except OSError:
            continue
        if remove_tree_quietly(entry, pauses=()):
            removed.append(entry)
            logger.info("share_tempdir.stale_removed path=%s", entry)
    return removed
