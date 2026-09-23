"""Copy persisted legacy previews into a run-scoped Share and update their links.

Dry-run is the default. Run with the web worker stopped when applying because
save_run rewrites a run snapshot. Original previews are retained for rollback.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys

from sqlalchemy.engine import make_url
from sqlalchemy import func, select

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.preview_generator import detailed_preview_path
from server.database import Database
from server.models import CheckRun, OrderResult
from services.preview_storage import preview_order_directory, preview_run_directory
from services.sql_repository import SqlRunRepository


ACTIVE_STATUSES = {"queued", "running", "waiting_confirmation", "cancelling"}


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_verified(source: Path, target: Path) -> None:
    expected = file_digest(source)
    if target.is_file():
        if file_digest(target) != expected:
            raise ValueError(f"Превью уже существует с другим содержимым: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.copying")
    try:
        shutil.copy2(source, temporary)
        if file_digest(temporary) != expected:
            raise IOError(f"Контрольная сумма превью не совпала: {source}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_run(
    repository: SqlRunRepository, run_id: str, preview_root: Path, *,
    apply: bool = False, ambiguous_custom_keys: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    run = repository.get_run(run_id)
    if run is None:
        raise KeyError(run_id)
    if run["status"] in ACTIVE_STATUSES:
        raise ValueError(f"Завершите обработку запуска {run_id} перед переносом превью.")
    input_root = Path(run["options"]["input_path"]).resolve()
    source_count = detailed_count = missing_count = custom_count = return_count = ambiguous_count = 0
    order_ids = {str(order.get("order_id") or "") for order in (run.get("orders") or {}).values()}
    for order in (run.get("orders") or {}).values():
        aggregate_id = f"{order.get('customer_id') or ''}:{order.get('order_id') or ''}"
        destination_dir = preview_order_directory(preview_root, run_id, aggregate_id) / "Legacy"
        paths = set(order.get("preview_paths") or [])
        for file in order.get("files") or []:
            paths.update(file.get("preview_paths") or [])
            if file.get("preview_path"):
                paths.add(file["preview_path"])
        replacements: dict[str, str] = {}
        for raw_path in paths:
            source = Path(raw_path).resolve()
            try:
                relative = source.relative_to(input_root)
            except ValueError:
                # Already in the target store, or deliberately external.
                continue
            if not source.is_file():
                missing_count += 1
                continue
            folder = sha256(str(relative).encode("utf-8")).hexdigest()[:12]
            target = destination_dir / folder / source.name
            if apply:
                copy_verified(source, target)
            replacements[str(raw_path)] = str(target)
            source_count += 1
            detailed = detailed_preview_path(source)
            if detailed.is_file():
                if apply:
                    copy_verified(detailed, detailed_preview_path(target))
                detailed_count += 1
        if not replacements:
            continue
        order["preview_paths"] = [replacements.get(path, path) for path in order.get("preview_paths") or []]
        for file in order.get("files") or []:
            if file.get("preview_path"):
                file["preview_path"] = replacements.get(file["preview_path"], file["preview_path"])
            if file.get("preview_paths"):
                file["preview_paths"] = [replacements.get(path, path) for path in file["preview_paths"]]
    for stage in ("Custom", "Return"):
        legacy_dir = input_root / "Previews" / stage
        if not legacy_dir.is_dir():
            continue
        for source in legacy_dir.iterdir():
            if not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            matching_order = next(
                (order_id for order_id in order_ids if source.stem == f"{order_id}_return-preview"),
                None,
            )
            if matching_order is None:
                continue
            if ambiguous_custom_keys and (str(input_root), matching_order) in ambiguous_custom_keys:
                ambiguous_count += 1
                continue
            target = preview_run_directory(preview_root, run_id) / stage / source.name
            if apply:
                copy_verified(source, target)
            if stage == "Custom":
                custom_count += 1
            else:
                return_count += 1
    if apply and missing_count:
        raise FileNotFoundError(
            f"У запуска {run_id} отсутствуют {missing_count} превью; ссылки в БД не изменены."
        )
    if apply and (source_count or custom_count or return_count):
        run["options"]["preview_root"] = str(preview_root.resolve())
        repository.save_run(run)
    return {"previews": source_count, "detailed": detailed_count, "custom": custom_count,
            "return": return_count, "ambiguous": ambiguous_count, "missing": missing_count}


def ambiguous_custom_keys(repository: SqlRunRepository) -> set[tuple[str, str]]:
    """A shared folder/order number cannot identify which historical run owns a custom file."""
    with repository._session_factory() as session:
        rows = session.execute(
            select(CheckRun.input_path, OrderResult.order_id)
            .join(OrderResult, OrderResult.run_id == CheckRun.id)
            .group_by(CheckRun.input_path, OrderResult.order_id)
            .having(func.count(func.distinct(CheckRun.id)) > 1)
        ).all()
    return {(str(Path(path).resolve()), str(order_id)) for path, order_id in rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True, help="URL той же БД, что использует сервис")
    parser.add_argument("--run-id", action="append", dest="run_ids")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Только отчёт; режим по умолчанию")
    mode.add_argument("--apply", action="store_true", help="Копировать файлы и обновить ссылки в БД")
    args = parser.parse_args()
    preview_root = args.preview_root.expanduser().resolve()
    database = Database(args.database_url)
    try:
        print(f"БД: {make_url(args.database_url).render_as_string(hide_password=True)}")
        repository = SqlRunRepository(database.session_factory, recover_interrupted=False)
        run_ids = args.run_ids or [item["id"] for item in repository.list_runs(include_orders=False)]
        ambiguous = ambiguous_custom_keys(repository)
        for run_id in run_ids:
            result = migrate_run(repository, run_id, preview_root, apply=args.apply,
                                 ambiguous_custom_keys=ambiguous)
            print(f"{run_id}: {result['previews']} превью, {result['detailed']} крупных, "
                  f"{result['custom']} пользовательских, {result['return']} возвратов, "
                  f"{result['ambiguous']} неоднозначны, {result['missing']} отсутствуют")
        print("Применено. Исходные файлы сохранены." if args.apply else "Проверка завершена; файлы и БД не изменены.")
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
