#!/usr/bin/env python3
"""Benchmark TIFF preflight using callas pdfToolbox only."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import html
import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
CALLAS_ROOT = ROOT / "tools/callas_pdfToolboxCLI_x64_Linux_17-0-682"
DEFAULT_CLI = CALLAS_ROOT / "pdfToolbox"
DEFAULT_CACHE = ROOT / ".callas-cache"
DEFAULT_PRINT_PROFILE = CALLAS_ROOT / "var/Profiles/PDF analysis/List potential printing problems.kfpx"
DEFAULT_TRANSPARENCY_PROFILE = CALLAS_ROOT / "var/Profiles/PDF analysis/List transparent objects.kfpx"
DEFAULT_FLATTEN_PROFILE = CALLAS_ROOT / "var/Profiles/PDF fixups/Flatten transparency (high resolution).kfpx"
TIFF_EXTENSIONS = {".tif", ".tiff"}
SUMMARY_RE = re.compile(r"^Summary\s+(Corrections|Errors|Warnings|Infos)\s+(\d+)", re.MULTILINE)
HIT_RE = re.compile(r"^Hit\s+(Error|Warning|Info)\s+(.+)$", re.MULTILINE)
PAGES_RE = re.compile(r"^Pages\s+(\d+)", re.MULTILINE)


@dataclass(slots=True)
class Result:
    index: int
    source: str
    source_bytes: int
    seconds: float
    status: str
    pages: int
    transparency_callas: bool
    soft_mask: bool
    opacity_transparency: bool
    blend_mode: bool
    transparency_group: bool
    flatten_corrections: int
    callas_errors: int
    callas_warnings: int
    callas_infos: int
    print_findings: str
    transparency_findings: str
    print_report: str
    transparency_report: str
    message: str


def safe_name(index: int, source: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", source.stem).strip("._")[:90] or "tiff"
    return f"{index:04d}_{stem}"


def callas_profile(source: Path, profile: Path, cli: Path, cache: Path, report_path: Path, timeout: float):
    with tempfile.TemporaryDirectory(prefix="callas-tiff-") as temporary:
        linked_input = Path(temporary) / source.name
        linked_input.symlink_to(source)
        command = [str(cli), f"--cachefolder={cache}", "--noprogress", "--analyze", "--report=JSONV2,ALWAYS", str(profile), str(linked_input)]
        completed = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        generated = linked_input.with_suffix(".json")
        if generated.is_file():
            shutil.copyfile(generated, report_path)
    summary = {name.lower(): int(count) for name, count in SUMMARY_RE.findall(output)}
    hits = [f"{severity}: {text.strip()}" for severity, text in HIT_RE.findall(output)]
    pages_match = PAGES_RE.search(output)
    return completed.returncode, output, summary, hits, int(pages_match.group(1)) if pages_match else 0


def callas_fixup_probe(source: Path, profile: Path, cli: Path, cache: Path, report_path: Path, timeout: float):
    """Apply a fixup to temporary data; the source TIFF is never modified."""
    with tempfile.TemporaryDirectory(prefix="callas-tiff-fixup-") as temporary:
        temporary_dir = Path(temporary)
        linked_input = temporary_dir / source.name
        linked_input.symlink_to(source)
        fixed_pdf = temporary_dir / f"{source.stem}_flattened.pdf"
        command = [
            str(cli), f"--cachefolder={cache}", "--noprogress",
            "--report=JSONV2,ALWAYS,ALLFIXUPS", f"--outputfile={fixed_pdf}",
            str(profile), str(linked_input),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        generated = linked_input.with_suffix(".json")
        if generated.is_file():
            shutil.copyfile(generated, report_path)
    summary = {name.lower(): int(count) for name, count in SUMMARY_RE.findall(output)}
    hits = [f"{severity}: {text.strip()}" for severity, text in HIT_RE.findall(output)]
    return completed.returncode, output, summary, hits


def process_one(index: int, source: Path, total: int, output_dir: Path, cli: Path, cache: Path, print_profile: Path, transparency_profile: Path, flatten_profile: Path, timeout: float, lock: Lock) -> Result:
    started = time.perf_counter()
    base = safe_name(index, source)
    print_report = output_dir / "reports" / f"{base}_printing.json"
    transparency_report = output_dir / "reports" / f"{base}_transparency.json"
    flatten_report = output_dir / "reports" / f"{base}_flatten_fixup.json"
    try:
        print_code, print_output, summary, print_hits, pages = callas_profile(source, print_profile, cli, cache, print_report, timeout)
        trans_code, trans_output, trans_summary, trans_hits, trans_pages = callas_profile(source, transparency_profile, cli, cache, transparency_report, timeout)
        fix_code, fix_output, fix_summary, fix_hits = callas_fixup_probe(source, flatten_profile, cli, cache, flatten_report, timeout)
        lowered_hits = "\n".join(trans_hits).lower()
        soft_mask = "soft mask" in lowered_hits or "softmask" in lowered_hits
        opacity = "ca value" in lowered_hits or "constant alpha" in lowered_hits or "annotation" in lowered_hits
        blend_mode = "blend mode" in lowered_hits
        transparency_group = "transparency group" in lowered_hits
        transparency = bool(trans_hits)
        flatten_corrections = fix_summary.get("corrections", 0)
        status = "success" if print_code < 100 and trans_code < 100 and fix_code < 100 and print_report.is_file() and transparency_report.is_file() else "failed"
        message = "OK" if status == "success" else f"Callas codes: printing={print_code}, transparency={trans_code}, fixup={fix_code}"
        combined = f"PRINTING PROFILE\n{print_output}\n\nTRANSPARENCY/MASK PROFILE\n{trans_output}\n\nFLATTEN FIXUP PROBE\n{fix_output}"
        errors = summary.get("errors", 0) + trans_summary.get("errors", 0) + fix_summary.get("errors", 0)
        warnings = summary.get("warnings", 0) + trans_summary.get("warnings", 0) + fix_summary.get("warnings", 0)
        infos = summary.get("infos", 0) + trans_summary.get("infos", 0) + fix_summary.get("infos", 0)
        pages = pages or trans_pages
    except Exception as exc:
        status, message, combined = "failed", str(exc), f"ERROR\n{exc}"
        errors = warnings = infos = pages = 0
        transparency, soft_mask, opacity, blend_mode, transparency_group = False, False, False, False, False
        flatten_corrections, print_hits, trans_hits = 0, [], []
    elapsed = time.perf_counter() - started
    (output_dir / "logs" / f"{base}.log").write_text(combined, encoding="utf-8")
    result = Result(index, str(source), source.stat().st_size, round(elapsed, 3), status, pages, transparency, soft_mask, opacity, blend_mode, transparency_group, flatten_corrections, errors, warnings, infos, " | ".join(print_hits), " | ".join(trans_hits), str(print_report) if print_report.is_file() else "", str(transparency_report) if transparency_report.is_file() else "", message)
    with lock:
        print(f"[{index}/{total}] {status} {elapsed:.3f}s {source.name}", flush=True)
    return result


def write_outputs(output_dir: Path, started_at: str, finished_at: str, wall: float, workers: int, results: list[Result]) -> None:
    ordered = sorted(results, key=lambda item: item.index)
    payload = {"engine": "callas pdfToolbox only", "started_at": started_at, "finished_at": finished_at, "workers": workers, "wall_seconds": round(wall, 3), "files": [asdict(item) for item in ordered]}
    (output_dir / "benchmark.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "benchmark.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(Result.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(item) for item in ordered)
    details_dir = output_dir / "details"
    details_dir.mkdir(exist_ok=True)
    rows = []
    for item in ordered:
        name = safe_name(item.index, Path(item.source))
        print_items = "".join(f"<li>{html.escape(value)}</li>" for value in item.print_findings.split(" | ") if value) or "<li>Нет</li>"
        trans_items = "".join(f"<li>{html.escape(value)}</li>" for value in item.transparency_findings.split(" | ") if value) or "<li>Нет</li>"
        detail = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>{html.escape(Path(item.source).name)}</title><style>body{{font:15px system-ui;max-width:1100px;margin:30px auto;color:#172033}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd3dc;padding:8px;text-align:left}}th{{width:300px;background:#eef3f8}}.bad{{color:#b42318;font-weight:700}}.ok{{color:#08783e;font-weight:700}}</style><p><a href="../benchmark.html">← Общий отчёт</a></p><h1>{html.escape(Path(item.source).name)}</h1><p><b>Источник результатов: callas pdfToolbox. ImageMagick не используется.</b></p><table><tr><th>Размер файла</th><td>{item.source_bytes / 1024 / 1024:.2f} MiB</td></tr><tr><th>Страниц по Callas</th><td>{item.pages}</td></tr><tr><th>Прозрачность по Callas</th><td class={'bad' if item.transparency_callas else 'ok'}>{'ОБНАРУЖЕНА' if item.transparency_callas else 'не обнаружена'}</td></tr><tr><th>Soft mask (в т.ч. маска Illustrator)</th><td>{'ЕСТЬ' if item.soft_mask else 'нет'}</td></tr><tr><th>Opacity / constant alpha</th><td>{'ЕСТЬ' if item.opacity_transparency else 'нет'}</td></tr><tr><th>Blend mode</th><td>{'ЕСТЬ' if item.blend_mode else 'нет'}</td></tr><tr><th>Transparency group</th><td>{'ЕСТЬ' if item.transparency_group else 'нет'}</td></tr><tr><th>Коррекции Flatten transparency</th><td>{item.flatten_corrections}</td></tr><tr><th>Время трёх проверок</th><td>{item.seconds:.3f} с</td></tr><tr><th>Статус</th><td>{html.escape(item.status)}</td></tr></table><h2>Потенциальные проблемы печати</h2><ul>{print_items}</ul><h2>Прозрачность и маски</h2><ul>{trans_items}</ul><p><small>Fixup выполняется только над временным PDF и не изменяет TIFF. Soft mask подтверждает PDF-маску после преобразования, но не доказывает наличие именованного альфа-канала в структуре исходного TIFF.</small></p></html>"""
        (details_dir / f"{name}.html").write_text(detail, encoding="utf-8")
        rows.append(f'<tr><td>{item.index}</td><td><a href="details/{name}.html">{html.escape(Path(item.source).name)}</a></td><td>{item.source_bytes / 1024 / 1024:.2f} MiB</td><td>{item.pages}</td><td>{"да" if item.transparency_callas else "нет"}</td><td>{"да" if item.soft_mask else "нет"}</td><td>{item.flatten_corrections}</td><td>{item.seconds:.3f}</td><td>{html.escape(item.status)}</td><td>{item.callas_errors}</td><td>{item.callas_warnings}</td></tr>')
    success = sum(item.status == "success" for item in ordered)
    transparent = sum(item.transparency_callas for item in ordered)
    document = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>Callas TIFF benchmark</title><style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd3dc;padding:5px;vertical-align:top}}th{{background:#e8eef6;position:sticky;top:0}}</style><h1>TIFF preflight — только callas pdfToolbox</h1><p>ImageMagick не используется. Файлов: {len(ordered)}; успешно: {success}; прозрачность по Callas: {transparent}; потоков: {workers}; время: {wall:.3f} с.</p><p>Тест 1: временный fixup Flatten transparency. Тест 2: профиль прозрачности отдельно ищет soft mask (типичная маска Illustrator), opacity, blend mode и transparency group.</p><table><thead><tr><th>#</th><th>Файл</th><th>Размер</th><th>Страницы</th><th>Прозрачность</th><th>Soft mask</th><th>Fixup-коррекции</th><th>с</th><th>Статус</th><th>Ошибки</th><th>Предупреждения</th></tr></thead><tbody>{''.join(rows)}</tbody></table></html>"""
    (output_dir / "benchmark.html").write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--print-profile", type=Path, default=DEFAULT_PRINT_PROFILE)
    parser.add_argument("--transparency-profile", type=Path, default=DEFAULT_TRANSPARENCY_PROFILE)
    parser.add_argument("--flatten-profile", type=Path, default=DEFAULT_FLATTEN_PROFILE)
    args = parser.parse_args()
    for path, label in ((args.input, "input"), (args.cli, "Callas CLI"), (args.print_profile, "print profile"), (args.transparency_profile, "transparency profile"), (args.flatten_profile, "flatten profile")):
        if not path.exists():
            parser.error(f"{label} not found: {path}")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "reports").mkdir()
    (args.output / "logs").mkdir()
    sources = sorted((path for path in args.input.iterdir() if path.is_file() and path.suffix.lower() in TIFF_EXTENSIONS), key=lambda path: path.name.casefold())
    started_at = datetime.now(timezone.utc).isoformat()
    wall_started = time.perf_counter()
    lock = Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process_one, index, source, len(sources), args.output, args.cli, args.cache, args.print_profile, args.transparency_profile, args.flatten_profile, args.timeout, lock) for index, source in enumerate(sources, 1)]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    wall = time.perf_counter() - wall_started
    write_outputs(args.output, started_at, datetime.now(timezone.utc).isoformat(), wall, args.workers, results)
    print(f"Completed {len(results)} files in {wall:.3f}s", flush=True)
    return 1 if any(item.status == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
