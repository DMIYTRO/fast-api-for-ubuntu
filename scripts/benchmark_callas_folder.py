#!/usr/bin/env python3
"""Convert a folder to PDF with callas pdfToolbox and record timings/logs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLI = ROOT / "tools/callas_pdfToolboxCLI_x64_Linux_17-0-682/pdfToolbox"
DEFAULT_CACHE = ROOT / ".callas-cache"
SUPPORTED = {".pdf", ".tif", ".tiff", ".psd", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}


@dataclass(slots=True)
class Result:
    index: int
    source: str
    source_type: str
    source_bytes: int
    output: str
    output_bytes: int
    operation: str
    seconds: float
    returncode: int
    status: str
    log: str
    message: str


def output_path(source: Path, output_dir: Path, reserved: set[str]) -> Path:
    candidate = output_dir / f"{source.stem}.pdf"
    key = candidate.name.casefold()
    if key in reserved or candidate.exists():
        candidate = output_dir / f"{source.stem}__{source.suffix.lstrip('.').lower()}.pdf"
        serial = 2
        while candidate.name.casefold() in reserved or candidate.exists():
            candidate = output_dir / f"{source.stem}__{source.suffix.lstrip('.').lower()}_{serial}.pdf"
            serial += 1
    reserved.add(candidate.name.casefold())
    return candidate


def save_json(path: Path, started_at: str, results: list[Result], finished_at: str | None = None) -> None:
    payload = {
        "started_at": started_at,
        "finished_at": finished_at,
        "processed": len(results),
        "success": sum(item.status == "success" for item in results),
        "failed": sum(item.status == "failed" for item in results),
        "total_seconds": round(sum(item.seconds for item in results), 3),
        "files": [asdict(item) for item in results],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def save_csv(path: Path, results: list[Result]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(Result.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(item) for item in results)


def save_html(path: Path, started_at: str, finished_at: str, wall_seconds: float, results: list[Result]) -> None:
    rows = []
    for item in results:
        delta_bytes = item.output_bytes - item.source_bytes
        delta_percent = (delta_bytes / item.source_bytes * 100) if item.source_bytes else 0.0
        rows.append(
            "<tr>"
            f"<td>{item.index}</td><td>{html.escape(Path(item.source).name)}</td>"
            f"<td>{html.escape(item.operation)}</td><td>{item.seconds:.3f}</td>"
            f"<td>{html.escape(item.status)}</td><td>{item.returncode}</td>"
            f"<td>{item.source_bytes / 1048576:.2f} MiB<br><small>{item.source_bytes} байт</small></td>"
            f"<td>{item.output_bytes / 1048576:.2f} MiB<br><small>{item.output_bytes} байт</small></td>"
            f"<td>{delta_bytes / 1048576:+.2f} MiB ({delta_percent:+.1f}%)</td>"
            f"<td>{html.escape(item.message)}</td></tr>"
        )
    success = sum(item.status == "success" for item in results)
    failed = len(results) - success
    document = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<title>Callas benchmark</title><style>
body{{font:14px system-ui;margin:24px;color:#18212f}}table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccd3dc;padding:6px;text-align:left}}th{{background:#e8eef6;position:sticky;top:0}}
.ok{{color:#08783e}}.bad{{color:#b42318}}</style>
<h1>Callas pdfToolbox — обработка без Fixup</h1>
<p>Начало: {html.escape(started_at)}<br>Завершение: {html.escape(finished_at)}<br>
Общее время: {wall_seconds:.3f} с<br>Файлов: {len(results)} · <span class="ok">успешно: {success}</span> · <span class="bad">ошибок: {failed}</span></p>
<p>Профили проверки и исправления (Fixup) не применялись.</p>
<table><thead><tr><th>#</th><th>Файл</th><th>Операция</th><th>Секунды</th><th>Статус</th><th>Код</th><th>Исходный размер</th><th>Размер PDF</th><th>Изменение</th><th>Сообщение</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></html>"""
    path.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    input_dir = args.input.expanduser().resolve()
    output_dir = args.output.expanduser().resolve()
    cli = args.cli.expanduser().resolve()
    cache = args.cache.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"Input folder not found: {input_dir}")
    if not cli.is_file():
        parser.error(f"Callas CLI not found: {cli}")
    output_dir.mkdir(parents=True, exist_ok=False)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir()
    cache.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        (path for path in input_dir.iterdir() if path.is_file() and path.suffix.lower() in SUPPORTED),
        key=lambda path: path.name.casefold(),
    )
    started_at = datetime.now(timezone.utc).isoformat()
    wall_start = time.perf_counter()
    results: list[Result] = []
    reserved: set[str] = set()
    state_path = output_dir / "benchmark.json"

    for index, source in enumerate(sources, 1):
        target = output_path(source, output_dir, reserved)
        operation = "resave-pdf" if source.suffix.lower() == ".pdf" else "convert-to-pdf"
        if operation == "resave-pdf":
            action = ["--mergepdf", "--nooptimization", f"--outputfile={target}", str(source)]
        else:
            action = ["--topdf", "--overwrite", f"--outputfile={target}", str(source)]
        command = [str(cli), f"--cachefolder={cache}", "--noprogress", *action]
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=args.timeout)
            elapsed = time.perf_counter() - started
            combined = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
            status = "success" if completed.returncode < 100 and target.is_file() and target.stat().st_size else "failed"
            message = combined.splitlines()[-1] if combined else ("OK" if status == "success" else "Callas produced no output")
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            combined = f"Timeout after {args.timeout} seconds\n{exc.stdout or ''}\n{exc.stderr or ''}"
            status, message, returncode = "failed", "timeout", 142
        log_path = logs_dir / f"{index:04d}_{source.stem[:100]}.log"
        log_path.write_text("COMMAND\n" + "\n".join(command) + "\n\nCALLAS OUTPUT\n" + combined, encoding="utf-8")
        results.append(Result(index, str(source), source.suffix.lower(), source.stat().st_size, str(target), target.stat().st_size if target.is_file() else 0, operation, round(elapsed, 3), returncode, status, str(log_path), message))
        save_json(state_path, started_at, results)
        print(f"[{index}/{len(sources)}] {status} {elapsed:.3f}s {source.name}", flush=True)

    finished_at = datetime.now(timezone.utc).isoformat()
    wall_seconds = time.perf_counter() - wall_start
    save_json(state_path, started_at, results, finished_at)
    save_csv(output_dir / "benchmark.csv", results)
    save_html(output_dir / "benchmark.html", started_at, finished_at, wall_seconds, results)
    print(f"Completed {len(results)} files in {wall_seconds:.3f}s", flush=True)
    return 1 if any(item.status == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
