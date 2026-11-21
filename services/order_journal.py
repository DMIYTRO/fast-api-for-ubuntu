"""Журнал заказа: все найденные проблемы, автоматические и ручные изменения, решения.

Чистая проекция (без БД и ввода-вывода) итогового состояния заказа, его событий
(``run_events`` + ``archived_run_events``), действий оператора (``order_actions``) и
ревизий PDF (``pdf_revisions``) в один версионный документ. Это контракт для
будущей внешней API: схема описана в ``docs/order-journal.md``. Новые поля
добавляются без смены версии; удаление или смена смысла поля — новая ``version``.

Правила:

* ничего не теряется молча: неизвестное действие обработки попадает в журнал
  со своим сырым ``kind``, а сбой разбора одного блока пишет предупреждение в лог и
  пропускает только этот блок;
* находки, добавленные самим исправлением Callas (``hidden_by_baseline``), и
  понижённые политикой проверки (``policy_demoted``) остаются в журнале с флагами
  в ``data``: интерфейс может показать их приглушённо, API видит всё;
* проблема, найденная первой проверкой (снимок события ``order.completed``) и
  отсутствующая в итоговом состоянии, остаётся в журнале с ``outcome: "resolved"``;
* в ``data`` нет абсолютных путей (только имена файлов) и сетевых ответов.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any

from config.callas_plans import CALLAS_PLANS
from services.check_policy import classify
from services.issue_groups import (
    _LOCAL_RULES,
    SOURCE_LOCAL,
    SOURCE_PITSTOP,
    issue_groups_for_order_dto,
    pitstop_severity,
)

logger = logging.getLogger("image_magic.order_journal")

SCHEMA = "image-magic.order-journal"
VERSION = 1

KIND_ISSUE = "issue"
KIND_AUTO = "auto_change"
KIND_OPERATOR = "operator_change"
KIND_DECISION = "decision"

#: Типы событий, которые читает журнал (загрузчик фильтрует по ним в SQL).
JOURNAL_EVENT_TYPES: tuple[str, ...] = (
    "order.completed",
    "order.callas_fix.started",
    "order.callas_fix.finished",
    "order.callas_fix.accepted",
    "order.callas_fix.rejected",
    "order.callas_fix.reset",
    "order.callas_fix.save_failed",
    "order.callas_source_fix",
    "order.correction_confirmed",
    "order.correction_rejected",
    "order.pdf_revision",
    "order.forced_print",
    "order.file_transition_rolled_back",
)

#: Локальные коды нарушения правила страниц (жёсткая ошибка, см. CLAUDE.md).
PAGE_RULE_CODES = frozenset({
    "pdf_too_many_pages",
    "order_too_many_pages",
    "two_page_pdf_one_sided",
    "duplex_pages_required",
    "two_page_pdf_extra_sides",
    "multiple_two_page_pdf",
})

#: Stable local codes; any other local message is ``local:other`` (text in title/samples).
_KNOWN_LOCAL_CODES = frozenset(rule[0] for rule in _LOCAL_RULES)

SIZE_FIX_TITLES = {
    "bleed": "Добавлены поля вылета (Callas)",
    "reveal": "Открыты поля вылета: страница расширена до формата без масштабирования",
    "stretch": "Макет растянут до формата (в допустимых пределах)",
    "shrink": "Макет пропорционально уменьшен до формата",
    "fill": "Макет подогнан к формату: пропорциональное увеличение и обрезка",
    "canvas": "Белые поля или обрезка без масштабирования",
    "callas": "Размер исправлен Callas",
}

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}
_OUTCOME_TEXT = {
    "accepted": "принято оператором",
    "rejected": "отклонено оператором",
    "failed": "не выполнено",
    "ready": "готово, ожидает решения оператора",
    "processing": "выполняется",
}
_FLAG_LABELS = {
    "light_areas": "Светлые участки",
    "overflow": "Перелив",
    "underflow": "Низкая средняя красочность",
}
# Revision event and Callas decision are written by one request a few ms apart.
_REVISION_LINK_SECONDS = 15.0
_TRAILING_COUNT_RE = re.compile(r"\((\d+)(\s*[x×])(?=[\s)])([^()]*)\)([.\s]*)$", re.IGNORECASE)
_CYRILLIC_START_RE = re.compile(r"^[А-Яа-яЁё]")
# An absolute path inside free text (error messages): keep only the last segment.
_EMBEDDED_PATH_RE = re.compile(r"(?<![\w.~])/(?:[^/\s'\"]+/)+([^/\s'\"]*)")
_PATH_KEYS = frozenset({"path", "pdf_path", "baseline_pdf", "content_path", "source_path", "preview_path"})


# --------------------------------------------------------------------------- utils


def _entry(
    kind: str,
    code: str,
    source: str,
    title: str,
    *,
    severity: str | None = None,
    detail: str | None = None,
    file: str | None = None,
    pages: Iterable[Any] | None = None,
    count: int | None = None,
    at: str | None = None,
    outcome: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "code": _scrub(code),
        "source": source,
        "severity": severity,
        "title": _scrub(title),
        "detail": _scrub(detail) if detail else None,
        "file": file or None,
        "pages": _pages(pages),
        "count": count if isinstance(count, int) and not isinstance(count, bool) else None,
        "at": at,
        "outcome": outcome,
        "data": _clean({key: value for key, value in (data or {}).items() if value is not None}),
    }


def _pages(values: Iterable[Any] | None) -> list[int] | None:
    if not values:
        return None
    pages = sorted({
        int(value) for value in values
        if (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, str) and value.isdigit())
    })
    return pages or None


def _num(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _name(value: Any) -> str | None:
    text = str(value or "").strip()
    return PurePath(text.replace("\\", "/")).name if text else None


def _scrub(text: str) -> str:
    return _EMBEDDED_PATH_RE.sub(lambda match: match.group(1), str(text))


def _clean(value: Any, key: str | None = None) -> Any:
    """JSON-safe copy without absolute paths (file names only)."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v, str(k)) for k, v in value.items() if not str(k).startswith("__")}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(item) for item in value]
    if isinstance(value, PurePath):
        return value.name
    if isinstance(value, str):
        return _name(value) if key in _PATH_KEYS else _scrub(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _mm(value: Any) -> str | None:
    values = _list(value)
    if len(values) < 2:
        return None
    try:
        return "×".join(f"{round(float(item), 2):g}" for item in values[:2])
    except (TypeError, ValueError):
        return None


def _size_range(from_mm: Any, to_mm: Any) -> str | None:
    before, after = _mm(from_mm), _mm(to_mm)
    return f"{before} → {after} мм" if before and after else None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else None


def _plan_title(plan: Any) -> str:
    key = str(plan or "")
    known = CALLAS_PLANS.get(key)
    return known.title if known else (key or "исправление")


def _guard(label: str, builder: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    try:
        return builder()
    except Exception:  # noqa: BLE001 - one bad block must not hide the rest
        logger.warning("order_journal.block_failed block=%s", label, exc_info=True)
        return []


def _file_names(order: Mapping[str, Any]) -> dict[str, str]:
    """``file_result_id``/``id`` → имя файла (события Callas знают только id)."""
    names: dict[str, str] = {}
    for item in _list(order.get("files")):
        item = _mapping(item)
        name = _name(item.get("name") or item.get("filename") or item.get("path"))
        if not name:
            continue
        for key in ("file_result_id", "id"):
            if item.get(key) not in (None, ""):
                names[str(item[key])] = name
    return names


def _event(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        data, kind, event_id, at = value.get("data"), value.get("type"), value.get("id"), value.get("created_at")
    else:
        data, kind = getattr(value, "data", None), getattr(value, "type", None)
        event_id, at = getattr(value, "id", None), getattr(value, "created_at", None)
    if not kind or not isinstance(data, Mapping):
        return None
    return {"id": event_id, "type": str(kind), "at": _iso(at), "data": data}


# -------------------------------------------------------------------------- issues


def _client_count(message: str, issues: list[Mapping[str, Any]]) -> str:
    """Как ``displayIssue`` в интерфейсе: в тексте — число вхождений клиента."""
    for issue in issues:
        baseline, introduced = issue.get("baseline_occurrences"), issue.get("introduced_by_fix")
        text = issue.get("message")
        if isinstance(text, str) and text in message and isinstance(baseline, int) and baseline > 0 and introduced:
            fixed = _TRAILING_COUNT_RE.sub(lambda m: f"({baseline}{m.group(2)}{m.group(3)}){m.group(4)}", text)
            message = message.replace(text, fixed)
    return message


def _pitstop_key(issue: Mapping[str, Any]) -> str:
    action = issue.get("action_id")
    if action not in (None, ""):
        return f"action:{action}"
    return re.sub(r"\s+", " ", re.sub(r"\d+(?:[.,]\d+)?", "#", str(issue.get("message") or ""))).strip()


def _group_entries(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pitstop = _mapping(state.get("pitstop"))
    raw = [issue for issue in _list(pitstop.get("issues")) if isinstance(issue, Mapping)]
    for group in issue_groups_for_order_dto(state):
        source, code = group.get("source"), str(group.get("code") or "")
        severity = group.get("severity") if group.get("severity") in _SEVERITY_RANK else "info"
        files = [str(item) for item in group.get("files") or ()]
        data: dict[str, Any] = {
            "group_code": code,
            "objects": group.get("objects"),
            "files": files or None,
            "samples": list(group.get("samples") or ()) or None,
            "fix_plan": group.get("fix_plan"),
        }
        title = str(group.get("message") or code)
        if source == SOURCE_PITSTOP:
            demoted = group.get("policy_demoted") is True
            matched = [
                issue for issue in raw
                if _pitstop_key(issue) == code
                and pitstop_severity(issue.get("severity")) == group.get("severity")
                and (issue.get("policy_demoted") is True) == demoted
            ]
            hidden = bool(matched) and all(issue.get("hidden_by_baseline") is True for issue in matched)
            introduced = sum(int(issue.get("introduced_by_fix") or 0) for issue in matched)
            baseline = [issue.get("baseline_occurrences") for issue in matched if isinstance(issue.get("baseline_occurrences"), int)]
            original = next(
                (issue.get("original_severity") or issue.get("policy_original_severity")
                 for issue in matched if issue.get("original_severity") or issue.get("policy_original_severity")),
                None,
            )
            category = classify(matched[0]) if matched else None
            # PitStop text is English; known groups already have a Russian label.
            if category is not None and not _CYRILLIC_START_RE.match(title):
                title = f"{category.label}: {title}"
            title = _client_count(title, matched)
            action = code[len("action:"):] if code.startswith("action:") else None
            data.update({
                "action_id": action,
                "category": category.key if category else None,
                "category_label": group.get("category_label") or (category.label if category else None),
                "policy_demoted": demoted or None,
                "policy_unclassified": group.get("policy_unclassified") or None,
                "original_severity": str(original) if original else None,
                "added_by_fix": hidden or None,
                "introduced_by_fix": introduced or None,
                "baseline_occurrences": sum(baseline) if baseline and introduced else None,
            })
            entries.append(_entry(
                KIND_ISSUE, f"pitstop:{action}" if action else "pitstop:other", "pitstop", title,
                severity=severity, pages=group.get("pages"), count=group.get("count"),
                file=files[0] if len(files) == 1 else None, data=data,
            ))
        elif source == SOURCE_LOCAL:
            page_rule = code in PAGE_RULE_CODES
            data["page_rule"] = page_rule or None
            entries.append(_entry(
                KIND_ISSUE,
                f"page_rule:{code}" if page_rule else f"local:{code}" if code in _KNOWN_LOCAL_CODES else "local:other",
                "local", title,
                severity=severity, pages=group.get("pages"), count=group.get("count"),
                file=files[0] if len(files) == 1 else None, data=data,
            ))
    return entries


def _prepress_entries(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    prepress = _mapping(state.get("prepress"))
    entries: list[dict[str, Any]] = []
    labels = {"fonts": "шрифтов", "high_ppi": "разрешения растров", "black_solid": "плашки 100% K"}
    for name, label in labels.items():
        block = _mapping(prepress.get(name))
        if block.get("status") == "failed":
            entries.append(_entry(
                KIND_ISSUE, f"prepress:{name}:failed", "callas", f"Проверка {label} (callas) не выполнена",
                severity="info", detail=str(block.get("error") or "") or None, outcome="failed",
            ))
    ppi = _mapping(prepress.get("high_ppi"))
    if ppi.get("status") == "ok":
        if ppi.get("message"):
            entries.append(_entry(
                KIND_ISSUE, "prepress:high_ppi", "callas", str(ppi["message"]), severity="warning",
                pages=ppi.get("pages"), count=ppi.get("count"),
                data={"max_ppi": ppi.get("max_ppi"), "limit": ppi.get("limit"), "fix_plan": "downsample_images"},
            ))
        bits = _mapping(ppi.get("one_bit"))
        if ppi.get("one_bit_message"):
            entries.append(_entry(
                KIND_ISSUE, "prepress:one_bit_ppi", "callas", str(ppi["one_bit_message"]), severity="info",
                pages=bits.get("pages"), count=bits.get("count"), data={"max_ppi": bits.get("max_ppi")},
            ))
    black = _mapping(prepress.get("black_solid"))
    if black.get("status") == "ok" and black.get("message"):
        if black.get("detected"):
            code, severity, pages = "prepress:black_solid", "warning", black.get("pages")
        elif black.get("fix_available"):
            code, severity, pages = "prepress:black_solid_minor", "info", black.get("fix_pages")
        else:
            code, severity, pages = "prepress:black_manual", "info", black.get("manual_pages")
        entries.append(_entry(
            KIND_ISSUE, code, "callas", str(black["message"]), severity=severity, pages=pages,
            data={
                "share": black.get("share"), "threshold": black.get("threshold"),
                "fix_share": black.get("fix_share"), "page_shares": black.get("page_shares"),
                "fix_plan": "black_to_rich" if black.get("detected") or black.get("fix_available") else None,
            },
        ))
    entries.extend(_overprint_entries(prepress))
    fonts = _mapping(prepress.get("fonts"))
    count = fonts.get("count") if isinstance(fonts.get("count"), int) else 0
    if fonts.get("status") == "ok" and count > 0:
        listed = [
            {"name": item.get("name"), "type": item.get("type"), "embedded": item.get("embedded")}
            for item in _list(fonts.get("fonts")) if isinstance(item, Mapping)
        ]
        missing = fonts.get("not_embedded") if isinstance(fonts.get("not_embedded"), int) else 0
        if missing:
            entries.append(_entry(
                KIND_ISSUE, "prepress:fonts_not_embedded", "callas",
                "Шрифты не встроены, перевод в кривые невозможен, нужна проверка", severity="warning",
                detail=str(fonts.get("message") or "") or None, pages=fonts.get("pages"), count=missing,
                data={"fonts": listed},
            ))
        else:
            entries.append(_entry(
                KIND_ISSUE, "prepress:fonts", "callas", str(fonts.get("message") or f"Шрифтов: {count}"),
                severity="info", pages=fonts.get("pages"), count=count,
                data={"fonts": listed, "fix_plan": "fonts_to_outlines"},
            ))
    return entries


def _overprint_entries(prepress: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Overprint (services/overprint.py): ``error`` — не только технические краски (заказ красный),
    ``info`` — только технические; сбой проверки — ``prepress:overprint:failed`` (warning)."""
    block = _mapping(prepress.get("overprint"))
    status = block.get("status")
    if status == "failed":
        return [_entry(
            KIND_ISSUE, "prepress:overprint:failed", "callas", "Проверка overprint (callas) не выполнена",
            severity="warning", detail=str(block.get("error") or "") or None, outcome="failed",
        )]
    if status != "ok":
        return []
    blocking = block.get("blocking_count") if isinstance(block.get("blocking_count"), int) else 0
    technical = block.get("technical_count") if isinstance(block.get("technical_count"), int) else 0
    if not blocking and not technical:
        return []
    data = {
        "by_kind": dict(_mapping(block.get("by_kind"))) or None,
        "blocking_count": blocking,
        "technical_count": technical,
        "technical_pages": list(_list(block.get("technical_pages"))) or None,
        "colorants": list(_list(block.get("colorants"))) or None,
        "technical_colorants": list(_list(block.get("technical_colorants"))) or None,
        "samples": list(_list(block.get("samples"))) or None,
        "technical_samples": list(_list(block.get("technical_samples"))) or None,
        "profile": block.get("profile"),
    }
    if blocking:
        return [_entry(
            KIND_ISSUE, "prepress:overprint", "callas", str(block.get("message") or f"Overprint: {blocking}"),
            severity="error", pages=block.get("blocking_pages"), count=blocking, data=data,
        )]
    return [_entry(
        KIND_ISSUE, "prepress:overprint", "callas", str(block.get("technical_message") or f"Overprint: {technical}"),
        severity="info", pages=block.get("technical_pages"), count=technical, data=data,
    )]


def _ink_entries(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    ink = _mapping(state.get("ink_coverage"))
    entries = []
    for flag in _list(ink.get("flags")):
        flag = _mapping(flag)
        kind = str(flag.get("kind") or "")
        if not kind:
            continue
        label = _FLAG_LABELS.get(kind, kind)
        page, percent, threshold = flag.get("page"), flag.get("percent"), flag.get("threshold")
        if kind == "light_areas":
            title = f"{label}: {_num(percent)}% площади стр. {page} светлее {_num(threshold)}%"
        else:
            title = f"{label}: {_num(percent)}% на стр. {page} (порог {_num(threshold)}%)"
        entries.append(_entry(
            KIND_ISSUE, f"ink:{kind}", "callas", title, severity="warning", pages=[page],
            data={"percent": percent, "threshold": threshold, "box": ink.get("box")},
        ))
    return entries


def _state_issues(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        *_guard("issue_groups", lambda: _group_entries(state)),
        *_guard("prepress", lambda: _prepress_entries(state)),
        *_guard("ink_coverage", lambda: _ink_entries(state)),
    ]


def _issue_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(entry["code"]), str(entry.get("file") or ""), str(entry["data"].get("group_code") or "")


def _issues(
    final: Mapping[str, Any], initial: Mapping[str, Any] | None, checked_at: str | None, revised_at: str | None,
) -> list[dict[str, Any]]:
    current = _state_issues(final)
    keys = {_issue_key(entry) for entry in current}
    initial_entries = _state_issues(initial) if initial else []
    initial_keys = {_issue_key(entry) for entry in initial_entries}
    for entry in current:
        entry["at"] = checked_at if (_issue_key(entry) in initial_keys or not revised_at) else revised_at
    # A block that is absent from the final state (no PitStop/prepress data) says
    # nothing about the finding; only a block that was re-checked can resolve it.
    rechecked = {
        "pitstop": isinstance(final.get("pitstop"), Mapping) and final["pitstop"].get("execution_status") == "completed",
        "callas": isinstance(final.get("prepress"), Mapping),
        "local": True,
    }
    resolved = []
    for entry in initial_entries:
        if _issue_key(entry) in keys:
            continue
        entry["at"] = checked_at
        if rechecked.get(entry["source"], False) and not entry["code"].startswith("ink:"):
            entry["outcome"] = "resolved"
            entry["data"]["resolved"] = True
            entry["detail"] = " ".join(filter(None, [entry["detail"], "После изменений не найдено."]))
        else:
            entry["data"]["from_initial_check"] = True
        resolved.append(entry)
    ordered = sorted(current, key=lambda item: (_SEVERITY_RANK.get(item["severity"], 3), item["source"] != "local"))
    return ordered + sorted(resolved, key=lambda item: _SEVERITY_RANK.get(item["severity"], 3))


# --------------------------------------------------------------------- auto changes


def _color_entries(state: Mapping[str, Any], at: str | None) -> list[dict[str, Any]]:
    info = _mapping(state.get("color_conversion"))
    status = info.get("status")
    if not status:
        return []
    reason = info.get("reason") or ("icc" if str(info.get("from") or "").upper() == "ICC" else "rgb")
    base = {"from": info.get("from"), "to": info.get("to"), "profile": info.get("profile"),
            "fixups_applied": info.get("fixups_applied"), "reason": reason}
    remaining = {page for page in _list(info.get("icc_remaining_pages"))}
    icc_pages = [page for page in _list(info.get("icc_pages")) if page not in remaining] or info.get("pages")
    remaining_text = f"ICC остался на стр. {', '.join(str(page) for page in sorted(remaining))}" if remaining else None
    entries = []
    if status == "converted":
        if reason == "rgb":
            entries.append(_entry(
                KIND_AUTO, "auto:color_conversion:rgb", "callas", "Цвет RGB переведён в CMYK", at=at,
                detail=f"Профиль callas: {info['profile']}" if info.get("profile") else None,
                pages=info.get("pages"), outcome="converted", data=base,
            ))
        if info.get("icc_removed") is True:
            title = (
                "Снят ICC-профиль: цвет переведён в DeviceCMYK без пересчёта"
                if reason == "icc" else "Снят ICC-профиль"
            )
            entries.append(_entry(
                KIND_AUTO, "auto:icc_removed", "callas", title, at=at, detail=remaining_text,
                pages=icc_pages, outcome="converted", data=base,
            ))
        if info.get("output_intent_removed") is True:
            entries.append(_entry(
                KIND_AUTO, "auto:output_intent_removed", "callas", "Удалён выходной профиль (OutputIntent)",
                at=at, outcome="converted", data=base,
            ))
    elif status == "failed":
        title = "Не удалось снять ICC-профиль" if reason == "icc" else "Не удалось перевести RGB в CMYK"
        entries.append(_entry(
            KIND_AUTO, f"auto:color_conversion:{reason}", "callas", f"{title}: PDF оставлен без изменений",
            severity="warning", detail=str(info.get("error") or "") or None, at=at, pages=info.get("pages"),
            outcome="failed", data=base,
        ))
    elif status == "skipped" and (info.get("skip_reason") or info.get("skip_message")):
        entries.append(_entry(
            KIND_AUTO, f"auto:color_conversion:{reason}", "callas", "Цветовой профиль не изменён",
            severity="info", detail=str(info.get("skip_message") or "") or None, at=at, outcome="skipped",
            data={**base, "skip_reason": info.get("skip_reason")},
        ))
    return entries


def _action_entry(file: str | None, action: Mapping[str, Any], source: str, at: str | None) -> dict[str, Any] | None:
    kind = str(action.get("kind") or "")
    if kind == "strip_icc":
        return _entry(KIND_AUTO, "auto:strip_icc", "local", "ICC-профиль растра CMYK удалён при сборке PDF",
                      file=file, at=at)
    if kind == "dpi_downsample":
        return _entry(
            KIND_AUTO, "auto:dpi_downsample", "local",
            f"Разрешение растра уменьшено: {_num(action.get('from_dpi'))} → {_num(action.get('target_dpi'))} dpi"
            if action.get("target_dpi") is not None
            else f"Разрешение растра уменьшено (было {_num(action.get('from_dpi'))} dpi)",
            file=file, at=at, data={"from_dpi": action.get("from_dpi"), "target_dpi": action.get("target_dpi"),
                                    "target_mm": action.get("target_mm")},
        )
    if kind == "size_correction":
        target = _mm(action.get("target_mm"))
        return _entry(
            KIND_AUTO, "auto:size_correction", source,
            f"Размер скорректирован при сборке PDF{f' до {target} мм' if target else ''}",
            file=file, at=at, data=dict(action),
        )
    return _entry(KIND_AUTO, f"auto:action:{kind or 'unknown'}", source, f"Обработка файла: {kind or 'неизвестное действие'}",
                  file=file, at=at, data=dict(action))


def _file_auto_entries(state: Mapping[str, Any], at: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for item in _list(state.get("files")):
        item = _mapping(item)
        name = _name(item.get("name") or item.get("filename") or item.get("path"))
        pdf = str(item.get("actual_format") or "").upper() == "PDF"
        source = "callas" if pdf else "local"
        size_fix = _mapping(item.get("size_fix"))
        callas = _mapping(item.get("callas_source_fix"))
        callas_accepted = callas.get("status") == "accepted"
        if size_fix.get("method") and size_fix.get("auto") is True:
            method = str(size_fix["method"])
            entries.append(_entry(
                KIND_AUTO, f"auto:size_fix:{method}", "callas" if method in {"bleed", "callas"} else source,
                SIZE_FIX_TITLES.get(method, f"Размер скорректирован ({method})"),
                detail=str(size_fix.get("detail") or "") or _size_range(size_fix.get("from_mm"), size_fix.get("to_mm")),
                file=name, at=at, outcome="applied",
                data={
                    "method": method, "from_mm": size_fix.get("from_mm"), "to_mm": size_fix.get("to_mm"),
                    "callas_plan": callas.get("plan") if callas_accepted else None,
                    "bleed_only": callas.get("bleed_only") if callas_accepted else None,
                },
            ))
        elif callas_accepted and callas.get("auto") is True and not size_fix.get("method"):
            plan = str(callas.get("plan") or "")
            entries.append(_entry(
                KIND_AUTO, f"auto:callas_source_fix:{plan}", "callas",
                "Добавлены поля вылета (Callas)" if callas.get("bleed_only") else f"Исправлено Callas: {_plan_title(plan)}",
                file=name, at=at, outcome="applied",
                data={"plan": plan, "plan_title": callas.get("plan_title"), "bleed_only": callas.get("bleed_only")},
            ))
        if item.get("tiff_flattened") is True:
            visible, hidden = item.get("tiff_visible_layer_count"), item.get("tiff_hidden_layer_count")
            entries.append(_entry(
                KIND_AUTO, "auto:tiff_flatten", "local", "Слои TIFF сведены",
                detail=f"Видимых слоёв: {visible}, скрытых: {hidden}" if visible is not None else None,
                file=name, at=at,
                data={"visible_layers": visible, "hidden_layers": hidden, "strategy": item.get("tiff_flatten_strategy"),
                      "layers": item.get("tiff_layer_count")},
            ))
        rotation = item.get("rotation_degrees")
        if isinstance(rotation, (int, float)) and not isinstance(rotation, bool) and rotation % 360:
            entries.append(_entry(
                KIND_AUTO, "auto:rotate", source, f"Сторона повёрнута на {rotation:g}°", file=name, at=at,
                data={"rotation_degrees": rotation},
            ))
        actions = [_mapping(action) for action in _list(item.get("processing_actions"))]
        if item.get("downsampled_from_dpi") is not None and not any(a.get("kind") == "dpi_downsample" for a in actions):
            actions.append({"kind": "dpi_downsample", "from_dpi": item.get("downsampled_from_dpi")})
        for action in actions:
            # The size_fix mark already describes the size ladder's result.
            if action.get("kind") == "size_correction" and size_fix.get("method"):
                continue
            entry = _action_entry(name, action, source, at)
            if entry is not None:
                entries.append(entry)
    return entries


def _auto_changes(states: Sequence[tuple[Mapping[str, Any], str | None]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result = []
    for state, at in states:
        for entry in (*_guard("color_conversion", lambda: _color_entries(state, at)),
                      *_guard("files", lambda: _file_auto_entries(state, at))):
            key = (entry["code"], entry.get("file") or "", entry.get("outcome") or "")
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
    return result


# ------------------------------------------------------------------ operator changes


class _Attempt:
    __slots__ = ("plan", "file", "started", "finished", "decided", "status", "events", "revision", "extra")

    def __init__(self, plan: str | None, file: str | None) -> None:
        self.plan, self.file = plan, file
        self.started = self.finished = self.decided = None
        self.status = "processing"
        self.events: list[int] = []
        self.revision: Mapping[str, Any] | None = None
        self.extra: dict[str, Any] = {}

    def to_entry(self) -> dict[str, Any]:
        outcome = self.status
        title = f"Исправление Callas: {_plan_title(self.plan)}"
        parts = [_OUTCOME_TEXT.get(outcome, outcome).capitalize() if outcome else None]
        data: dict[str, Any] = {
            "plan": self.plan, "plan_title": _plan_title(self.plan) if self.plan else None,
            "started_at": self.started, "finished_at": self.finished, "decided_at": self.decided,
            "event_ids": self.events or None, **self.extra,
        }
        if self.revision:
            number, status = self.revision.get("revision"), self.revision.get("status")
            data.update({"pdf_revision": number, "revision_status": status})
            if number:
                parts.append(f"новая ревизия PDF r{number}" + (f" (статус {status})" if status else ""))
        return _entry(
            KIND_OPERATOR, f"callas_fix:{self.plan or 'unknown'}", "callas", title,
            severity="warning" if outcome == "failed" else None,
            detail="; ".join(filter(None, parts)) or None, file=self.file,
            at=self.decided or self.finished or self.started, outcome=outcome, data=data,
        )


def _operator_changes(
    order: Mapping[str, Any], events: list[dict[str, Any]], revisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    names = _file_names(order)
    attempts: list[_Attempt] = []
    open_by_file: dict[str, _Attempt] = {}
    entries: list[dict[str, Any]] = []
    revision_events: list[dict[str, Any]] = []
    folded_corrections: list[dict[str, Any]] = []

    def attempt_for(file: str | None, plan: str | None = None) -> _Attempt:
        key = file or ""
        current = open_by_file.get(key)
        if current is None:
            current = _Attempt(plan, file)
            attempts.append(current)
            open_by_file[key] = current
        return current

    for event in events:
        kind, data, at = event["type"], event["data"], event["at"]
        file = _name(data.get("file")) or (names.get(str(data.get("file_id"))) if data.get("file_id") not in (None, "") else None)
        if kind == "order.callas_fix.started":
            current = _Attempt(str(data.get("plan") or "") or None, file)
            current.started = at
            attempts.append(current)
            open_by_file[file or ""] = current
            current.events.append(event["id"])
        elif kind == "order.callas_fix.reset":
            current = attempt_for(file)
            current.extra["reset_source"] = True
            current.events.append(event["id"])
        elif kind == "order.callas_fix.finished":
            current = attempt_for(file, str(data.get("plan") or "") or None)
            current.finished = at
            current.status = "failed" if data.get("status") == "failed" else str(data.get("status") or "ready")
            current.events.append(event["id"])
        elif kind in {"order.callas_fix.accepted", "order.callas_fix.rejected", "order.callas_fix.save_failed"}:
            current = attempt_for(file)
            current.decided = at
            current.status = {"order.callas_fix.accepted": "accepted", "order.callas_fix.rejected": "rejected"}.get(kind, "failed")
            if kind == "order.callas_fix.save_failed":
                current.extra["save_failed"] = True
            current.events.append(event["id"])
            open_by_file.pop(file or "", None)
        elif kind == "order.callas_source_fix":
            match = next((item for item in reversed(attempts) if item.file == file), None)
            if match is not None:
                match.extra["source_applied"] = data.get("applied") is True
                match.events.append(event["id"])
            else:
                applied = data.get("applied") is True
                entries.append(_entry(
                    KIND_OPERATOR, "callas_source_fix:applied" if applied else "callas_source_fix:reset", "callas",
                    "Принятое исправление Callas применено к исходному файлу" if applied
                    else "Исходный файл снова проверяется без исправления Callas",
                    file=file, at=at, outcome="applied" if applied else "reset", data={"event_id": event["id"]},
                ))
        elif kind in {"order.correction_confirmed", "order.correction_rejected"}:
            confirmed = kind == "order.correction_confirmed"
            for decision in _list(data.get("decisions")):
                decision = _mapping(decision)
                decision_file = _name(decision.get("path")) or file
                if decision.get("decision") == "callas_source_fix" or (data.get("callas_source_fix") and confirmed):
                    folded_corrections.append({"file": decision_file, "at": at, "event_id": event["id"]})
                    continue
                original, proposed = _mapping(decision.get("original")), _mapping(decision.get("proposed"))
                entries.append(_entry(
                    KIND_OPERATOR, "correction:confirmed" if confirmed else "correction:rejected", "operator",
                    "Коррекция размера подтверждена оператором" if confirmed else "Коррекция размера отклонена оператором",
                    detail=_size_range([original.get("width_mm"), original.get("height_mm")], proposed.get("target_mm")),
                    file=decision_file, at=at, outcome="accepted" if confirmed else "rejected",
                    data={
                        "event_id": event["id"],
                        "original_mm": [original.get("width_mm"), original.get("height_mm")]
                        if original.get("width_mm") is not None else None,
                        "proposed_mm": proposed.get("target_mm"), "crop_mm": proposed.get("crop_mm"),
                        "scale": proposed.get("scale"), "rotation_degrees": proposed.get("rotation_degrees"),
                        "resample_decision": original.get("resample_decision"),
                    },
                ))
            if not _list(data.get("decisions")) and data.get("callas_source_fix"):
                folded_corrections.append({"file": file, "at": at, "event_id": event["id"]})
        elif kind == "order.pdf_revision":
            snapshot = _mapping(data.get("order"))
            revision_events.append({
                "at": at, "event_id": event["id"], "status": data.get("status") or snapshot.get("status"),
                "revision": snapshot.get("current_pdf_revision"),
            })
        elif kind == "order.forced_print":
            errors = [str(item) for item in _list(data.get("errors"))]
            entries.append(_entry(
                KIND_OPERATOR, "forced_print", "operator",
                "Отправлено в печать с ошибками по подтверждению оператора", severity="warning",
                detail="; ".join(errors[:3]) or None, at=at, outcome="accepted",
                data={"event_id": event["id"], "pdf_revision": data.get("pdf_revision"), "errors": errors or None},
            ))

    # Order-level fixes: the accepted fix and its PDF revision are written by one request.
    for revision in revision_events:
        when = _parse_time(revision["at"])
        match = None
        if when is not None:
            for attempt in attempts:
                decided = _parse_time(attempt.decided)
                if (attempt.status == "accepted" and attempt.file is None and attempt.revision is None
                        and decided is not None and abs((decided - when).total_seconds()) <= _REVISION_LINK_SECONDS):
                    match = attempt
                    break
        if match is not None:
            match.revision = revision
            continue
        number = revision.get("revision")
        entries.append(_entry(
            KIND_OPERATOR, "pdf_revision", "operator",
            f"PDF заказа заменён: ревизия r{number}" if number else "PDF заказа заменён",
            detail=f"Повторная проверка: статус {revision['status']}" if revision.get("status") else None,
            at=revision["at"], outcome=str(revision.get("status") or "") or None,
            data={"event_id": revision["event_id"], "pdf_revision": number, "status": revision.get("status")},
        ))
    for folded in folded_corrections:
        match = next((item for item in reversed(attempts) if item.file == folded["file"] and item.status == "accepted"), None)
        if match is not None:
            match.extra["order_resumed_at"] = folded["at"]
            continue
        entries.append(_entry(
            KIND_OPERATOR, "correction:callas_source_fix", "operator",
            "Заказ продолжен: размер исправлен Callas", file=folded["file"], at=folded["at"], outcome="accepted",
            data={"event_id": folded["event_id"]},
        ))
    callas_entries = [attempt.to_entry() for attempt in attempts]

    # Structured marks without a matching event (old runs, archived journal gaps).
    corrected = {entry.get("file") for entry in entries if entry["code"] == "correction:confirmed"}
    fixed_files = {attempt.file for attempt in attempts if attempt.status == "accepted"}
    for entry in entries:
        if entry["code"] == "correction:confirmed":
            size_fix = _size_fix_of(order, entry.get("file"))
            if size_fix:
                entry["data"].update(_clean({"method": size_fix.get("method"), "size_fix_detail": size_fix.get("detail")}))
    for item in _list(order.get("files")):
        item = _mapping(item)
        name = _name(item.get("name") or item.get("filename") or item.get("path"))
        size_fix, callas = _mapping(item.get("size_fix")), _mapping(item.get("callas_source_fix"))
        if size_fix.get("method") and size_fix.get("auto") is not True and name not in corrected and name not in fixed_files:
            method = str(size_fix["method"])
            entries.append(_entry(
                KIND_OPERATOR, f"operator:size_fix:{method}", "operator",
                f"{SIZE_FIX_TITLES.get(method, 'Размер скорректирован')} (подтверждено оператором)",
                detail=str(size_fix.get("detail") or "") or _size_range(size_fix.get("from_mm"), size_fix.get("to_mm")),
                file=name, outcome="accepted",
                data={"method": method, "from_mm": size_fix.get("from_mm"), "to_mm": size_fix.get("to_mm")},
            ))
        elif (callas.get("status") == "accepted" and callas.get("auto") is not True
              and not size_fix.get("method") and name not in fixed_files):
            plan = str(callas.get("plan") or "")
            entries.append(_entry(
                KIND_OPERATOR, f"operator:callas_source_fix:{plan}", "callas",
                f"Исправление Callas: {_plan_title(plan)}", file=name, at=_iso(callas.get("accepted_at")),
                outcome="accepted", data={"plan": plan, "bleed_only": callas.get("bleed_only")},
            ))

    # Revisions without any event (beyond the first build) still changed the PDF.
    known = {entry["data"].get("pdf_revision") for entry in (*entries, *callas_entries)}
    for revision in revisions:
        number = revision.get("revision_number")
        if not isinstance(number, int) or number <= 1 or number in known:
            continue
        entries.append(_entry(
            KIND_OPERATOR, "pdf_revision", "system", f"Новая ревизия PDF r{number}",
            detail=f"Проверка PitStop: {revision['verdict']}" if revision.get("verdict") else None,
            at=_iso(revision.get("created_at")), data={"pdf_revision": number, "verdict": revision.get("verdict")},
        ))
    combined = [*callas_entries, *entries]
    return sorted(combined, key=lambda item: (item["at"] is None, item["at"] or ""))


def _size_fix_of(order: Mapping[str, Any], file: str | None) -> Mapping[str, Any]:
    for item in _list(order.get("files")):
        item = _mapping(item)
        if _name(item.get("name") or item.get("path")) == file:
            return _mapping(item.get("size_fix"))
    return {}


# -------------------------------------------------------------------------- decisions


def _decisions(actions: Sequence[Mapping[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for action in actions:
        action = _mapping(action)
        kind = str(action.get("action") or "")
        failed = action.get("status") == "failed"
        response = _mapping(action.get("cms_response"))
        title = {"print": "Передано в печать", "reject": "Возвращено на доработку"}.get(kind, kind or "Действие")
        if failed:
            title = f"{title}: операция не выполнена"
        entries.append(_entry(
            KIND_DECISION, f"action:{kind or 'unknown'}", "operator", title,
            severity="warning" if failed else None,
            detail=str(action.get("comment") or "") or None,
            at=_iso(action.get("created_at")), outcome="failed" if failed else "done",
            data={
                "action_id": action.get("id"), "status": action.get("status"),
                "comment": action.get("comment") or None,
                "kit_main_id": response.get("kit_main_id"),
                "returned_with_kit": response.get("returned_with_kit"),
                "message": response.get("message") if isinstance(response.get("message"), str) else None,
            },
        ))
    for event in events:
        if event["type"] == "order.file_transition_rolled_back":
            entries.append(_entry(
                KIND_DECISION, "transition:rolled_back", "system", "Перемещение файлов отменено",
                severity="warning", at=event["at"], outcome="rolled_back",
                data={"event_id": event["id"], "status": event["data"].get("status")},
            ))
    return sorted(entries, key=lambda item: (item["at"] is None, item["at"] or ""))


# ---------------------------------------------------------------------------- public


def build_order_journal(
    order: Mapping[str, Any],
    events: Sequence[Any] = (),
    actions: Sequence[Mapping[str, Any]] = (),
    revisions: Sequence[Mapping[str, Any]] = (),
    *,
    run_id: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Собрать журнал одного заказа.

    ``order`` — итоговый DTO заказа (как в ``GET /api/checks/{id}``); ``events`` —
    события этого заказа (``RunEvent`` или словари ``{id, type, created_at, data}``,
    уже отфильтрованные по заказу); ``actions`` — записи ``order_actions``
    (``{id, action, status, comment, created_at, cms_response}``); ``revisions`` —
    ревизии PDF (``{revision_number, created_at, sha256, is_current, verdict}``).
    """
    order = _mapping(order)
    normalized = sorted(
        (item for item in (_event(value) for value in events or ()) if item is not None),
        key=lambda item: (item["id"] if isinstance(item["id"], int) else 0, item["at"] or ""),
    )
    completed = next(
        (item for item in normalized if item["type"] == "order.completed" and isinstance(item["data"].get("order"), Mapping)),
        None,
    )
    initial = completed["data"]["order"] if completed else None
    checked_at = completed["at"] if completed else None
    revision_snapshots = [
        (item["data"]["order"], item["at"]) for item in normalized
        if item["type"] == "order.pdf_revision" and isinstance(item["data"].get("order"), Mapping)
    ]
    revised_at = revision_snapshots[-1][1] if revision_snapshots else None

    issues = _issues(order, initial, checked_at, revised_at)
    auto = _auto_changes([
        *([(initial, checked_at)] if initial else []),
        *revision_snapshots,
        (order, None),
    ])
    operator = _guard("operator_changes", lambda: _operator_changes(order, normalized, revisions))
    decisions = _guard("decisions", lambda: _decisions(actions, normalized))

    policy = _mapping(order.get("check_policy"))
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "run_id": run_id,
        "order_id": order.get("order_id"),
        "customer_id": order.get("customer_id") or None,
        "aggregate_id": order.get("aggregate_id") or None,
        "status": order.get("status"),
        "check_policy": _clean({key: policy.get(key) for key in ("key", "label", "reason")}) if policy else None,
        "pdf_revision": order.get("current_pdf_revision"),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "summary": {
            "issues": sum(1 for entry in issues if entry["outcome"] != "resolved"),
            "resolved_issues": sum(1 for entry in issues if entry["outcome"] == "resolved"),
            "auto_changes": len(auto),
            "operator_changes": len(operator),
            "decisions": len(decisions),
        },
        "entries": [*issues, *auto, *operator, *decisions],
    }
