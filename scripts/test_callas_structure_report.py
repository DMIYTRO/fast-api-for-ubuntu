#!/usr/bin/env python3
"""Проверка структуры растровых файлов через Callas и PDF-отчёт.

Скрипт не изменяет исходники: Callas получает исходный файл только для чтения,
а все результаты складываются в отдельный каталог.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.callas_toolbox import CallasToolbox, CallasToolboxError
from server.settings import Settings
DEFAULT_CLI = ROOT / "tools/callas_pdfToolboxCLI_x64_Linux_17-0-682/pdfToolbox"
DEFAULT_OUTPUT = ROOT / "output/callas_structure_test"
IMAGE_EXTENSIONS = {".tif", ".tiff", ".psd", ".png", ".jpg", ".jpeg", ".pdf"}


@dataclass
class FileResult:
    path: str
    format: str = ""
    channels: str = "не определено"
    alpha: str = "не проверено"
    layers: str = "не проверено"
    pages: str = "не проверено"
    callas: str = "не запускался"
    error: str = ""


def _font() -> str:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            pdfmetrics.registerFont(TTFont("ReportFont", str(path)))
            return "ReportFont"
    return "Helvetica"


def inspect(path: Path) -> FileResult:
    result = FileResult(str(path))
    identify = shutil.which("magick") or shutil.which("identify")
    if not identify:
        result.error = "ImageMagick (magick/identify) не найден"
        return result
    command = [identify, "identify"] if Path(identify).name == "magick" else [identify]
    command += ["-format", "%m\t%[channels]\t%[tiff:has-layers]\t%p\n", str(path)]
    command[-2] = "%m" + chr(9) + "%[channels]" + chr(9) + "%[tiff:has-layers]" + chr(9) + "%p" + chr(10)
    import subprocess
    completed = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if completed.returncode:
        result.error = completed.stderr.strip() or f"identify завершился с кодом {completed.returncode}"
        return result
    rows = [line.split(chr(9)) for line in completed.stdout.splitlines() if line.strip()]
    if not rows:
        result.error = "ImageMagick не вернул сведения о файле"
        return result
    first = rows[0]
    result.format = first[0]
    result.channels = first[1] if len(first) > 1 else ""
    channel_name = result.channels.lower().split()
    result.alpha = "есть" if channel_name and channel_name[0].endswith("a") else "нет"
    if path.suffix.lower() in {".tif", ".tiff", ".psd"}:
        result.layers = "есть" if any(len(row) > 2 and row[2].strip().lower() in {"true", "1", "yes"} for row in rows) else "нет"
    result.pages = str(len(rows))
    return result


def build_report(results: list[FileResult], output: Path) -> None:
    font = _font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle("title", parent=styles["Title"], fontName=font, fontSize=18, leading=22)
    body = ParagraphStyle("body", parent=styles["BodyText"], fontName=font, fontSize=9, leading=12)
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    story = [Paragraph("Отчёт проверки Callas Toolbox", title), Spacer(1, 5 * mm), Paragraph("Растровые файлы сначала обрабатываются Callas (--topdf). Структурные свойства исходника (каналы, альфа-канал и слои) фиксируются отдельно до конвертации. Исходные файлы не изменялись.", body)]
    for index, item in enumerate(results, 1):
        story += [PageBreak(), Paragraph(f"Файл {index}: {escape(Path(item.path).name)}", title), Spacer(1, 4 * mm)]
        rows = [["Параметр", "Результат"], ["Путь", item.path], ["Формат", item.format], ["Каналы", item.channels], ["Альфа-канал", item.alpha], ["Несведённые слои", item.layers], ["Страницы/кадры", item.pages], ["Callas", item.callas], ["Ошибка", item.error or "нет"]]
        table = Table([[Paragraph(escape(str(cell)), body) for cell in row] for row in rows], colWidths=[45 * mm, 135 * mm])
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dbeafe")), ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTNAME", (0, 0), (-1, -1), font), ("PADDING", (0, 0), (-1, -1), 5)]))
        story.append(table)
    doc.build(story)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Файл или каталог с тестовыми файлами")
    parser.add_argument("--cli", type=Path, default=DEFAULT_CLI, help="Путь к pdfToolbox")
    parser.add_argument("--cache", type=Path, default=ROOT / ".callas-cache", help="Кэш/активация Callas")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-callas", action="store_true", help="Только анализ ImageMagick, без запуска Callas")
    args = parser.parse_args()
    source = args.input.expanduser().resolve()
    files = [source] if source.is_file() else sorted(p for p in source.rglob("*") if p.suffix.lower() in IMAGE_EXTENSIONS)
    if not files:
        raise SystemExit("Не найдено файлов для проверки")
    results = [inspect(path) for path in files]
    args.output.mkdir(parents=True, exist_ok=True)
    if not args.no_callas:
        settings = Settings.from_env()
        toolbox = CallasToolbox(settings.__class__(**{
            **{field: getattr(settings, field) for field in settings.__dataclass_fields__},
            "callas_cli_path": args.cli,
            "callas_cache_dir": args.cache,
        }))
        for item, path in zip(results, files):
            if path.suffix.lower() == ".pdf":
                item.callas = "исходный PDF (конвертация не требуется)"
                continue
            try:
                converted = args.output / "callas-output" / f"{path.stem}.pdf"
                toolbox.convert_to_pdf(path, converted)
                item.callas = f"PDF создан: {converted.name}"
            except CallasToolboxError as exc:
                item.callas = "ошибка"
                item.error = f"{item.error}; {exc}".strip("; ")
    (args.output / "results.json").write_text(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8")
    build_report(results, args.output / "callas_structure_report.pdf")
    print(args.output / "callas_structure_report.pdf")


if __name__ == "__main__":
    main()
