"""Measure fresh-process import, app construction, and FastAPI lifespan.

Examples:
  .venv/bin/python benchmarks/benchmark_startup.py --rounds 5
  .venv/bin/python benchmarks/benchmark_startup.py --scenario lifespan --rounds 3

Every sample uses a fresh temporary SQLite database. External integrations are
explicitly disabled; no production database or service is contacted.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from metrics import summarize


def _probe(scenario: str, db_path: Path, log_path: Path) -> dict[str, float]:
    code = r'''
import json, os, time
from pathlib import Path
started = time.perf_counter()
import server.database as database_module
migration_durations = []
original_upgrade = database_module.upgrade_database
def timed_upgrade(*args, **kwargs):
    migration_started = time.perf_counter()
    try:
        return original_upgrade(*args, **kwargs)
    finally:
        migration_durations.append(time.perf_counter() - migration_started)
database_module.upgrade_database = timed_upgrade
import control_panel
imported = time.perf_counter()
result = {"import_and_module_app_seconds": imported - started}
if migration_durations:
    result["alembic_upgrade_seconds"] = sum(migration_durations)
if os.environ["BENCH_SCENARIO"] == "create_app":
    from server.settings import Settings
    app_started = time.perf_counter()
    app = control_panel.create_app(settings=Settings.from_env())
    app_created = time.perf_counter()
    result["additional_create_app_seconds"] = app_created - app_started
    result["additional_alembic_upgrade_seconds"] = sum(migration_durations[1:])
elif os.environ["BENCH_SCENARIO"] == "lifespan":
    import asyncio
    async def measure_lifespan():
        started = time.perf_counter()
        async with control_panel.app.router.lifespan_context(control_panel.app):
            result["lifespan_ready_seconds"] = time.perf_counter() - started
        result["lifespan_shutdown_seconds"] = time.perf_counter() - started - result["lifespan_ready_seconds"]
    asyncio.run(measure_lifespan())
print("BENCH_RESULT=" + json.dumps(result))
'''
    env = os.environ.copy()
    env.update({
        "IMAGE_MAGIC_DATABASE_URL": f"sqlite:///{db_path}",
        "IMAGE_MAGIC_LOG_DIR": str(log_path),
        "IMAGE_MAGIC_SBORKA_ENABLED": "0",
        "IMAGE_MAGIC_PITSTOP_ENABLED": "0",
        "BENCH_SCENARIO": scenario,
    })
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    wall = time.perf_counter() - started
    marker = next(
        (line[len("BENCH_RESULT="):] for line in completed.stdout.splitlines()
         if line.startswith("BENCH_RESULT=")),
        None,
    )
    if marker is None:
        raise RuntimeError(f"benchmark child returned no result: {completed.stdout}\n{completed.stderr}")
    return {**json.loads(marker), "fresh_process_wall_seconds": wall}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--scenario", choices=("import", "create_app", "lifespan"), default="import")
    parser.add_argument("--database", choices=("warm", "cold"), default="warm",
                        help="warm reuses a pre-migrated DB; cold measures initial migrations")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")

    samples: dict[str, list[float]] = {}
    with tempfile.TemporaryDirectory(prefix="image-magic-startup-bench-") as temporary:
        root = Path(temporary)
        warm_db = root / "warm.sqlite3"
        if args.database == "warm":
            # Seed/migrate once outside measurements to represent normal service
            # restarts against an already initialized database.
            _probe("import", warm_db, root / "logs")
        for index in range(args.rounds):
            db_path = warm_db if args.database == "warm" else root / f"sample-{index}.sqlite3"
            result = _probe(args.scenario, db_path, root / "logs")
            for key, value in result.items():
                samples.setdefault(key, []).append(value)
    print(json.dumps({
        "scenario": args.scenario,
        "database_mode": args.database,
        "rounds": args.rounds,
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "measurements": {key: summarize(values) for key, values in samples.items()},
        "notes": [
            "fresh Python process per sample",
            "startup includes synchronous Alembic upgrade check in create_app()",
            "no hard timing threshold; compare on the same host and database type",
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
