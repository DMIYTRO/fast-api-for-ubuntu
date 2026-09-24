"""Non-blocking maintenance for old run event history."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any


logger = logging.getLogger("image_magic.event_retention")
RETENTION_DAYS = 90
BATCH_SIZE = 500
MAX_BATCHES_PER_CYCLE = 10
INITIAL_DELAY_SECONDS = 300
CYCLE_INTERVAL_SECONDS = 24 * 60 * 60


def _archive_cycle(repository: Any, cutoff: datetime) -> int:
    archive = getattr(repository, "archive_old_terminal_events", None)
    if not callable(archive):
        return 0
    total = 0
    for _ in range(MAX_BATCHES_PER_CYCLE):
        moved = archive(cutoff, batch_size=BATCH_SIZE)
        total += moved
        if moved < BATCH_SIZE:
            break
    return total


async def run_event_retention(
    repository: Any,
    stop: asyncio.Event,
    *,
    retention_days: int = RETENTION_DAYS,
    initial_delay: float = INITIAL_DELAY_SECONDS,
    interval: float = CYCLE_INTERVAL_SECONDS,
) -> None:
    """Archive bounded batches off the event loop, then sleep until next cycle."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=initial_delay)
        return
    except TimeoutError:
        pass

    while not stop.is_set():
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        try:
            archived = await asyncio.to_thread(_archive_cycle, repository, cutoff)
            if archived:
                logger.info(
                    "run_events.archived count=%d retention_days=%d",
                    archived,
                    retention_days,
                )
        except Exception:
            logger.exception("run_events.archive_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
