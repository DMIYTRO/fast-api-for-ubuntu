"""Разбор XML-отчётов pdfToolbox для допечатных проверок: ICC, шрифты, разрешение.

Формат отчёта проверен на тестовом сервере (docs/callas-profiles.md): ``<results>`` содержит
``<hits><hit type page=PAGn>``, а справочники ``<resources><fonts|images|colorspaces>`` лежат
в ``<document>``. Здесь только чистые функции над уже разобранным корнем.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterator

from services.callas.report import _attr, _iter, _local

# Допуск на округление пикселей: 499.745 и 501.015 бывают у «ровных» 500 ppi.
PPI_TOLERANCE = 0.5
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def _page_number(value: str | None) -> int | None:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else None


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if child is not element and _local(child.tag) == name:
            return child
    return None


def _hits(root: ET.Element) -> Iterator[ET.Element]:
    for results in _iter(root, "results"):
        for hit in _iter(results, "hit"):
            yield hit


def icc_pages(root: ET.Element) -> list[int]:
    """Страницы с объектами в ICC/Lab/калиброванном цвете (hit профиля «List objects using ICC…»)."""
    pages = {_page_number(_attr(hit, "page")) for hit in _hits(root)}
    return sorted(page for page in pages if page)


def clean_font_name(name: str) -> str:
    return _SUBSET_PREFIX.sub("", name or "").strip()


def fonts_report(root: ET.Element) -> dict[str, Any]:
    """Уникальные шрифты из отчёта «Warn Fonts» (не срабатывания): тип, имя, встроен ли."""
    resources: dict[str, dict[str, Any]] = {}
    for font in _iter(root, "font"):
        font_id = _attr(font, "id")
        if not font_id:
            continue
        name_element = _child(font, "name")
        resources[font_id] = {
            "type": _attr(font, "type") or "",
            "name": (name_element.text or "").strip() if name_element is not None else "",
            "embedded": _attr(font, "embedded") == "1",
        }
    unique: dict[str, dict[str, Any]] = {}
    for hit in _hits(root):
        state = _child(hit, "textstate")
        font_id = _attr(state, "font_id") if state is not None else None
        trigger = _child(hit, "trigger")
        value = _child(trigger, "value") if trigger is not None else None
        detail = _child(trigger, "detail") if trigger is not None else None
        known = resources.get(font_id or "", {})
        name = known.get("name") or ((detail.text or "").strip() if detail is not None else "")
        kind = known.get("type") or ((value.text or "").strip() if value is not None else "")
        key = font_id or f"{kind}:{name}"
        item = unique.setdefault(
            key,
            {
                "name": clean_font_name(name) or "(без имени)",
                "type": kind or "?",
                "embedded": known.get("embedded") if known else None,
                "pages": set(),
                "visible": False,
            },
        )
        page = _page_number(_attr(hit, "page"))
        if page:
            item["pages"].add(page)
        if _attr(hit, "type") != "TextInvisible":
            item["visible"] = True
    fonts = [
        {
            "name": item["name"],
            "type": item["type"],
            "embedded": item["embedded"],
            "pages": sorted(item["pages"]),
            "invisible_only": not item["visible"],
        }
        for item in sorted(unique.values(), key=lambda value: (value["name"].lower(), value["type"]))
    ]
    pages = sorted({page for item in fonts for page in item["pages"]})
    return {
        "count": len(fonts),
        "fonts": fonts,
        "pages": pages,
        "not_embedded": sum(1 for item in fonts if item["embedded"] is False),
    }


def high_ppi_report(root: ET.Element, limit: float = 500.0) -> dict[str, Any]:
    """Растры с разрешением выше ``limit`` ppi (по max(v_ppi, h_ppi)); 1-бит отдельно."""
    images: dict[str, dict[str, Any]] = {}
    for image in _iter(root, "image"):
        image_id = _attr(image, "id")
        if image_id:
            images[image_id] = {
                "bits": _attr(image, "bits_per_color_component"),
                "mask": _attr(image, "treated_as_mask") == "1",
            }
    regular: dict[tuple, float] = {}
    one_bit: dict[tuple, float] = {}
    for hit in _hits(root):
        if _attr(hit, "type") not in (None, "Image"):
            continue
        state = _child(hit, "imagestate")
        if state is None:
            continue
        try:
            ppi = max(float(_attr(state, "v_ppi") or 0), float(_attr(state, "h_ppi") or 0))
        except ValueError:
            continue
        if ppi <= limit + PPI_TOLERANCE:
            continue
        image_id = _attr(state, "image_id") or ""
        known = images.get(image_id, {})
        key = (
            _page_number(_attr(hit, "page")),
            image_id,
            _attr(hit, "llx"), _attr(hit, "lly"), _attr(hit, "urx"), _attr(hit, "ury"),
        )
        bucket = one_bit if known.get("bits") == "1" or known.get("mask") else regular
        bucket[key] = max(ppi, bucket.get(key, 0.0))

    def summarize(bucket: dict[tuple, float]) -> dict[str, Any]:
        return {
            "count": len(bucket),
            "pages": sorted({key[0] for key in bucket if key[0]}),
            "max_ppi": round(max(bucket.values()), 1) if bucket else None,
        }

    return {"limit": limit, **summarize(regular), "one_bit": summarize(one_bit)}
