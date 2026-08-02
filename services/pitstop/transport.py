"""Transport boundary and an SSH implementation for PitStop Server."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class PitStopTransportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TransportResult:
    returncode: int
    stdout: str
    stderr: str


class PitStopTransport(Protocol):
    def execute(self, remote_command: str, *, timeout_seconds: float) -> TransportResult:
        ...


@dataclass(frozen=True, slots=True)
class SSHSettings:
    host: str
    username: str
    known_hosts_file: Path
    port: int = 22
    identity_file: Path | None = None
    connect_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        if not self.host or not self.username:
            raise ValueError("SSH host и username обязательны")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port должен быть в диапазоне 1..65535")
        if self.connect_timeout_seconds < 1:
            raise ValueError("SSH connect timeout должен быть положительным")


class SSHTransport:
    """Run a non-interactive SSH command with strict host verification."""

    def __init__(self, settings: SSHSettings) -> None:
        self._settings = settings

    def execute(self, remote_command: str, *, timeout_seconds: float) -> TransportResult:
        command = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self._settings.known_hosts_file}",
            "-o", f"ConnectTimeout={self._settings.connect_timeout_seconds}",
            "-p", str(self._settings.port),
        ]
        if self._settings.identity_file is not None:
            command.extend(["-i", str(self._settings.identity_file)])
        command.extend(
            [f"{self._settings.username}@{self._settings.host}", remote_command]
        )
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise PitStopTransportError("PitStop Server не ответил за отведённое время") from exc
        except OSError as exc:
            raise PitStopTransportError("не удалось запустить SSH-клиент PitStop") from exc
        return TransportResult(completed.returncode, completed.stdout, completed.stderr)
