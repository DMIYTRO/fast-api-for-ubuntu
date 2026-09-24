"""External-share storage helpers for generated preview renditions."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path


logger = logging.getLogger("image_magic.preview")


def _is_external_mount(path: Path) -> bool:
    """Check that the configured production input tree is backed by a mount."""
    current = path.resolve()
    while current != current.parent:
        if os.path.ismount(current):
            return True
        current = current.parent
    return os.path.ismount(current)


def preview_cache_root(input_hint: Path | None = None) -> Path:
    """Return a cache directory on the configured input share.

    An explicitly configured cache outside IMAGE_MAGIC_INPUT_DIR is ignored
    with a warning; the fallback remains inside the mounted external share.
    """
    input_value = os.environ.get("IMAGE_MAGIC_INPUT_DIR")
    if input_value:
        input_root = Path(input_value).expanduser().resolve()
    elif input_hint is not None:
        # Development/CLI runs without a configured service input root keep
        # cache beside the selected input folder, never in the OS temp dir.
        input_root = input_hint.resolve().parent.parent
    else:
        raise RuntimeError("IMAGE_MAGIC_INPUT_DIR must point to the external share")
    # A configured production root must actually be mounted; otherwise its
    # mountpoint directory could silently write to the system volume.
    if input_value and not _is_external_mount(input_root):
        raise RuntimeError(
            f"Внешний диск Share недоступен: {input_root} не находится на смонтированном томе."
        )
    configured = os.environ.get("IMAGE_MAGIC_PREVIEW_CACHE_DIR")
    cache_root = input_root / ".preview-cache"
    if configured:
        candidate = Path(configured).expanduser().resolve()
        try:
            candidate.relative_to(input_root)
        except ValueError:
            logger.warning(
                "preview.cache_path_outside_share configured=%s fallback=%s",
                candidate,
                cache_root,
            )
        else:
            cache_root = candidate
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def full_preview_cache_path(thumbnail_path: Path) -> Path:
    """Stable full rendition path for a stored thumbnail across page refreshes."""
    identity = hashlib.sha256(str(thumbnail_path.resolve()).encode("utf-8")).hexdigest()
    return preview_cache_root(thumbnail_path) / identity[:2] / f"{identity}.png"


def preview_work_dir(preview_path: Path) -> Path:
    """Use the external cache for work files belonging to shared input data."""
    input_value = os.environ.get("IMAGE_MAGIC_INPUT_DIR")
    if input_value:
        share = Path(input_value).expanduser().resolve()
        try:
            preview_path.resolve().relative_to(share)
        except ValueError as exc:
            raise RuntimeError(
                "Preview output is outside IMAGE_MAGIC_INPUT_DIR; refusing "
                "to put temporary render data on the local disk"
            ) from exc
    work_root = preview_cache_root(preview_path) / ".work"
    work_root.mkdir(parents=True, exist_ok=True)
    return work_root
