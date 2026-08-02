"""Report-only PitStop check orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from .catalog import PitStopProfileCatalog
from .models import PitStopCheckResult, PitStopExecutionStatus
from .parser import PitStopReportError, parse_pitstop_report
from .paths import SharedPathError, mac_shared_path_to_windows
from .transport import PitStopTransport, PitStopTransportError


@dataclass(frozen=True, slots=True)
class PitStopServiceSettings:
    cli_path: PureWindowsPath
    mac_shared_root: Path
    windows_shared_root: PureWindowsPath
    report_root: Path
    command_timeout_seconds: float = 180.0

    def __post_init__(self) -> None:
        if not self.cli_path.is_absolute():
            raise ValueError("путь к PitStopServerCLI.exe должен быть абсолютным")
        if self.command_timeout_seconds <= 0:
            raise ValueError("таймаут PitStop должен быть положительным")


class PitStopService:
    def __init__(
        self,
        *,
        settings: PitStopServiceSettings,
        profiles: PitStopProfileCatalog,
        transport: PitStopTransport,
    ) -> None:
        self._settings = settings
        self._profiles = profiles
        self._transport = transport

    def check_pdf(self, input_pdf: Path, *, profile_id: str) -> PitStopCheckResult:
        """Run a profile without an output argument and parse JSON/XML artifacts."""
        input_pdf = input_pdf.expanduser().resolve(strict=False)
        if not input_pdf.is_file() or input_pdf.suffix.lower() != ".pdf":
            raise ValueError(f"PDF для проверки не найден: {input_pdf}")
        profile = self._profiles.get(profile_id)
        input_sha256 = _sha256(input_pdf)
        job_id = uuid4().hex
        report_dir = self._settings.report_root / job_id
        report_dir.mkdir(parents=True, exist_ok=False)
        report_json = report_dir / "report.json"
        report_xml = report_dir / "report.xml"
        checked_at = datetime.now(UTC)

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
            execution = self._transport.execute(
                remote_command,
                timeout_seconds=self._settings.command_timeout_seconds,
            )
            if execution.returncode != 0:
                raise PitStopTransportError(
                    f"PitStop Server завершился с кодом {execution.returncode}"
                )
            report = parse_pitstop_report(report_json)
            if _sha256(input_pdf) != input_sha256:
                raise PitStopTransportError(
                    "исходный PDF изменился во время report-only проверки"
                )
        except (PitStopTransportError, PitStopReportError, SharedPathError) as exc:
            return PitStopCheckResult(
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

        return PitStopCheckResult(
            status=PitStopExecutionStatus.COMPLETED,
            profile_id=profile_id,
            input_pdf=input_pdf,
            checked_at=checked_at,
            input_sha256=input_sha256,
            report=report,
            report_json_path=report_json,
            report_xml_path=report_xml if report_xml.exists() else None,
            job_id=job_id,
        )

    def _map(self, path: Path) -> PureWindowsPath:
        return mac_shared_path_to_windows(
            path,
            mac_shared_root=self._settings.mac_shared_root,
            windows_shared_root=self._settings.windows_shared_root,
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
