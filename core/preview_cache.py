"""External-share storage helpers for generated preview renditions."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path


logger = logging.getLogger("image_magic.preview")

# --- Preview image format -------------------------------------------------
# New previews can be written as PNG (historical default) or JPEG.  Historical
# PNG files remain readable everywhere: consumers must look for every suffix in
# ``PREVIEW_SUFFIXES`` instead of assuming ``.png``.
PREVIEW_FORMAT_ENV = "IMAGE_MAGIC_PREVIEW_FORMAT"
PREVIEW_JPEG_QUALITY_ENV = "IMAGE_MAGIC_PREVIEW_JPEG_QUALITY"
DEFAULT_PREVIEW_FORMAT = "png"
DEFAULT_JPEG_QUALITY = 95
JPEG_SAMPLING_FACTOR = "1x1"  # 4:4:4, no chroma subsampling
JPEG_BACKGROUND = "white"  # explicit backdrop for transparent pages
# Order is the preference for ties between variants of one preview.
PREVIEW_SUFFIXES = (".jpg", ".jpeg", ".png")
_FORMAT_ALIASES = {"jpeg": "jpeg", "jpg": "jpeg", "png": "png"}
_EXTENSIONS = {"jpeg": ".jpg", "png": ".png"}
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def normalize_preview_format(value: str | None) -> str:
    """Return ``'jpeg'`` or ``'png'``; ``None``/empty means the configured default."""
    if value is None or not str(value).strip():
        return default_preview_format()
    try:
        return _FORMAT_ALIASES[str(value).strip().lower().lstrip(".")]
    except KeyError:
        raise ValueError(
            f"Неизвестный формат превью: {value!r}. Допустимо: 'jpeg' или 'png'."
        ) from None


def default_preview_format() -> str:
    """Format for newly written previews: ``IMAGE_MAGIC_PREVIEW_FORMAT`` or PNG."""
    raw = os.environ.get(PREVIEW_FORMAT_ENV, "").strip()
    if not raw:
        return DEFAULT_PREVIEW_FORMAT
    try:
        return normalize_preview_format(raw)
    except ValueError:
        logger.warning("preview.invalid_format value=%s fallback=%s", raw, DEFAULT_PREVIEW_FORMAT)
        return DEFAULT_PREVIEW_FORMAT


def preview_extension(preview_format: str | None = None) -> str:
    """File extension (``.jpg`` / ``.png``) for a format or the configured default."""
    return _EXTENSIONS[normalize_preview_format(preview_format)]


def preview_format_for_path(path: str | Path) -> str | None:
    """Format implied by a file suffix, or ``None`` when it is not a preview suffix."""
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "jpeg"
    if suffix == ".png":
        return "png"
    return None


def preview_jpeg_quality() -> int:
    """JPEG quality: ``IMAGE_MAGIC_PREVIEW_JPEG_QUALITY`` (1-100) or 95."""
    raw = os.environ.get(PREVIEW_JPEG_QUALITY_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_JPEG_QUALITY
    except ValueError:
        logger.warning("preview.invalid_jpeg_quality value=%s", raw)
        return DEFAULT_JPEG_QUALITY
    return value if 1 <= value <= 100 else DEFAULT_JPEG_QUALITY


def jpeg_output_arguments(quality: int | None = None) -> list[str]:
    """ImageMagick options placed just before a JPEG output file.

    Transparency is flattened onto an explicit white background (JPEG has no
    alpha), chroma subsampling is disabled (4:4:4) and the pixels are written
    as sRGB, because a CMYK JPEG is not shown correctly by browsers (a PNG
    output was converted to RGB implicitly as well).  Source files are untouched.
    """
    return [
        "-background", JPEG_BACKGROUND,
        "-alpha", "remove",
        "-alpha", "off",
        "-colorspace", "sRGB",
        "-sampling-factor", JPEG_SAMPLING_FACTOR,
        "-quality", str(quality if quality is not None else preview_jpeg_quality()),
    ]


def with_preview_extension(path: str | Path, preview_format: str | None = None) -> Path:
    """Replace the suffix of ``path`` with the extension of the chosen format."""
    return Path(path).with_suffix(preview_extension(preview_format))


def is_preview_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in PREVIEW_SUFFIXES


def preview_media_type(path: str | Path) -> str:
    """MIME type of a stored preview rendition (historical PNG or new JPEG)."""
    return _MEDIA_TYPES.get(Path(path).suffix.lower(), "image/png")


def preview_name_key(name: str) -> str:
    """File name without a preview image suffix, to match PNG/JPEG variants."""
    path = Path(name)
    return path.with_suffix("").name if path.suffix.lower() in PREVIEW_SUFFIXES else path.name


def preview_variants(path: str | Path) -> list[Path]:
    """Existing files with the same stem and any preview suffix, newest first."""
    base = Path(path)
    found = [
        base.with_suffix(suffix)
        for suffix in PREVIEW_SUFFIXES
        if base.with_suffix(suffix).is_file()
    ]
    return sorted(
        found,
        key=lambda item: (item.stat().st_mtime_ns, -PREVIEW_SUFFIXES.index(item.suffix.lower())),
        reverse=True,
    )


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
    """Stable full rendition path for a stored thumbnail across page refreshes.

    The key is the thumbnail's absolute path and the extension follows the
    thumbnail's format (``.jpg`` for a JPEG thumbnail, ``.png`` otherwise), so
    historical PNG thumbnails keep resolving to their existing ``.png`` cache.
    """
    identity = hashlib.sha256(str(thumbnail_path.resolve()).encode("utf-8")).hexdigest()
    extension = ".jpg" if preview_format_for_path(thumbnail_path) == "jpeg" else ".png"
    return preview_cache_root(thumbnail_path) / identity[:2] / f"{identity}{extension}"


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
