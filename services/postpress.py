"""Normalize Sborka postpress text into a small, stable UI contract."""

from __future__ import annotations

import re
from typing import Any


_FOLD_PATTERNS = (
    ("half-fold", (r"\bпополам\b", r"\bpoplam\b", r"\bhalf(?:[ -]?fold)?\b")),
    ("c-fold", (r"\bнамотк", r"\bnamotka\b", r"\bc[ -]?fold\b")),
    ("z-fold", (r"\bгармошк", r"\bgarmoshka\b", r"\bz[ -]?fold\b")),
)
_COUNT_PATTERN = re.compile(
    r"(?:(?:сгиб|биг|фальц)\w*\s*[:—-]?\s*|\bfold\s*)(\d+)", re.I
)


def normalize_postpress(post_text: object) -> dict[str, Any] | None:
    """Return fold metadata, preserving unknown Sborka text for operators.

    A fold is safe to render only when its type is recognized.  Unknown text
    deliberately remains visible but is marked as requiring confirmation.
    """
    raw = str(post_text or "").strip()
    if not raw:
        return None
    normalized = raw.casefold()
    count_match = _COUNT_PATTERN.search(normalized)
    count = int(count_match.group(1)) if count_match else None
    operation = "Биг" if "биг" in normalized else "Сгиб"
    for fold_type, patterns in _FOLD_PATTERNS:
        if any(re.search(pattern, normalized, re.I) for pattern in patterns):
            return {
                "raw": raw,
                "fold": {
                    "type": fold_type,
                    "count": count,
                    "operation": operation,
                    "needs_confirmation": False,
                },
            }
    # Sborka often sends a one-fold operation without the word "пополам".
    # A single fold/crease still has an unambiguous central guide.
    if count == 1 and ("сгиб" in normalized or "биг" in normalized or "фальц" in normalized):
        return {
            "raw": raw,
            "fold": {
                "type": "half-fold",
                "count": 1,
                "operation": operation,
                "label": f"{operation}: 1",
                "needs_confirmation": False,
            },
        }
    if "сгиб" in normalized or "биг" in normalized or "фальц" in normalized or "fold" in normalized:
        return {
            "raw": raw,
            "fold": {
                "type": "unknown",
                "count": count,
                "operation": operation,
                "needs_confirmation": True,
            },
        }
    return {"raw": raw}
