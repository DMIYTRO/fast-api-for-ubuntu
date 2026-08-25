"""Locate generated previews used in Sborka rework requests.

This module selects an existing preview name only. Rendering and uploading the
file to Sborka's ``inbox/press/`` will be added later.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Iterable
import shutil
import subprocess

from core.tool_runner import ExternalToolError, run_command


class ReturnPreviewNotFoundError(RuntimeError):
    """No unambiguous, generated preview could be found for an order."""


PROCESSED_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Processed"
RETURN_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Return"
CUSTOM_PREVIEWS_RELATIVE_PATH = Path("Previews") / "Custom"


def custom_return_preview_path(order_id: str, *, input_path: Path) -> Path | None:
    """Return an operator-provided preview, which always wins over generated ones."""
    normalized_order_id = str(order_id).strip()
    if not normalized_order_id or Path(normalized_order_id).name != normalized_order_id:
        raise ValueError("Номер заказа не должен быть пустым или содержать путь.")
    directory = Path(input_path) / CUSTOM_PREVIEWS_RELATIVE_PATH
    for suffix in (".png", ".jpg"):
        candidate = directory / f"{normalized_order_id}_return-preview{suffix}"
        if candidate.is_file():
            return candidate
    return None


class ReturnPreviewCollageError(RuntimeError):
    """A face/back return preview could not be rendered."""


def create_return_preview_collage(
    order_id: str,
    *,
    input_path: Path,
    face_preview_path: Path,
    back_preview_path: Path,
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
    )


def create_return_preview_sheet(
    order_id: str,
    *,
    input_path: Path,
    preview_paths: Iterable[Path],
) -> Path:
    """Create one deterministic Sborka preview from ordered pages/sides."""
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
    output_path = output_dir / f"{normalized_order_id}_return-preview.png"

    # Parenthesized image sequences keep each side independent before appending.
    # -resize x1000 changes only height, so the original aspect ratio is retained.
    def side_arguments(path: Path) -> list[str]:
        return [
            "(",
            str(path),
            "-auto-orient",
            "-resize",
            "x1000",
            ")",
        ]

    command = [
        magick,
        *(argument for path in previews for argument in side_arguments(path)),
        "-background",
        "white",
        "+smush",
        "28",
        str(output_path),
    ]
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
    for preview_path in preview_paths or ():
        path = Path(str(preview_path))
        if path.is_file():
            candidates[path.name] = path
        elif (preview_dir / path.name).is_file():
            candidates[path.name] = preview_dir / path.name

    normalized_files = list(files or [])
    sides: dict[str, str] = {}
    for item in normalized_files:
        parsed = item.get("parsed") or {}
        side = str(parsed.get("side") or "").lower()
        source_path = item.get("path")
        if side in {"face", "back"} and source_path:
            expected_name = f"{Path(str(source_path)).stem}_preview.png"
            sides[side] = expected_name
            processed_path = preview_dir / expected_name
            if processed_path.is_file():
                candidates[expected_name] = processed_path

    if preview_dir.is_dir():
        order_marker = f"-{str(order_id).strip()})"
        for item in preview_dir.iterdir():
            if (
                item.is_file()
                and item.name.endswith("_preview.png")
                and order_marker in item.name
            ):
                candidates.setdefault(item.name, item)

    if {"face", "back"}.issubset(sides):
        face_path = candidates.get(sides["face"])
        back_path = candidates.get(sides["back"])
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
        if (match := re.search(r"(?:_|-)(face|back)_preview\.png$", filename, re.IGNORECASE))
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
        raise ReturnPreviewNotFoundError(
            f"Не найдено сформированное превью для заказа №{order_id} в {preview_dir}"
        )
    ordered = [candidates[name] for name in sorted(candidates, key=_preview_sort_key)]
    return create_return_preview_sheet(
        order_id,
        input_path=input_path,
        preview_paths=ordered,
    ).name


def _preview_sort_key(filename: str) -> tuple[int, str]:
    match = re.search(r"(?:page|стр(?:аница)?)[_-]?(\d+)", filename, re.IGNORECASE)
    return (int(match.group(1)) if match else 0, filename.lower())
