"""Small dependency-free helpers shared by local performance probes."""

from __future__ import annotations

import math
import statistics
from typing import Iterable


def summarize(samples: Iterable[float]) -> dict[str, float | int]:
    values = sorted(float(value) for value in samples)
    if not values:
        raise ValueError("at least one sample is required")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("samples must be finite, non-negative durations")
    p95_index = max(0, math.ceil(0.95 * len(values)) - 1)
    return {
        "samples": len(values),
        "min_seconds": values[0],
        "median_seconds": statistics.median(values),
        "p95_seconds": values[p95_index],
        "max_seconds": values[-1],
    }
