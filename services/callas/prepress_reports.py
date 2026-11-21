"""Разбор XML-отчётов pdfToolbox для допечатных проверок: ICC, шрифты, разрешение, overprint.

Формат отчёта проверен на тестовом сервере (docs/callas-profiles.md): ``<results>`` содержит
``<hits><hit type page=PAGn>``, а справочники ``<resources><fonts|images|colorspaces>`` лежат
в ``<document>``. Здесь только чистые функции над уже разобранным корнем.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Iterator

from services.callas.report import _attr, _iter, _local
from services.overprint import is_technical_spot

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


# ----------------------------------------------------------------------------- overprint
#: Сколько срабатываний разбирается подробно; остальные только считаются (как блокирующие).
OVERPRINT_MAX_HITS = 200_000
OVERPRINT_SAMPLE_LIMIT = 5

KIND_PROCESS = "process"
KIND_WHITE = "white"
KIND_SPOT = "spot"
KIND_TECHNICAL = "technical_spot"
KIND_UNKNOWN = "unknown"

_PROCESS_COLORANTS = frozenset({"cyan", "magenta", "yellow", "black"})
_SPOT_TYPES = frozenset({"separation", "devicen", "nchannel"})
_HIT_LABELS = {
    "fill": "Заливка",
    "stroke": "Обводка",
    "textfill": "Текст",
    "textstroke": "Обводка текста",
    "textfillstroke": "Текст",
    "image": "Изображение",
    "shading": "Градиент",
    "shfill": "Градиент",
}
WHITE_OVERPRINT_NOTE = "белый объект с overprint пропадёт при печати"


def _colorspaces(root: ET.Element) -> dict[str, dict[str, Any]]:
    """id → ``{type, channels, base, names, profile}`` из ``resources/colorspaces``.

    Separation: прямой ``<name>``; DeviceN/NChannel: ``alternate_colorants/colorant/name``;
    ICCBased: ``<name>`` — имя ICC-профиля (не краска); Indexed: ``base_colorspace``.
    """
    table: dict[str, dict[str, Any]] = {}
    for space in _iter(root, "colorspace"):
        space_id = _attr(space, "id")
        kind = (_attr(space, "type") or "").strip()
        if not space_id or not kind:
            continue  # ссылка alternate_colorspace внутри другого элемента
        direct = [
            (child.text or "").strip() for child in space if _local(child.tag) == "name"
        ]
        colorants = [
            (name.text or "").strip()
            for colorant in _iter(space, "colorant")
            for name in colorant
            if _local(name.tag) == "name"
        ]
        lowered = kind.lower()
        try:
            channels = int(_attr(space, "channels") or 0)
        except ValueError:
            channels = 0
        table[space_id] = {
            "type": kind,
            "channels": channels,
            "base": _attr(space, "base_colorspace"),
            "names": (colorants or direct) if lowered in _SPOT_TYPES else [],
            "profile": direct[0] if lowered == "iccbased" and direct else None,
        }
    return table


def _image_colorspaces(root: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for image in _iter(root, "image"):
        image_id, space = _attr(image, "id"), _attr(image, "colorspace")
        if image_id and space and _attr(image, "treated_as_mask") != "1":
            result[image_id] = space
    return result


def _values(text: str | None) -> list[float] | None:
    if not text:
        return None
    try:
        return [float(part) for part in text.replace(",", "/").split("/") if part.strip()]
    except ValueError:
        return None


def _resolve(spaces: dict[str, dict[str, Any]], space_id: str | None) -> tuple[dict[str, Any] | None, bool]:
    """Цветовое пространство с учётом Indexed → base; второй элемент — было ли Indexed."""
    space = spaces.get(space_id or "")
    indexed = False
    for _ in range(4):
        if space is None or space["type"].lower() != "indexed":
            break
        indexed = True
        space = spaces.get(space.get("base") or "")
    return space, indexed


def _percent(value: float) -> str:
    return f"{round(value * 100):g}"


def _describe_values(kind: str, values: list[float] | None, channels: int) -> tuple[str, bool]:
    """(текст цвета, белый ли) для процессных пространств."""
    lowered = kind.lower()
    if values is None:
        return "", False
    if lowered in {"devicecmyk"} or (lowered == "iccbased" and channels == 4 and len(values) == 4):
        if len(values) == 4:
            text = " ".join(f"{letter}{_percent(value)}" for letter, value in zip("CMYK", values) if value > 0)
            white = all(value <= 0 for value in values)
            return (text or "C0 M0 Y0 K0"), white
    if lowered in {"devicergb", "calrgb"} or (lowered == "iccbased" and channels == 3 and len(values) == 3):
        if len(values) == 3:
            text = "RGB " + "/".join(f"{round(value * 255):g}" for value in values)
            return text, all(value >= 1 for value in values)
    if lowered in {"devicegray", "calgray"} or (lowered == "iccbased" and channels == 1 and len(values) == 1):
        if len(values) == 1:
            return f"Gray {_percent(values[0])}%", values[0] >= 1
    if lowered == "lab" and values:
        return "Lab " + "/".join(f"{value:g}" for value in values), values[0] >= 99.5
    return "/".join(f"{value:g}" for value in values), False


def _classify_color(
    spaces: dict[str, dict[str, Any]],
    space_id: str | None,
    values_text: str | None,
    ignore: tuple[str, ...],
) -> tuple[str, str, list[str]]:
    """(вид, описание цвета, имена красок) одного цвета объекта."""
    space, indexed = _resolve(spaces, space_id)
    if space is None:
        return KIND_UNKNOWN, "цвет не определён", []
    kind = space["type"]
    values = None if indexed else _values(values_text)
    names = [name for name in space["names"] if name]
    if kind.lower() in _SPOT_TYPES:
        label = "+".join(f"«{name}»" for name in names) or "без имени"
        tints = ""
        if values:
            tints = " " + "/".join(_percent(value) for value in values) + "%"
        text = f"{label}{tints} ({kind})"
        if names and all(is_technical_spot(name, ignore) for name in names):
            return KIND_TECHNICAL, text, names
        if values and all(value <= 0 for value in values):
            return KIND_WHITE, text, names
        if names and all(name.casefold() in _PROCESS_COLORANTS for name in names):
            return KIND_PROCESS, text, names
        return KIND_SPOT, text, names
    color, white = _describe_values(kind, values, space["channels"])
    where = kind if not space.get("profile") else f"ICC «{space['profile']}»"
    text = f"{color} ({where})" if color else where
    if kind.lower() == "pattern":
        return KIND_UNKNOWN, text, []
    return (KIND_WHITE if white else KIND_PROCESS), text, []


def _hit_colors(hit: ET.Element, image_spaces: dict[str, str]) -> list[tuple[str | None, str | None]]:
    """Цвета, к которым относится overprint объекта: заливка и/или обводка.

    callas отмечает объект, только если у его вида операции (заливка/обводка) включён
    overprint (проверено: заливка при OP=true, op=false не попадает в отчёт). Атрибуты цвета —
    в ``gstate`` или ``textstate`` (у текста); у изображения — пространство самого растра.
    """
    hit_type = (_attr(hit, "type") or "").lower()
    states = [child for child in hit if _local(child.tag) in {"gstate", "textstate"}]
    merged: dict[str, str] = {}
    for state in states:
        for key, value in state.attrib.items():
            merged.setdefault(key.rsplit("}", 1)[-1].lower(), value)
    if hit_type == "image":
        image_state = next((child for child in hit if _local(child.tag) == "imagestate"), None)
        image_id = _attr(image_state, "image_id") if image_state is not None else None
        if image_id and image_id in image_spaces:
            return [(image_spaces[image_id], None)]
        # маска-трафарет красится текущим цветом заливки
        return [(merged.get("fill_colorspace"), merged.get("fill_colorant_values"))]
    sides = []
    if "stroke" in hit_type:
        sides.append("stroke")
    if "fill" in hit_type or "stroke" not in hit_type:
        sides.append("fill")
    active = [side for side in sides if merged.get(f"overprint_for_{side}") != "0"] or sides
    return [
        (merged.get(f"{side}_colorspace"), merged.get(f"{side}_colorant_values")) for side in active
    ]


def overprint_report(root: ET.Element, ignore_spots: Iterable[str] = ()) -> dict[str, Any]:
    """Сводка отчёта «Overprint - any object»: число объектов с overprint по видам и страницам.

    Каждый ``<hit>`` — объект с overprint. Вид по цветовому пространству цвета объекта:
    ``technical_spot`` (Separation/DeviceN, все краски — технические, см.
    ``services.overprint.is_technical_spot``), ``white`` (нулевые краски CMYK/спота, RGB 255/255/255,
    Gray 100%: объект пропадёт), ``process``, ``spot`` (нетехнический спот) и ``unknown``
    (пространство не указано, например у части текстов). Блокирует всё, кроме ``technical_spot``.
    Хранятся только счётчики и до ``OVERPRINT_SAMPLE_LIMIT`` образцов, не все срабатывания.
    """
    ignore = tuple(ignore_spots)
    spaces = _colorspaces(root)
    image_spaces = _image_colorspaces(root)
    by_kind = {KIND_PROCESS: 0, KIND_WHITE: 0, KIND_SPOT: 0, KIND_TECHNICAL: 0, KIND_UNKNOWN: 0}
    per_page: dict[int, dict[str, int]] = {}
    blocking_samples: dict[str, None] = {}
    white_samples: dict[str, None] = {}
    technical_samples: dict[str, None] = {}
    colorants: dict[str, None] = {}
    technical_colorants: dict[str, None] = {}
    count = processed = 0
    for hit in _hits(root):
        count += 1
        page = _page_number(_attr(hit, "page"))
        if processed >= OVERPRINT_MAX_HITS:
            kind, text, names = KIND_UNKNOWN, "", []
        else:
            processed += 1
            hit_type = (_attr(hit, "type") or "").lower()
            classified = [
                _classify_color(spaces, space_id, values, ignore)
                for space_id, values in _hit_colors(hit, image_spaces)
            ]
            # объект блокирует, если хоть один его цвет с overprint не технический
            blocking = [item for item in classified if item[0] != KIND_TECHNICAL]
            kind, text, names = (blocking or classified)[0]
            label = _HIT_LABELS.get(hit_type, _attr(hit, "type") or "Объект")
            text = f"{label} {text}" + (f", стр. {page}" if page else "")
            for item in classified:
                target = technical_colorants if item[0] == KIND_TECHNICAL else colorants
                for name in item[2]:
                    target.setdefault(name, None)
        by_kind[kind] += 1
        bucket = per_page.setdefault(page or 0, {"blocking": 0, "technical": 0})
        bucket["technical" if kind == KIND_TECHNICAL else "blocking"] += 1
        if not text:
            continue
        if kind == KIND_TECHNICAL:
            if len(technical_samples) < OVERPRINT_SAMPLE_LIMIT:
                technical_samples.setdefault(text, None)
        elif kind == KIND_WHITE:
            if len(white_samples) < OVERPRINT_SAMPLE_LIMIT:
                white_samples.setdefault(f"{text} — {WHITE_OVERPRINT_NOTE}", None)
        elif len(blocking_samples) < OVERPRINT_SAMPLE_LIMIT:
            blocking_samples.setdefault(text, None)
    technical = by_kind[KIND_TECHNICAL]
    return {
        "count": count,
        "pages": sorted(page for page in per_page if page),
        "by_kind": by_kind,
        "blocking_count": count - technical,
        "blocking_pages": sorted(page for page, value in per_page.items() if page and value["blocking"]),
        "technical_count": technical,
        "technical_pages": sorted(page for page, value in per_page.items() if page and value["technical"]),
        "per_page": {str(page): value for page, value in sorted(per_page.items()) if page},
        "colorants": list(colorants),
        "technical_colorants": list(technical_colorants),
        # белые объекты первыми: это самый опасный случай
        "samples": [*white_samples, *blocking_samples][:OVERPRINT_SAMPLE_LIMIT],
        "technical_samples": list(technical_samples)[:OVERPRINT_SAMPLE_LIMIT],
        "processed_hits": processed,
        "max_hits": OVERPRINT_MAX_HITS,
        "truncated": count > processed,
    }
