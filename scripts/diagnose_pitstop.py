"""Run report-only PitStop diagnostics against one or more PDF files.

Configuration is read from IMAGE_MAGIC_PITSTOP_* environment variables, just
like the web service. Diagnostics and PitStop reports are preserved in a
run-specific folder; input PDFs are never modified.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sys
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.settings import Settings
from services.pitstop import (
    PitStopProfile,
    PitStopProfileCatalog,
    PitStopReportError,
    SSHSettings,
    SSHTransport,
    SharedPathError,
    PitStopTransportError,
    mac_shared_path_to_windows,
    parse_pitstop_report,
)
from services.pitstop.service import _powershell_command


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_settings(settings: Settings, profile_id: str, report_root: Path):
    required = {
        "IMAGE_MAGIC_PITSTOP_HOST": settings.pitstop_host,
        "IMAGE_MAGIC_PITSTOP_USERNAME": settings.pitstop_username,
        "IMAGE_MAGIC_PITSTOP_CLI_PATH": settings.pitstop_cli_path,
    }
    missing = [key for key, value in required.items() if not value]
    profiles = {profile_id: (label, path) for profile_id, label, path in settings.pitstop_profiles}
    if profile_id not in profiles:
        missing.append(f"IMAGE_MAGIC_PITSTOP_PROFILE_{profile_id.upper()}")
    if missing:
        raise ValueError("Не заданы параметры: " + ", ".join(missing))
    label, profile_path = profiles[profile_id]
    profile = PitStopProfile(profile_id, label, PureWindowsPath(profile_path))
    catalog = PitStopProfileCatalog([profile])
    transport = SSHTransport(SSHSettings(
        host=settings.pitstop_host,
        username=settings.pitstop_username,
        port=settings.pitstop_port,
        known_hosts_file=settings.pitstop_known_hosts_file,
        identity_file=settings.pitstop_identity_file,
        connect_timeout_seconds=settings.pitstop_connect_timeout_seconds,
    ))
    return catalog, transport, report_root


def diagnose_one(
    pdf: Path,
    *,
    profile_id: str,
    settings: Settings,
    catalog: PitStopProfileCatalog,
    transport: SSHTransport,
    report_root: Path,
) -> tuple[dict[str, object], int]:
    source = pdf.expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(f"Не PDF-файл: {source}")
    root = settings.pitstop_mac_shared_root.expanduser().resolve(strict=True)
    sha_before = sha256_file(source)
    job_dir = report_root / f"{source.stem[:48]}-{uuid4().hex[:10]}"
    job_dir.mkdir(parents=True, exist_ok=False)
    report_json = job_dir / "report.json"
    report_xml = job_dir / "report.xml"
    log_path = job_dir / "diagnostic.log"
    try:
        windows_input = mac_shared_path_to_windows(
            source, mac_shared_root=root,
            windows_shared_root=PureWindowsPath(settings.pitstop_windows_shared_root),
        )
        windows_json = mac_shared_path_to_windows(
            report_json, mac_shared_root=root,
            windows_shared_root=PureWindowsPath(settings.pitstop_windows_shared_root),
        )
        windows_xml = mac_shared_path_to_windows(
            report_xml, mac_shared_root=root,
            windows_shared_root=PureWindowsPath(settings.pitstop_windows_shared_root),
        )
        profile = catalog.get(profile_id)
        command = _powershell_command(
            PureWindowsPath(settings.pitstop_cli_path),
            input_pdf=windows_input,
            profile=profile.windows_path,
            report_json=windows_json,
            report_xml=windows_xml,
        )
        result = transport.execute(command, timeout_seconds=settings.pitstop_command_timeout_seconds)
        stdout, stderr = result.stdout or "", result.stderr or ""
        log_path.write_text(
            f"Started: {datetime.now(UTC).isoformat()}\n"
            f"Input: {source}\nProfile: {profile_id} ({profile.windows_path})\n"
            f"Return code: {result.returncode}\n\n"
            f"===== STDOUT =====\n{stdout}\n===== STDERR =====\n{stderr}\n",
            encoding="utf-8",
        )
        report = None
        report_error = None
        if report_json.is_file():
            try:
                parsed = parse_pitstop_report(report_json)
                report = {
                    "verdict": parsed.verdict.value,
                    "pages": parsed.pages,
                    "counts": {
                        "errors": parsed.counts.errors,
                        "warnings": parsed.counts.warnings,
                        "critical_failures": parsed.counts.critical_failures,
                        "noncritical_failures": parsed.counts.noncritical_failures,
                        "fixes": parsed.counts.fixes,
                    },
                    "issues": [
                        {"severity": issue.severity.value, "message": issue.message}
                        for issue in parsed.issues
                    ],
                }
            except PitStopReportError as exc:
                report_error = str(exc)
        sha_after = sha256_file(source)
        record = {
            "input": str(source),
            "input_sha256_before": sha_before,
            "input_sha256_after": sha_after,
            "input_unchanged": sha_before == sha_after,
            "profile_id": profile_id,
            "exit_code": result.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "report_json": str(report_json) if report_json.exists() else None,
            "report_xml": str(report_xml) if report_xml.exists() else None,
            "report_error": report_error,
            "report": report,
            "log": str(log_path),
        }
        (job_dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        failed = result.returncode != 0 or report is None or not record["input_unchanged"]
        return record, 1 if failed else 0
    except (OSError, PitStopTransportError, SharedPathError, TimeoutError, ValueError) as exc:
        log_path.write_text(f"Input: {source}\nProfile: {profile_id}\nDiagnostic error: {exc}\n", encoding="utf-8")
        record = {
            "input": str(source), "input_sha256_before": sha_before,
            "profile_id": profile_id, "diagnostic_error": str(exc),
            "log": str(log_path),
        }
        (job_dir / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return record, 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", nargs="+", type=Path, help="PDF-файлы, доступные через общую папку PitStop")
    parser.add_argument("--profile", choices=("digital", "offset"), default="digital")
    parser.add_argument(
        "--report-root", type=Path,
        help="Папка результатов внутри IMAGE_MAGIC_PITSTOP_MAC_SHARED_ROOT",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    report_root = (
        args.report_root.expanduser().resolve()
        if args.report_root
        else settings.pitstop_mac_shared_root.expanduser().resolve() / "output_report" / "pitstop-diagnostics"
    )
    try:
        report_root.relative_to(settings.pitstop_mac_shared_root.expanduser().resolve())
    except ValueError:
        parser.error("--report-root должен находиться внутри общей папки PitStop")
    if not settings.pitstop_mac_shared_root.expanduser().is_dir():
        parser.error("IMAGE_MAGIC_PITSTOP_MAC_SHARED_ROOT недоступен на этом сервере")
    try:
        catalog, transport, report_root = build_settings(settings, args.profile, report_root)
        report_root.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"PitStop: {settings.pitstop_host}:{settings.pitstop_port}; profile={args.profile}")
    print(f"Результаты: {report_root}")
    overall = 0
    for pdf in args.pdf:
        record, code = diagnose_one(
            pdf, profile_id=args.profile, settings=settings, catalog=catalog,
            transport=transport, report_root=report_root,
        )
        overall |= code
        report = record.get("report") or {}
        counts = report.get("counts") or {}
        state = "CHECKED" if code == 0 else "FAILED"
        print(
            f"[{state}] {record['input']} exit={record.get('exit_code', '—')} "
            f"verdict={report.get('verdict', 'нет отчёта')} "
            f"errors={counts.get('errors', '—')} warnings={counts.get('warnings', '—')}"
        )
        if record.get("diagnostic_error"):
            print(f"  Ошибка диагностики: {record['diagnostic_error']}")
        for issue in (report.get("issues") or [])[:20]:
            print(f"  {issue['severity']}: {issue['message']}")
        diagnostic_output = record.get("stderr") or record.get("stdout")
        if diagnostic_output:
            print(f"  Вывод PitStop: {diagnostic_output[:1200]}")
        if record.get("report_json"):
            print(f"  JSON: {record['report_json']}")
        if record.get("log"):
            print(f"  Лог: {record['log']}")
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
