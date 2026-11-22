"""«Получить заказ» из командной строки: перенос файлов заказов во входную папку.

Конфигурация — из IMAGE_MAGIC_* (``Settings.from_env()``, как у веб-сервиса) плюс флаги.
Скрипт не импортирует ``control_panel`` и не обращается к базе данных.

    python scripts/fetch_orders.py --dry-run
    python scripts/fetch_orders.py --mode copy --workers 2 --json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from pathlib import Path
import sys
import threading

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.settings import Settings
from services.order_fetch import (
    MODES,
    FetchConfigError,
    FetchProgress,
    fetch_config_from_settings,
    parse_routes,
    run_fetch,
    scan_fetch,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    parser.add_argument("--mode", choices=MODES, help="move — перенести, copy — только скопировать")
    parser.add_argument("--workers", type=int, help="потоков копирования")
    parser.add_argument("--source-root", type=Path, help="корень сетевой папки (IMAGE_MAGIC_FETCH_SOURCE_ROOT)")
    parser.add_argument("--routes", help="маршруты «src1|src2>dst;src3>dst2» (IMAGE_MAGIC_FETCH_ROUTES)")
    parser.add_argument("--no-require-mount", action="store_true", help="не проверять точку монтирования")
    parser.add_argument("--json", action="store_true", help="итог в JSON")
    return parser.parse_args(argv)


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    changes: dict = {}
    if args.mode:
        changes["fetch_mode"] = args.mode
    if args.workers:
        changes["fetch_workers"] = max(1, args.workers)
    if args.source_root:
        changes["fetch_source_root"] = args.source_root.expanduser()
    if args.routes is not None:
        changes["fetch_routes"] = parse_routes(args.routes)
        changes["fetch_routes_error"] = ""
    if args.no_require_mount:
        changes["fetch_require_mount"] = False
    return dataclasses.replace(settings, **changes) if changes else settings


def _mb(value: float) -> str:
    return f"{value / (1024 * 1024):.1f} МБ"


def _print_items(title: str, items: list[dict]) -> None:
    if not items:
        return
    print(f"{title}: {len(items)}")
    for item in items:
        reason = f" — {item['reason']}" if item.get("reason") else ""
        print(f"  {item['name']} ({_mb(item.get('size', 0))}){reason}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    try:
        config = fetch_config_from_settings(_settings(args))
    except (FetchConfigError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        plan = scan_fetch(dataclasses.replace(config, log_path=None))
        data = plan.as_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"Режим: {config.mode} (пробный запуск, файлы не трогаются)")
            for route in config.routes:
                print(f"  {' | '.join(map(str, route.sources))} -> {route.destination}")
            _print_items("Будет перенесено", data["transfer"])
            print(f"Всего: {_mb(data['total_bytes'])}")
            _print_items("Пропущено", data["skipped"])
            _print_items("Ошибки", data["failed"])
            for error in data["source_errors"]:
                print(f"Источник [{error['severity']}]: {error['message']}")
        return 0

    progress = FetchProgress()
    done = threading.Event()

    def report() -> None:
        while not done.wait(2.0):
            snap = progress.snapshot()
            print(
                f"\r{snap['state']}: {snap['done_files']}/{snap['total_files']} файлов, "
                f"{_mb(snap['done_bytes'])} из {_mb(snap['total_bytes'])}, "
                f"{_mb(snap['bytes_per_second'])}/с",
                end="", file=sys.stderr, flush=True,
            )

    reporter = threading.Thread(target=report, daemon=True)
    if sys.stderr.isatty():
        reporter.start()
    try:
        result = run_fetch(config, progress)
    finally:
        done.set()
    if sys.stderr.isatty():
        print(file=sys.stderr)
    data = result.as_dict()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_items("Перенесено", data["moved"])
        _print_items("Скопировано", data["copied"])
        _print_items("Пропущено", data["skipped"])
        _print_items("Ошибки", data["failed"])
        for error in data["source_errors"]:
            print(f"Источник [{error['severity']}]: {error['message']}")
        print(
            f"Готово за {data['duration_seconds']:.1f} с, {_mb(data['total_bytes'])}, "
            f"{_mb(data['bytes_per_second'])}/с; лог: {config.log_path}"
        )
    return 1 if data["failed"] or any(e["severity"] == "error" for e in data["source_errors"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
