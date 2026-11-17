"""Safe, consistent execution boundary for ImageMagick and Ghostscript."""

from __future__ import annotations

import os
import signal
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
    input: str | bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run an argv-only command with a deadline and POSIX process isolation."""
    if not command or not all(isinstance(value, str) for value in command):
        raise ValueError("Команда внешнего инструмента должна быть списком строк.")
    resolved_timeout = tool_timeout_seconds() if timeout is None else timeout
    try:
        if os.name != "posix":
            return subprocess.run(
                list(command),
                timeout=resolved_timeout,
                input=input,
                capture_output=capture_output,
                check=check,
                **kwargs,
            )

        if input is not None:
            if kwargs.get("stdin") is not None:
                raise ValueError("stdin and input arguments may not both be used.")
            kwargs["stdin"] = subprocess.PIPE
        if capture_output:
            if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
                raise ValueError(
                    "stdout and stderr arguments may not be used with capture_output."
                )
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE

        with subprocess.Popen(list(command), start_new_session=True, **kwargs) as process:
            try:
                stdout, stderr = process.communicate(input, timeout=resolved_timeout)
            except subprocess.TimeoutExpired:
                # Signal the group even if its leader exited while descendants
                # were still holding the output pipes open.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            except BaseException:
                # Match subprocess.run: do not wait for an interrupted tool to finish.
                process.kill()
                raise

            result = subprocess.CompletedProcess(process.args, process.poll(), stdout, stderr)
            if check:
                result.check_returncode()
            return result
    except subprocess.TimeoutExpired as exc:
        name = os.path.basename(command[0])
        raise ExternalToolError(
            f"{name} превысил лимит выполнения {resolved_timeout:g} с"
        ) from exc
