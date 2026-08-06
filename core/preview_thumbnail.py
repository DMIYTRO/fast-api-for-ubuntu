"""Persistent, web-sized derivatives for generated order previews."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import shutil
from uuid import uuid4

from core.tool_runner import run_command


THUMBNAIL_MAX_PIXELS = 480


def history_thumbnail_path(preview: Path, *, run_root: Path) -> Path:
    """Return a cache path inside the run, without exposing source path names."""
    resolved_preview = preview.resolve()
    resolved_root = run_root.resolve()
    try:
        relative = resolved_preview.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Превью находится за пределами папки запуска.") from exc
    digest = sha256(str(relative).encode("utf-8")).hexdigest()
    return resolved_root / "output_report" / "history_thumbnails" / f"{digest}.webp"


def get_history_thumbnail(preview: Path, *, run_root: Path) -> Path:
    """Create a thumbnail once and reuse it while its source is unchanged.

    The cache deliberately lives under ``output_report`` instead of next to a
    preview, so opening History never changes an operator-managed preview file
    or its directory.
    """
    source = preview.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    target = history_thumbnail_path(source, run_root=run_root)
    if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target

    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("Утилита ImageMagick (`magick`) не найдена в системе.")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{os.getpid()}.{uuid4().hex}.webp")
    try:
        run_command(
            [
                magick_cmd,
                str(source),
                "-auto-orient",
                "-thumbnail",
                f"{THUMBNAIL_MAX_PIXELS}x{THUMBNAIL_MAX_PIXELS}>",
                "-strip",
                "-quality",
                "82",
                str(temporary),
            ],
            check=True,
        )
        if not temporary.is_file():
            raise RuntimeError("ImageMagick не создал миниатюру превью.")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
