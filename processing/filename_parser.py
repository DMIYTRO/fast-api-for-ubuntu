import re
from pathlib import Path

from .models import ParsedFilename


SIZE_RE = re.compile(r"_\((\d+(?:[.,]\d+)?)\s*[xх]\s*(\d+(?:[.,]\d+)?)\)_", re.IGNORECASE)
COLORS_RE = re.compile(r"_(\d+)-(\d+)_")
ORDER_RE = re.compile(r"_\((\d+)-(\d+)\)(?:_|$)")
SIDE_RE = re.compile(r"(?:_|-)(face|back)$", re.IGNORECASE)


def parse_filename(path: Path) -> ParsedFilename:
    """Extract the fields required for the first processing stage."""
    stem = path.stem
    size_match = SIZE_RE.search(stem)
    colors_match = COLORS_RE.search(stem)
    order_match = ORDER_RE.search(stem)
    side_match = SIDE_RE.search(stem)

    missing = []
    if not size_match:
        missing.append("размер в формате _(ШxВ)_")
    if not colors_match:
        missing.append("цветность, например 4-4 или 4-0")
    if not order_match:
        missing.append("номер клиента и заказа в формате _(клиент-заказ)_")
    # PDF side assignment is deliberately resolved by the order layer.  A
    # one-page 4-0 PDF is valid without a face/back suffix, while a one-page
    # 4-4 PDF still needs a complementary file (and therefore a side).
    # A one-sided raster (for example 1-0 / 4-0 / 5-0) is unambiguously the face.
    # Duplex raster files still require explicit face/back suffixes.
    if (
        not side_match
        and path.suffix.lower() != ".pdf"
        and (not colors_match or int(colors_match.group(2)) > 0)
    ):
        missing.append("сторона face или back")
    if missing:
        raise ValueError("в имени отсутствует: " + ", ".join(missing))

    return ParsedFilename(
        customer_id=order_match.group(1),
        order_id=order_match.group(2),
        width_mm=float(size_match.group(1).replace(",", ".")),
        height_mm=float(size_match.group(2).replace(",", ".")),
        front_colors=int(colors_match.group(1)),
        back_colors=int(colors_match.group(2)),
        side=side_match.group(1).lower() if side_match else "",
    )
