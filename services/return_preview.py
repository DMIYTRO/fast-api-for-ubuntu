"""Locate generated previews used in Sborka rework requests.

This module selects an existing preview name only. Rendering and uploading the
file to Sborka's ``inbox/press/`` will be added later.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
from typing import Iterable, Iterator
import shutil
import subprocess
from uuid import uuid4

try:
    import fcntl
except ImportError:
    import msvcrt

    fcntl = None

from core.preview_cache import (
    PREVIEW_SUFFIXES,
    default_preview_format,
    jpeg_output_arguments,
    normalize_preview_format,
    preview_extension,
    preview_name_key,
    preview_variants,
)
from core.tool_runner import ExternalToolError, run_command

# Generated previews: historical ``*_preview.png`` and new ``*_preview.jpg``.
PREVIEW_FILE_RE = re.compile(r"_preview\.(?:jpe?g|png)$", re.IGNORECASE)
_SIDE_PREVIEW_RE = re.compile(r"(?:_|-)(face|back)_preview\.(?:jpe?g|png)$", re.IGNORECASE)


class ReturnPreviewNotFoundError(RuntimeError):
    """No unambiguous, generated preview could be found for an order."""


PROCESSED_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Processed"
RETURN_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Return"
CUSTOM_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Custom"


def _custom_return_preview_base(order_id: str, input_path: Path) -> Path:
    normalized_order_id = str(order_id).strip()
    if not normalized_order_id or Path(normalized_order_id).name != normalized_order_id:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")
    return Path(input_path) / CUSTOM_PREVIEWS_RELATIVE_PATH / f"{normalized_order_id}_return-preview"


@contextmanager
def _custom_preview_lock(
    base: Path, *, writing: bool = False, legacy_exists: bool = False
) -> Iterator[bool]:
    lock_path = base.parent / f".{base.name}.lock"
    if writing:
        base.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("ab")
    else:
        try:
            lock = lock_path.open("rb")
        except FileNotFoundError:
            if not legacy_exists:
                yield False
                return
            try:
                lock = lock_path.open("ab")
            except OSError as exc:
                # Previously uploaded previews can live on read-only storage.
                if exc.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
                    raise
                try:
                    lock = lock_path.open("rb")
                except FileNotFoundError:
                    yield True
                    return
    with lock:
        if fcntl is None:
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock, fcntl.LOCK_EX if writing else fcntl.LOCK_SH)
        yield True


@contextmanager
def selected_custom_return_preview(
    order_id: str, *, input_path: Path
) -> Iterator[Path | None]:
    """Keep the selected preview available while a consumer opens or uploads it."""
    base = _custom_return_preview_base(order_id, input_path)
    candidates = [base.parent / f"{base.name}{suffix}" for suffix in (".png", ".jpg")]
    with _custom_preview_lock(
        base, legacy_exists=any(path.is_file() for path in candidates)
    ) as may_have_preview:
        if may_have_preview:
            for candidate in candidates:
                if candidate.is_file():
                    yield candidate
                    return
        yield None


def custom_return_preview_path(order_id: str, *, input_path: Path) -> Path | None:
    """Return an operator-provided preview, which always wins over generated ones."""
    with selected_custom_return_preview(order_id, input_path=input_path) as preview:
        return preview


def publish_custom_return_preview(
    order_id: str, *, input_path: Path, suffix: str, content: bytes | bytearray
) -> Path:
    """Replace an operator preview only after the upload is fully staged."""
    base = _custom_return_preview_base(order_id, input_path)
    target = base.parent / f"{base.name}{suffix}"
    alternate_suffix = ".jpg" if suffix == ".png" else ".png"
    alternate = base.parent / f"{base.name}{alternate_suffix}"
    with _custom_preview_lock(base, writing=True):
        temporary = None
        staged_path = base.parent / f".upload-{uuid4().hex}"
        try:
            # Exclusive creation keeps the file's normal umask and directory defaults.
            with staged_path.open("xb") as staged:
                temporary = staged_path
                staged.write(content)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        alternate.unlink(missing_ok=True)
    return target


class ReturnPreviewCollageError(RuntimeError):
    """A face/back return preview could not be rendered."""


#: White gap between pages of a side-by-side sheet, px.
SIDE_BY_SIDE_GAP_PX = 28


def side_by_side_command(
    magick: str,
    pages: Iterable[Path],
    output_path: Path,
    *,
    height: int | None,
    output_options: Iterable[str] = (),
    captions: Iterable[str] | None = None,
    font: Path | str | None = None,
    caption_bar_px: int = 48,
) -> list[str]:
    """ImageMagick argv that puts pages left to right, top-aligned, white gap between.

    Parenthesized image sequences keep each page independent before appending;
    ``-resize x<height>`` changes only height, so each aspect ratio is retained
    (``height=None`` keeps the pages as rendered). ``captions`` (one per page, e.g.
    «Лицо» / «Оборот») are drawn on a white bar of ``caption_bar_px`` above each
    page only when ``font`` is given: ImageMagick installations do not always have
    a usable font, so callers pass a font file they found.
    """
    page_list = [Path(item) for item in pages]
    labels = list(captions) if captions is not None and font else [None] * len(page_list)
    bar = max(24, int(caption_bar_px))
    pointsize = max(14, int(bar * 0.62))

    def page_arguments(path: Path, caption: str | None) -> list[str]:
        arguments = ["(", str(path), "-auto-orient"]
        if height is not None:
            arguments += ["-resize", f"x{int(height)}"]
        if caption:
            arguments += [
                "-background", "white", "-gravity", "north", "-splice", f"0x{bar}",
                "-font", str(font), "-pointsize", str(pointsize), "-fill", "#1f2937",
                "-annotate", f"+0+{max(2, (bar - pointsize) // 2)}", caption, "+gravity",
            ]
        return [*arguments, ")"]

    return [
        magick,
        *(
            argument
            for path, caption in zip(page_list, labels)
            for argument in page_arguments(path, caption)
        ),
        "-background",
        "white",
        "+smush",
        str(SIDE_BY_SIDE_GAP_PX),
        *output_options,
        str(output_path),
    ]


def create_return_preview_collage(
    order_id: str,
    *,
    input_path: Path,
    face_preview_path: Path,
    back_preview_path: Path,
    output_format: str | None = None,
) -> Path:
    """Create the upload preview for a two-sided order.

    ``face_preview_path`` and ``back_preview_path`` must be the already selected
    real preview files; this function deliberately does not search for or infer
    their names.  The face is always placed left of the back.  Images are scaled
    to the same height while keeping their aspect ratio, with a white gap.  The
    source filenames are already unambiguous (``face`` / ``back``); text labels
    are intentionally omitted because ImageMagick installations do not always
    include a usable font.
    """
    normalized_order_id = str(order_id).strip()
    if not normalized_order_id or Path(normalized_order_id).name != normalized_order_id:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")

    return create_return_preview_sheet(
        order_id,
        input_path=input_path,
        preview_paths=(face_preview_path, back_preview_path),
        output_format=output_format,
    )


def create_return_preview_sheet(
    order_id: str,
    *,
    input_path: Path,
    preview_paths: Iterable[Path],
    output_format: str | None = None,
) -> Path:
    """Create one deterministic Sborka preview from ordered pages/sides.

    Inputs may be historical PNG or new JPEG previews in any mix.  The collage
    is written in ``output_format`` (``'jpeg'`` | ``'png'``; default
    ``IMAGE_MAGIC_PREVIEW_FORMAT``, PNG unless enabled) as
    ``<order>_return-preview.<jpg|png>``.
    """
    normalized_order_id = str(order_id).strip()
    if not normalized_order_id or Path(normalized_order_id).name != normalized_order_id:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")

    previews = [Path(item) for item in preview_paths]
    if not previews:
        raise ReturnPreviewCollageError("Не переданы файлы превью для коллажа.")
    missing = [str(item) for item in previews if not item.is_file()]
    if missing:
        raise ReturnPreviewCollageError(
            "Не найдены файлы превью для коллажа: " + ", ".join(missing)
        )

    magick = shutil.which("magick")
    if not magick:
        raise ReturnPreviewCollageError("Не найдена утилита ImageMagick (`magick`).")

    output_dir = Path(input_path) / RETURN_PREVIEWS_RELATIVE_PATH
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_format = normalize_preview_format(output_format)
    output_path = output_dir / (
        f"{normalized_order_id}_return-preview{preview_extension(resolved_format)}"
    )
    output_options = jpeg_output_arguments() if resolved_format == "jpeg" else []
    command = side_by_side_command(
        magick, previews, output_path, height=1000, output_options=output_options
    )
    try:
        run_command(command, check=True, capture_output=True)
    except (ExternalToolError, OSError, subprocess.CalledProcessError) as exc:
        # Callers receive a domain-specific error instead of an ImageMagick
        # traceback when rendering fails.
        raise ReturnPreviewCollageError(
            f"Не удалось сформировать коллаж превью для заказа №{normalized_order_id}."
        ) from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ReturnPreviewCollageError(
            f"ImageMagick не создал коллаж превью: {output_path}"
        )
    return output_path


def prepare_return_preview_name(
    order_id: str,
    *,
    input_path: Path,
    preview_paths: Iterable[str] | None = None,
    files: Iterable[dict] | None = None,
) -> str:
    """Select one preview or create a face/back collage for return to Sborka."""
    custom_preview = custom_return_preview_path(order_id, input_path=input_path)
    if custom_preview is not None:
        return custom_preview.name
    preview_dir = input_path / PROCESSED_PREVIEWS_RELATIVE_PATH
    candidates: dict[str, Path] = {}
    # Names forced by a layered composite; they win over PNG/JPEG twins.
    locked: set[str] = set()
    for preview_path in preview_paths or ():
        path = Path(str(preview_path))
        if path.is_file():
            _add_candidate(candidates, locked, path)
        elif (preview_dir / path.name).is_file():
            _add_candidate(candidates, locked, preview_dir / path.name)

    normalized_files = list(files or [])
    sides: dict[str, str] = {}
    generated_preview_dir = input_path / "Previews"
    for item in normalized_files:
        parsed = item.get("parsed") or {}
        side = str(parsed.get("side") or item.get("side") or "").lower()
        source_path = item.get("path")
        if side in {"face", "back"} and source_path:
            source_stem = Path(str(source_path)).stem
            # Prefer whichever generated variant (JPEG or historical PNG) is
            # newest; fall back to the name of the currently configured format.
            variants = preview_variants(preview_dir / f"{source_stem}_preview")
            expected_name = (
                variants[0].name
                if variants
                else f"{source_stem}_preview{preview_extension(default_preview_format())}"
            )
            sides[side] = expected_name
            if variants:
                _add_candidate(candidates, locked, variants[0])
            # Layered TIFF orders already have a rendered composite preview
            # alongside their experimental PDF. Use that image for a rework
            # request if the ordinary batch preview has gone missing.
            layered_variants = preview_variants(
                generated_preview_dir / f"{source_stem}_layered-composite"
            )
            if item.get("has_unflattened_layers") and layered_variants:
                # Keep the historical contract: the entry is addressed by the
                # expected ``*_preview.*`` name.
                for name in [n for n in candidates if preview_name_key(n) == preview_name_key(expected_name)]:
                    del candidates[name]
                candidates[expected_name] = layered_variants[0]
                locked.add(expected_name)

    if preview_dir.is_dir():
        order_marker = f"-{str(order_id).strip()})"
        for item in sorted(preview_dir.iterdir()):
            if (
                item.is_file()
                and PREVIEW_FILE_RE.search(item.name)
                and order_marker in item.name
            ):
                _add_candidate(candidates, locked, item)

    missing_side = _missing_duplex_side(normalized_files, sides)
    if missing_side is not None:
        present_side = "back" if missing_side == "face" else "face"
        present_path = _find_candidate(candidates, sides[present_side])
        if present_path is not None:
            # Двусторонний заказ без одной стороны (заказ 26262655): в Сборку уходит
            # пара с пустым листом на месте недостающей стороны, а не одна сторона,
            # которую легко принять за лицо.
            blank = create_missing_side_preview(
                order_id, input_path=input_path, like=present_path, side=missing_side
            )
            face_path, back_path = (
                (blank, present_path) if missing_side == "face" else (present_path, blank)
            )
            return create_return_preview_collage(
                order_id,
                input_path=input_path,
                face_preview_path=face_path,
                back_preview_path=back_path,
            ).name

    if {"face", "back"}.issubset(sides):
        face_path = _find_candidate(candidates, sides["face"])
        back_path = _find_candidate(candidates, sides["back"])
        if not face_path or not back_path:
            raise ReturnPreviewNotFoundError(
                f"Не найдены превью face/back для заказа №{order_id}."
            )
        return create_return_preview_collage(
            order_id,
            input_path=input_path,
            face_preview_path=face_path,
            back_preview_path=back_path,
        ).name

    side_by_filename = {
        match.group(1).lower(): path
        for filename, path in candidates.items()
        if (match := _SIDE_PREVIEW_RE.search(filename))
    }
    if set(side_by_filename) == {"face", "back"} and len(candidates) == 2:
        return create_return_preview_collage(
            order_id,
            input_path=input_path,
            face_preview_path=side_by_filename["face"],
            back_preview_path=side_by_filename["back"],
        ).name

    if len(candidates) == 1:
        return next(iter(candidates))
    if not candidates:
        # Some legacy/one-sided actions are allowed to return without an
        # attached preview. Keep that behavior when no face/back sides were
        # identified; a two-sided order still fails clearly above.
        if not sides:
            return ""
        raise ReturnPreviewNotFoundError(
            f"Не найдено сформированное превью для заказа №{order_id} в {preview_dir}"
        )
    ordered = [candidates[name] for name in sorted(candidates, key=_preview_sort_key)]
    return create_return_preview_sheet(
        order_id,
        input_path=input_path,
        preview_paths=ordered,
    ).name


def _missing_duplex_side(files: list[dict], sides: dict[str, str]) -> str | None:
    """Недостающая сторона двустороннего заказа (``face``/``back``) или ``None``."""
    if len(sides) != 1:
        return None
    duplex = False
    for item in files:
        parsed = item.get("parsed") or {}
        try:
            duplex = duplex or int(parsed.get("back_colors") or 0) > 0
        except (TypeError, ValueError):
            continue
    if not duplex:
        return None
    return "back" if "face" in sides else "face"


def create_missing_side_preview(
    order_id: str, *, input_path: Path, like: Path, side: str
) -> Path:
    """Пустой лист размером с превью ``like``: серая рамка и диагональный крест.

    Без надписи: шрифт в ImageMagick есть не везде; сторону задаёт место в коллаже.
    """
    normalized_order_id = str(order_id).strip()
    if not normalized_order_id or Path(normalized_order_id).name != normalized_order_id:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")
    magick = shutil.which("magick")
    if not magick:
        raise ReturnPreviewCollageError("Не найдена утилита ImageMagick (`magick`).")
    output_dir = Path(input_path) / RETURN_PREVIEWS_RELATIVE_PATH
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{normalized_order_id}_missing-{side}.png"
    command = [
        magick, str(like), "-auto-orient", "-alpha", "off",
        "-fill", "white", "-colorize", "100",
        "-fill", "none", "-stroke", "#9ca3af",
        "-strokewidth", "8",
        "-draw", "rectangle 0,0 %[fx:w-1],%[fx:h-1]",
        "-draw", "line 0,0 %[fx:w],%[fx:h]",
        "-draw", "line %[fx:w],0 0,%[fx:h]",
        str(output_path),
    ]
    try:
        run_command(command, check=True, capture_output=True)
    except (ExternalToolError, OSError, subprocess.CalledProcessError) as exc:
        raise ReturnPreviewCollageError(
            f"Не удалось нарисовать пустую сторону {side} для заказа №{normalized_order_id}."
        ) from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ReturnPreviewCollageError(f"ImageMagick не создал пустую сторону: {output_path}")
    return output_path


def _add_candidate(candidates: dict[str, Path], locked: set[str], path: Path) -> None:
    """Register a preview by name, collapsing PNG/JPEG twins of one preview.

    When both ``x_preview.png`` and ``x_preview.jpg`` exist (for example after a
    re-check with another format) only the newest is kept; ties prefer JPEG.
    Entries in ``locked`` are never replaced.
    """
    key = preview_name_key(path.name)
    twins = [name for name in candidates if preview_name_key(name) == key]
    if any(name in locked for name in twins):
        return
    if twins:
        current = candidates[twins[0]]
        if _freshness(path) <= _freshness(current):
            return
        for name in twins:
            del candidates[name]
    candidates[path.name] = path


def _find_candidate(candidates: dict[str, Path], name: str) -> Path | None:
    """Look a preview up by name, accepting its PNG/JPEG twin."""
    if name in candidates:
        return candidates[name]
    key = preview_name_key(name)
    return next((path for item, path in candidates.items() if preview_name_key(item) == key), None)


def _freshness(path: Path) -> tuple[int, int]:
    suffix = path.suffix.lower()
    priority = -PREVIEW_SUFFIXES.index(suffix) if suffix in PREVIEW_SUFFIXES else -len(PREVIEW_SUFFIXES)
    try:
        return (path.stat().st_mtime_ns, priority)
    except OSError:
        return (0, priority)


def _preview_sort_key(filename: str) -> tuple[int, str]:
    match = re.search(r"(?:page|стр(?:аница)?)[_-]?(\d+)", filename, re.IGNORECASE)
    return (int(match.group(1)) if match else 0, filename.lower())
