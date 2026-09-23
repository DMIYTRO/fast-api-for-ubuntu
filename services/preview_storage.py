"""Versioned preview locations on the configured shared volume."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re


def preview_order_directory(root: Path, run_id: str, aggregate_id: str) -> Path:
    """Keep every order inside one run, even with unusual external IDs."""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Некорректный идентификатор запуска для превью.")
    label = re.sub(r"[^\w-]+", "_", aggregate_id, flags=re.UNICODE).strip("_")[:48] or "order"
    digest = hashlib.sha256(aggregate_id.encode("utf-8")).hexdigest()[:12]
    return Path(root) / run_id / f"{label}-{digest}"


def preview_run_directory(root: Path, run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Некорректный идентификатор запуска для превью.")
    return Path(root) / run_id
