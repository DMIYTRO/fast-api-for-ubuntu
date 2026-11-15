"""Report-only PitStop check orchestration."""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Callable
from uuid import uuid4

from .catalog import PitStopProfile, PitStopProfileCatalog
from .concurrency import (
    DEGRADE_COOLDOWN_SECONDS_DEFAULT,
    DEGRADE_THRESHOLD_DEFAULT,
    DEGRADE_WINDOW_SECONDS_DEFAULT,
    MAX_CONCURRENT_MAX,
    MAX_CONCURRENT_MIN,
    GateConfig,
    PitStopGate,
    max_concurrent_from_env,
    shared_gate_for,
)
from .failures import FailureKind, classify_cli_exit
from .models import PitStopCheckResult, PitStopExecutionStatus
from .parser import PitStopReportError, PitStopReportUnreadableError, parse_pitstop_report
from .paths import SharedPathError, mac_shared_path_to_windows
from .transport import PitStopTransport, PitStopTransportError


logger = logging.getLogger(__name__)

_MAX_ERROR_LENGTH = 1000
_MAX_TRANSIENT_RETRIES = 3


@dataclass(frozen=True, slots=True)
class PitStopServiceSettings:
    cli_path: PureWindowsPath
    mac_shared_root: Path
    windows_shared_root: PureWindowsPath
    report_root: Path
    command_timeout_seconds: float = 180.0
    # How many PitStopServerCLI jobs may run at once (IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT).
    # None: read the variable from the environment (default 4); 1 is strictly serial.
    max_concurrent: int | None = None
    # Exclusive repeats of a transient failure; 0 disables retries.
    transient_retries: int = 1
    # Pause before an exclusive retry, so the server can settle.
    retry_pause_seconds: float = 1.0
    # Automatic fallback to serial mode (see concurrency.py).
    degrade_threshold: int = DEGRADE_THRESHOLD_DEFAULT
    degrade_window_seconds: float = DEGRADE_WINDOW_SECONDS_DEFAULT
    degrade_cooldown_seconds: float = DEGRADE_COOLDOWN_SECONDS_DEFAULT

    def __post_init__(self) -> None:
        if not self.cli_path.is_absolute():
            raise ValueError("путь к PitStopServerCLI.exe должен быть абсолютным")
        if self.command_timeout_seconds <= 0:
            raise ValueError("таймаут PitStop должен быть положительным")
        if self.max_concurrent is not None and not (
            MAX_CONCURRENT_MIN <= self.max_concurrent <= MAX_CONCURRENT_MAX
        ):
            raise ValueError(
                "число параллельных проверок PitStop должно быть от "
                f"{MAX_CONCURRENT_MIN} до {MAX_CONCURRENT_MAX}"
            )
        if not 0 <= self.transient_retries <= _MAX_TRANSIENT_RETRIES:
            raise ValueError(
                f"число повторов PitStop должно быть от 0 до {_MAX_TRANSIENT_RETRIES}"
            )
        if self.retry_pause_seconds < 0:
            raise ValueError("пауза перед повтором PitStop не может быть отрицательной")

    def gate_config(self) -> GateConfig:
        return GateConfig(
            max_concurrent=(
                self.max_concurrent
                if self.max_concurrent is not None
                else max_concurrent_from_env()
            ),
            degrade_threshold=self.degrade_threshold,
            degrade_window_seconds=self.degrade_window_seconds,
            degrade_cooldown_seconds=self.degrade_cooldown_seconds,
        )


@dataclass(frozen=True, slots=True)
class _Attempt:
    result: PitStopCheckResult
    kind: FailureKind | None = None  # None: the attempt completed
    overlapped: bool = False
    report_dir: Path | None = None


