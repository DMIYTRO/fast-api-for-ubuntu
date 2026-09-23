#!/usr/bin/env python3
"""Resave artwork to PDF, run a callas profile, and create first-page previews."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "tools/callas_pdfToolboxCLI_x64_Linux_17-0-682/pdfToolbox"
CACHE = ROOT / ".callas-cache"
SUPPORTED = {".pdf", ".tif", ".tiff", ".psd", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}


@dataclass
class Result:
    source: str
    pdf: str
    preview: str
    seconds: float
    convert_code: int
    profile_code: int
    preview_code: int
    status: str
    profile_summary: str


def run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("pdf_dir", type=Path)
    parser.add_argument("preview_dir", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    for path, label in ((args.input, "input"), (args.profile, "profile"), (CLI, "Callas CLI")):
        if not path.exists():
            parser.error(f"{label} not found: {path}")
    args.pdf_dir.mkdir(parents=True, exist_ok=True)
    args.preview_dir.mkdir(parents=True, exist_ok=True)
    reports = args.pdf_dir / "reports"
    logs = args.pdf_dir / "logs"
    reports.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    sources = sorted((p for p in args.input.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED), key=lambda p: p.name.casefold())
    width = max(4, len(str(len(sources))))

    def process(pair: tuple[int, Path]) -> Result:
        index, source = pair
        prefix = f"{index:0{width}d}_"
        pdf = args.pdf_dir / f"{prefix}{source.stem}.pdf"
        work_pdf = args.pdf_dir / f".{prefix}{source.stem}.source.pdf"
        profile_pdf = args.pdf_dir / f".{prefix}{source.stem}.profile.pdf"
        report = reports / f"{prefix}{source.stem}.txt"
        preview_base = args.preview_dir / f"{prefix}{source.stem}.jpg"
        started = time.perf_counter()
        output_parts: list[str] = []
        existing_previews = sorted(args.preview_dir.glob(f"{prefix}{source.stem}*.jpg"))
        if pdf.is_file() and pdf.stat().st_size and report.is_file() and report.stat().st_size and existing_previews:
            report_text = report.read_text(encoding="utf-8", errors="replace")
            summary = " | ".join(line.strip() for line in report_text.splitlines() if line.startswith(("Hit", "Summary")))
            return Result(str(source), str(pdf), str(existing_previews[0]), 0.0, 0, 0, 0, "success", summary)
        try:
            work_pdf.unlink(missing_ok=True)
            if source.suffix.lower() == ".pdf":
                conversion = run([str(CLI), f"--cachefolder={CACHE}", "--noprogress", "--mergepdf", "--nooptimization", "--overwrite", f"--outputfile={work_pdf}", str(source)], args.timeout)
            else:
                conversion = run([str(CLI), f"--cachefolder={CACHE}", "--noprogress", "--topdf", "--overwrite", f"--outputfile={work_pdf}", str(source)], args.timeout)
            output_parts.extend((conversion.stdout, conversion.stderr))
            if conversion.returncode >= 100 or not work_pdf.exists():
                raise RuntimeError("Callas did not create PDF")
            # Let callas write a unique staging file. Replacing an existing target
            # directly from callas is unreliable on SMB shares.
            profile_pdf.unlink(missing_ok=True)
            profile_run = run([str(CLI), f"--cachefolder={CACHE}", "--noprogress", "--overwrite", f"--outputfile={profile_pdf}", str(args.profile), str(work_pdf)], args.timeout)
            output_parts.extend((profile_run.stdout, profile_run.stderr))
            report.write_text("\n".join((profile_run.stdout, profile_run.stderr)), encoding="utf-8")
            work_pdf.unlink(missing_ok=True)
            if profile_run.returncode >= 100 or not profile_pdf.exists():
                raise RuntimeError("Callas profile did not create final PDF")
            profile_pdf.replace(pdf)
            preview_run = run([str(CLI), f"--cachefolder={CACHE}", "--noprogress", "--saveasimg", "--pagerange=1", "--resolution=1200x1200", "--colorspace=RGB", "--imgformat=JPEG", "--compression=JPEG_high", "--overwrite", f"--outputfile={preview_base}", str(pdf)], args.timeout)
            output_parts.extend((preview_run.stdout, preview_run.stderr))
            candidates = sorted(args.preview_dir.glob(f"{prefix}{source.stem}*.jpg"))
            preview = candidates[0] if candidates else preview_base
            ok = profile_run.returncode < 100 and preview_run.returncode < 100 and preview.exists()
            summary = " | ".join(line.strip() for line in profile_run.stdout.splitlines() if line.startswith(("Hit", "Summary")))
            status = "success" if ok else "failed"
            profile_code, preview_code = profile_run.returncode, preview_run.returncode
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            output_parts.append(str(exc))
            status, profile_code, preview_code, summary, preview = "failed", 142, 142, str(exc), preview_base
        (logs / f"{prefix}{source.stem}.log").write_text("\n".join(output_parts), encoding="utf-8")
        return Result(str(source), str(pdf), str(preview), round(time.perf_counter() - started, 3), conversion.returncode if 'conversion' in locals() else 142, profile_code, preview_code, status, summary)

    started_at = datetime.now(timezone.utc).isoformat()
    wall = time.perf_counter()
    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process, item): item[0] for item in enumerate(sources, 1)}
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{done}/{len(sources)}] {result.status} {result.seconds:.2f}s {Path(result.source).name}", flush=True)
    results.sort(key=lambda r: Path(r.pdf).name)
    wall_seconds = round(time.perf_counter() - wall, 3)
    finished_at = datetime.now(timezone.utc).isoformat()
    payload = {"started_at": started_at, "finished_at": finished_at, "wall_seconds": wall_seconds, "workers": args.workers, "profile": str(args.profile), "files": [asdict(r) for r in results]}
    (args.pdf_dir / "benchmark.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = "".join(f"<tr><td>{i}</td><td>{html.escape(Path(r.source).name)}</td><td>{r.seconds:.3f}</td><td>{html.escape(r.status)}</td><td>{r.profile_code}</td><td>{html.escape(r.profile_summary)}</td></tr>" for i, r in enumerate(results, 1))
    successful = sum(r.status == "success" for r in results)
    page = f"""<!doctype html><html lang=ru><meta charset=utf-8><title>Callas White & Clear benchmark</title><style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccd3dc;padding:6px;text-align:left}}th{{background:#e8eef6}}</style><h1>Callas: пересохранение и White & Clear spot check</h1><p>Файлов: {len(results)}; успешно: {successful}; ошибок: {len(results)-successful}; потоков: {args.workers}; время: {wall_seconds:.3f} с.</p><p>Профиль: {html.escape(args.profile.name)}</p><table><tr><th>#</th><th>Файл</th><th>Секунды</th><th>Статус</th><th>Код профиля</th><th>Результат Callas</th></tr>{rows}</table></html>"""
    (args.pdf_dir / "benchmark.html").write_text(page, encoding="utf-8")
    print(f"Completed {len(results)} files in {wall_seconds:.3f}s; successful={successful}", flush=True)
    return 0 if successful == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
