"""Normalize Sborka postpress text into a small, stable UI contract."""

from __future__ import annotations

import html
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


def normalize_customer_comment(value: object) -> str | None:
    """Full customer comment from Sborka ``comment_client`` as plain text.

    Every line is kept (line breaks are unified to ``\\n``); only blank space around
    the whole text is dropped.  Anything that is not a non-empty string means
    "no comment".
    """
    if not isinstance(value, str):
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text or None


def normalize_postpress(post_text: object) -> dict[str, Any] | None:
    """Return fold metadata and the other postpress operations.

    A fold is safe to render only when its type is recognized.  Unknown text
    deliberately remains visible but is marked as requiring confirmation.
    """
    raw = str(post_text or "").strip()
    if not raw:
        return None
    result = _normalize_fold(raw)
    operations = postpress_operations(raw)
    if operations:
        result["operations"] = operations
    return result


def with_postpress_operations(postpress: dict[str, Any]) -> dict[str, Any]:
    """Add ``operations`` to postpress stored before they were parsed."""
    if "operations" in postpress:
        return postpress
    operations = postpress_operations(postpress.get("raw"))
    return {**postpress, "operations": operations} if operations else postpress


# Parts of ``post_text`` that are not postpress or are shown elsewhere:
# kits have their own badge, design work is not a finishing operation, and
# folds/creases are the fold indicator.
_SKIP_PART = re.compile(r"^(?:комплект\b|(?:до|раз)работка макета\b)", re.I)
_FOLD_PART = re.compile(r"сгиб|биг|фальц|\bfold\b", re.I)
_ORIENTATION = {"вертикально": "вертикально", "горизонтально": "горизонтально"}
_DIAMETER = re.compile(r"(?:[ø⌀∅]|диаметр[^:\d]*)\s*[:\-–—]?\s*(\d+(?:[.,]\d+)?)\s*мм", re.I)
_LEADING_COUNT = re.compile(r"^(\d+)\s*(?:шт\.?)?(?=$|,)")
_LAMINATION_FINISHES = (
    (re.compile(r"soft\s*touch", re.I), "soft touch"),
    (re.compile(r"anti\s*scuff", re.I), "antiscuff"),
    (re.compile(r"уф-?лак", re.I), "УФ-лак"),
    (re.compile(r"матов", re.I), "мат"),
    (re.compile(r"глянц", re.I), "глянец"),
)
_GENERIC_KINDS = (
    (re.compile(r"перфорац", re.I), "perforation"),
    (re.compile(r"люверс", re.I), "grommet"),
    (re.compile(r"порезк|вырубк|высечк", re.I), "cutting"),
    (re.compile(r"кашировк", re.I), "mounting"),
    (re.compile(r"скоб", re.I), "staple"),
    (re.compile(r"склейк|клеен", re.I), "gluing"),
)


def postpress_operations(post_text: object) -> list[dict[str, Any]]:
    """Split Sborka ``post_text`` into non-fold operations for the order card.

    Each item keeps its source text in ``text``; ``count`` and ``diameter_mm``
    are set only when Sborka states them explicitly.
    """
    raw = html.unescape(str(post_text or ""))
    operations: list[dict[str, Any]] = []
    pending_orientation: str | None = None
    for part in raw.split(";"):
        text = " ".join(part.split())
        if not text:
            continue
        key = text.casefold()
        if key in _ORIENTATION:
            pending_orientation = _ORIENTATION[key]
            continue
        if _SKIP_PART.search(text) or _FOLD_PART.search(text):
            continue
        if re.search(r"ламін|ламин", key) and "конверт" in key and "мікрон" not in key and "мкм" not in key:
            previous = next((op for op in reversed(operations) if op["kind"] == "lamination"), None)
            if previous is not None:
                previous["detail"] = ", ".join(filter(None, [previous.get("detail"), "в конверт"]))
                previous["text"] = f"{previous['text']}; {text}"
                continue
        operation = _parse_operation(text)
        if pending_orientation:
            operation["detail"] = ", ".join(filter(None, [operation.get("detail"), pending_orientation]))
            pending_orientation = None
        operations.append(operation)
    return operations


def _parse_operation(text: str) -> dict[str, Any]:
    key = text.casefold()
    name, _, value = text.partition(":")
    name, value = name.strip(), value.strip()
    if "скруглени" in key or "закруглени" in key:
        operation: dict[str, Any] = {"kind": "rounding", "label": "Скругление углов", "text": text}
        count = _int_value(value) if "скруглени" in name.casefold() else None
        if count is not None:
            operation["count"] = count
        if "порезк" in name.casefold():
            operation["detail"] = name.casefold()
        return operation
    if "сверлени" in key:
        operation = {"kind": "drilling", "label": "Сверление", "text": text}
        count_match = _LEADING_COUNT.match(value)
        if count_match:
            operation["count"] = int(count_match.group(1))
        diameter = _DIAMETER.search(value)
        if diameter:
            operation["diameter_mm"] = float(diameter.group(1).replace(",", "."))
        return operation
    if re.search(r"ламін|ламин", key):
        return _lamination(text)
    kind = next((kind for pattern, kind in _GENERIC_KINDS if pattern.search(name)), "other")
    operation = {"kind": kind, "label": name[:1].upper() + name[1:], "text": text}
    if kind == "grommet":
        diameter = _DIAMETER.search(value)
        if diameter:
            operation["diameter_mm"] = float(diameter.group(1).replace(",", "."))
    count_match = _LEADING_COUNT.match(value)
    if count_match:
        operation["count"] = int(count_match.group(1))
        rest = value[count_match.end():].lstrip(" ,")
        if rest:
            operation["detail"] = rest
    elif value:
        operation["detail"] = value
    return operation


def _lamination(text: str) -> dict[str, Any]:
    key = text.casefold()
    finish = next((label for pattern, label in _LAMINATION_FINISHES if pattern.search(text)), None)
    if finish == "мат" and "ritrama" in key:
        finish = "мат Ritrama"
    if "двох сторін" in key or "двох боків" in key or "двух сторон" in key:
        sides = "2 стороны"
    elif "лицьов" in key or "лицев" in key:
        sides = "лицо"
    else:
        sides = None
    if "конверт" in key:
        sides = ", ".join(filter(None, [sides, "в конверт"]))
    microns = re.search(r"(\d+)\s*(?:мікрон|мкм|микрон)", key)
    operation: dict[str, Any] = {"kind": "lamination", "label": "Ламинация", "text": text}
    detail = ", ".join(filter(None, [finish, sides, f"{microns.group(1)} мкм" if microns else None]))
    if detail:
        operation["detail"] = detail
    return operation


def _int_value(value: str) -> int | None:
    return int(value) if re.fullmatch(r"\d+", value.strip()) else None


def _normalize_fold(raw: str) -> dict[str, Any]:
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