class PitStopService:
    """Run report-only checks with bounded concurrency and safe retries.

    Concurrency.  Up to ``max_concurrent`` PitStopServerCLI jobs run at once
    (default 4).  On 2026-11-15 the live server was exercised with 144 checks
    at 1-4 parallel jobs (36 started at the same instant): no failures, reports
    identical to the serial baseline, 3.3 s per check serially versus 3.6 s at
    4 jobs.  The old "transient exit code 1 when jobs overlap" did not
    reproduce, but long soak runs, real production traffic and very large PDFs
    were not covered, so the safeguards below stay:

    * a transient failure (CLI exit 1, SSH connection errors, a missing or
      unreadable JSON report after exit 0) is repeated once with no other
      check in flight (``PitStopGate.exclusive``);
    * several such failures of overlapping checks within a short window switch
      the process to serial mode for a cool-down period;
    * ``IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT=1`` reproduces the old strictly
      serial behaviour.

    Thread safety.  The service holds only immutable state.  Every attempt has
    its own ``uuid4`` report directory and report paths; the transport builds
    its command per call.  The gate is shared per transport, because the
    application builds a new service for every run.
    """

    def __init__(
        self,
        *,
        settings: PitStopServiceSettings,
        profiles: PitStopProfileCatalog,
        transport: PitStopTransport,
        gate: PitStopGate | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._transport = transport
        self._gate = gate or shared_gate_for(transport, settings.gate_config())
        self._sleep = sleep

    @property
    def gate(self) -> PitStopGate:
        return self._gate

    def check_pdf(self, input_pdf: Path, *, profile_id: str) -> PitStopCheckResult:
        """Run a profile without an output argument and parse JSON/XML artifacts."""
        input_pdf = input_pdf.expanduser().resolve(strict=False)
        if not input_pdf.is_file() or input_pdf.suffix.lower() != ".pdf":
            raise ValueError(f"PDF для проверки не найден: {input_pdf}")
        profile = self._profiles.get(profile_id)

        first = self._attempt(input_pdf, profile, exclusive=False)
        if first.kind is None:
            return first.result
        reason = first.result.technical_error or "причина не указана"
        if first.kind in (FailureKind.TRANSIENT, FailureKind.TIMEOUT) and first.overlapped:
            self._gate.note_suspect_failure(overlapped=True, reason=reason)
        if first.kind is not FailureKind.TRANSIENT or self._settings.transient_retries < 1:
            return first.result

        attempts = [first]
        for number in range(2, 2 + self._settings.transient_retries):
            logger.info(
                "PitStop: временный сбой проверки %s (попытка %d: %s); "
                "повтор в эксклюзивном режиме без параллельных проверок.",
                input_pdf.name,
                number - 1,
                reason,
            )
            if self._settings.retry_pause_seconds > 0:
                self._sleep(self._settings.retry_pause_seconds)
            attempt = self._attempt(input_pdf, profile, exclusive=True)
            if attempt.kind is None:
                logger.warning(
                    "PitStop: проверка %s выполнена с попытки %d в эксклюзивном "
                    "режиме; первая попытка завершилась временным сбоем: %s",
                    input_pdf.name,
                    number,
                    reason,
                )
                for failed in attempts:
                    if failed.report_dir is not None:
                        self._remove_successful_report_dir(failed.report_dir)
                return attempt.result
            attempts.append(attempt)
            if attempt.kind is not FailureKind.TRANSIENT:
                break

        last = attempts[-1]
        logger.warning(
            "PitStop: проверка %s не удалась после %d попыток (первая: %s; "
            "последняя: %s).",
            input_pdf.name,
            len(attempts),
            reason,
            last.result.technical_error,
        )
        message = (
            f"Проверка PitStop не удалась после {len(attempts)} попыток "
            "(повтор выполнялся без параллельных проверок). Последняя ошибка: "
            f"{last.result.technical_error}"
        )
        return replace(last.result, technical_error=message[:_MAX_ERROR_LENGTH])

    def _attempt(
        self, input_pdf: Path, profile: PitStopProfile, *, exclusive: bool
    ) -> _Attempt:
        """One CLI run with its own report directory.

        Hashing and report parsing stay outside the gate; only the remote
        command is limited.  Classified failures are returned, not raised;
        unexpected exceptions propagate (the gate is released either way).
        """
        profile_id = profile.id
        input_sha256 = _sha256(input_pdf)
        job_id = uuid4().hex
        report_dir = self._settings.report_root / job_id
        report_dir.mkdir(parents=True, exist_ok=False)
        report_json = report_dir / "report.json"
        report_xml = report_dir / "report.xml"
        checked_at = datetime.now(UTC)
        overlapped = False

        try:
            windows_input = self._map(input_pdf)
            windows_json = self._map(report_json)
            windows_xml = self._map(report_xml)
            remote_command = _powershell_command(
                self._settings.cli_path,
                input_pdf=windows_input,
                profile=profile.windows_path,
                report_json=windows_json,
                report_xml=windows_xml,
            )
            gate_slot = self._gate.exclusive() if exclusive else self._gate.shared()
            with gate_slot as ticket:
                try:
                    execution = self._transport.execute(
                        remote_command,
                        timeout_seconds=self._settings.command_timeout_seconds,
                    )
                finally:
                    overlapped = ticket.overlapped
            if execution.returncode != 0:
                details = " ".join(
                    part.strip()
                    for part in (execution.stderr, execution.stdout)
                    if part and part.strip()
                )
                message = f"PitStop Server завершился с кодом {execution.returncode}"
                if details:
                    available = max(0, _MAX_ERROR_LENGTH - len(message) - 2)
                    if len(details) > available:
                        details = details[: max(0, available - 3)] + "..."
                    message += f": {details}"
                raise PitStopTransportError(
                    message, kind=classify_cli_exit(execution.returncode, details)
                )
            report = parse_pitstop_report(report_json)
            if _sha256(input_pdf) != input_sha256:
                raise PitStopTransportError(
                    "исходный PDF изменился во время report-only проверки",
                    kind=FailureKind.DEFINITIVE,
                )
        except (PitStopTransportError, PitStopReportError, SharedPathError) as exc:
            if isinstance(exc, PitStopTransportError):
                kind = exc.kind
            elif isinstance(exc, PitStopReportUnreadableError):
                kind = FailureKind.TRANSIENT
            else:
                kind = FailureKind.DEFINITIVE
            failed = PitStopCheckResult(
                status=PitStopExecutionStatus.FAILED,
                profile_id=profile_id,
                input_pdf=input_pdf,
                checked_at=checked_at,
                input_sha256=input_sha256,
                report_json_path=report_json if report_json.exists() else None,
                report_xml_path=report_xml if report_xml.exists() else None,
                technical_error=str(exc),
                job_id=job_id,
            )
            return _Attempt(failed, kind, overlapped, report_dir)

        # The parsed result is persisted in the application database.  JSON and
        # XML are only the transport artifacts required by PitStop, so retaining
        # them for every successful order needlessly grows output_report.
        self._remove_successful_report_dir(report_dir)
        completed = PitStopCheckResult(
            status=PitStopExecutionStatus.COMPLETED,
            profile_id=profile_id,
            input_pdf=input_pdf,
            checked_at=checked_at,
            input_sha256=input_sha256,
            report=report,
            job_id=job_id,
        )
        return _Attempt(completed, None, overlapped, report_dir)

    def _map(self, path: Path) -> PureWindowsPath:
        return mac_shared_path_to_windows(
            path,
            mac_shared_root=self._settings.mac_shared_root,
            windows_shared_root=self._settings.windows_shared_root,
        )

    @staticmethod
    def _remove_successful_report_dir(report_dir: Path) -> None:
        try:
            shutil.rmtree(report_dir)
        except OSError:
            # A failed cleanup must not turn a completed preflight into an
            # operator-visible failure. The next run will use another job dir.
            logger.warning(
                "Не удалось удалить временный отчёт PitStop: %s", report_dir
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _powershell_command(
    cli_path: PureWindowsPath,
    *,
    input_pdf: PureWindowsPath,
    profile: PureWindowsPath,
    report_json: PureWindowsPath,
    report_xml: PureWindowsPath,
) -> str:
    arguments = [
        str(cli_path),
        "-input", str(input_pdf),
        "-mutator", str(profile),
        "-reportJSON", str(report_json),
        "-reportXML", str(report_xml),
    ]
    quoted = " ".join(_ps_quote(argument) for argument in arguments)
    return f"powershell.exe -NoProfile -NonInteractive -Command \"& {quoted}\""


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
