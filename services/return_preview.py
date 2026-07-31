"""Preparation point for Sborka previews used in rework requests.

This is deliberately a stub: it only chooses the preview filename.  Rendering
and uploading the file to Sborka's ``inbox/press/`` will be added here later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


DEFAULT_EMPLOYEE_ID = "20"


def prepare_return_preview_name(
    order_id: str, preview_paths: Iterable[str] | None = None
) -> str:
    """Return a preview filename without creating or uploading the file."""
    for preview_path in preview_paths or ():
        filename = Path(str(preview_path)).name
        if filename:
            return filename
    return f"{str(order_id).strip()}_{DEFAULT_EMPLOYEE_ID}.png"
