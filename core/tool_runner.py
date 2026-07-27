"""Safe, consistent execution boundary for ImageMagick and Ghostscript."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import Any


DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0


class ExternalToolError(RuntimeError):
    pass


def tool_timeout_seconds() -> float:
    value = os.environ.get("IMAGE_MAGIC_TOOL_TIMEOUT_SECONDS")
    if value is None:
        return DEFAULT_TOOL_TIMEOUT_SECONDS
    try:
        return max(1.0, float(value))
    except ValueError as exc:
        raise ExternalToolError(
            "IMAGE_MAGIC_TOOL_TIMEOUT_SECONDS должен быть числом"
        ) from exc


def run_command(
    command: Sequence[str],
    *,
    timeout: float | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run an argv-only command with a deadline and isolated process group."""
    if not command or not all(isinstance(value, str) for value in command):
        raise ValueError("Команда внешнего инструмента должна быть списком строк.")
    resolved_timeout = tool_timeout_seconds() if timeout is None else timeout
    try:
        return subprocess.run(
            list(command),
            timeout=resolved_timeout,
            start_new_session=True,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        name = os.path.basename(command[0])
        raise ExternalToolError(
            f"{name} превысил лимит выполнения {resolved_timeout:g} с"
        ) from exc
