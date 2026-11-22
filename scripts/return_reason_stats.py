"""Насколько подсказки причин возврата совпадают с тем, что пишут операторы.

Берёт каждый возврат на доработку (``order_actions.action = 'reject'``) с комментарием
оператора, собирает DTO заказа так же, как API (``SqlRunRepository.get_run``), считает
подсказки ``suggest_return_reasons`` и сравнивает с причинами из комментария
(``services/return_suggestion_stats.py``). База открывается ТОЛЬКО на чтение
(``mode=ro``); ``control_panel`` не импортируется (он применяет миграции к базе).

    .venv/bin/python scripts/return_reason_stats.py
    .venv/bin/python scripts/return_reason_stats.py --show-orders --limit 20
    .venv/bin/python scripts/return_reason_stats.py --json > stats.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.return_reasons import load_return_reasons
from services.return_suggestion_stats import (
    CatalogueMatcher,
    OperatorLine,
    aggregate,
    comment_key,
    compare_return,
    prefill_summary,
)
from services.return_suggestions import suggest_return_reasons
from services.sql_repository import SqlRunRepository

_CONFIDENCE_LABELS = {"high": "high (отмечается сам)", "medium": "medium", "low": "low"}


@dataclass
class ReturnRecord:
    action_id: int
    run_id: str
    order_id: str
    created_at: str
    status: str
    comment: str | None
    dto_source: str
    operator: list[OperatorLine]
    suggestions: list[dict[str, Any]]
    dto: dict[str, Any] = field(repr=False, default_factory=dict)


# --------------------------------------------------------------------------- чтение базы


def _connect_ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)


def _fallback_dto(raw: sqlite3.Connection, order_result_id: int) -> dict[str, Any]:
    """DTO из ``order_results``/``file_results``, если заказа нет в собранном запуске."""
    row = raw.execute(
        "select order_id, status, errors_json from order_results where id = ?", (order_result_id,)
    ).fetchone()
    files = [
        {"name": name, "errors": json.loads(errors or "[]")}
        for name, errors in raw.execute(
            "select filename, errors_json from file_results where order_result_id = ?", (order_result_id,)
        )
    ]
    return {"order_id": row[0], "status": row[1], "errors": json.loads(row[2] or "[]"), "files": files}


def load_returns(db: Path, limit: int | None) -> list[ReturnRecord]:
    raw = _connect_ro(db)
    engine = create_engine("sqlite://", creator=lambda: _connect_ro(db))
    repo = SqlRunRepository(sessionmaker(bind=engine), recover_interrupted=False)
    reasons = load_return_reasons()
    matcher = CatalogueMatcher(reasons)
    query = (
        "select a.id, a.order_result_id, o.run_id, o.order_id, a.created_at, a.status, a.comment "
        "from order_actions a join order_results o on o.id = a.order_result_id "
        "where a.action = 'reject' order by a.created_at desc, a.id desc"
    )
    rows = raw.execute(query + (" limit ?" if limit else ""), (limit,) if limit else ()).fetchall()
    runs: dict[str, dict[str, Any] | None] = {}
    records: list[ReturnRecord] = []
    for action_id, order_result_id, run_id, order_id, created_at, status, comment in reversed(rows):
        if run_id not in runs:
            runs[run_id] = repo.get_run(run_id)
        orders = (runs[run_id] or {}).get("orders") or {}
        values = orders.values() if isinstance(orders, Mapping) else orders
        dto = next((item for item in values if str(item.get("order_id")) == str(order_id)), None)
        source = "repository"
        if dto is None:
            dto, source = _fallback_dto(raw, order_result_id), "order_results"
        records.append(ReturnRecord(
            action_id=action_id, run_id=run_id, order_id=str(order_id), created_at=str(created_at),
            status=status, comment=(comment or "").strip() or None, dto_source=source,
            operator=matcher.split(comment), suggestions=suggest_return_reasons(dto, reasons), dto=dto,
        ))
    raw.close()
    engine.dispose()
    return records


def dedupe(records: Sequence[ReturnRecord]) -> list[ReturnRecord]:
    """Повторный возврат того же заказа с тем же комментарием — один (последний) возврат."""
    latest: dict[tuple[str, str], ReturnRecord] = {}
    for record in records:
        latest[(record.order_id, comment_key(record.comment))] = record
    return list(latest.values())


def build_stats(records: Sequence[ReturnRecord]) -> dict[str, Any]:
    with_comment = [r for r in records if r.comment]
    without = [r for r in records if not r.comment]
    comparisons = [compare_return(r.operator, r.suggestions) for r in with_comment]
    return {
        "returns": len(records),
        "with_comment": len(with_comment),
        "without_comment": len(without),
        "fallback_dto": sum(1 for r in records if r.dto_source != "repository"),
        "commented": aggregate(comparisons, groups=[r.run_id for r in with_comment]),
        "uncommented": prefill_summary([r.suggestions for r in without]),
    }


# --------------------------------------------------------------------------- отчёт


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _short(text: str, limit: int = 110) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _missed_note(item: Mapping[str, Any]) -> str:
    if not item["catalogue"]:
        return "свободный текст (нет в справочнике)"
    if item["semantic_hits"] == item["count"]:
        return f"синоним: подсказан другой текст того же смысла ({item['signal']})"
    if item["rule_signal"]:
        return f"правило есть ({item['rule_signal']}), сигнал не найден"
    if item["signal"]:
        note = f"синоним сигнала {item['signal']}, сигнал не найден"
        return note + (f"; подсказан по смыслу {item['semantic_hits']} раз" if item["semantic_hits"] else "")
    return "нет правила"


def format_report(raw: Mapping[str, Any], dedup: Mapping[str, Any], top: int = 15) -> str:
    lines: list[str] = []
    add = lines.append
    c_raw, c = raw["commented"], dedup["commented"]
    add("Подсказки причин возврата против комментариев операторов")
    add("=" * 60)
    add("Комментарий оператора — не эталон: операторы пишут не все причины, поэтому точность —")
    add("оценка снизу. «Дословно» — тот же пункт справочника; «по смыслу» — тот же сигнал")
    add("(синонимичные пункты справочника, любые пункты размера).")
    add("")
    add(f"{'':44}{'все':>8}{'без повторов':>14}")
    rows = [
        ("Возвратов", raw["returns"], dedup["returns"]),
        ("  с комментарием", raw["with_comment"], dedup["with_comment"]),
        ("  без комментария", raw["without_comment"], dedup["without_comment"]),
        ("Причин в комментариях", c_raw["operator_lines"], c["operator_lines"]),
        ("  из них пункты справочника", c_raw["operator_catalogue_lines"], c["operator_catalogue_lines"]),
        ("Полнота дословно", _pct(c_raw["recall"]), _pct(c["recall"])),
        ("  среди пунктов справочника", _pct(c_raw["recall_catalogue"]), _pct(c["recall_catalogue"])),
        ("Полнота по смыслу", _pct(c_raw["recall_semantic"]), _pct(c["recall_semantic"])),
        ("Возвратов, где подсказано всё написанное", c_raw["returns_fully_covered"], c["returns_fully_covered"]),
        ("Отмечено заранее (preselect)", c_raw["preselected"], c["preselected"]),
        ("  из них оператор использовал", c_raw["preselected_used"], c["preselected_used"]),
        ("Точность отмеченных дословно", _pct(c_raw["preselect_precision"]), _pct(c["preselect_precision"])),
        ("Точность отмеченных по смыслу", _pct(c_raw["preselect_precision_semantic"]),
         _pct(c["preselect_precision_semantic"])),
    ]
    for label, left, right in rows:
        add(f"{label:44}{left!s:>8}{right!s:>14}")
    if dedup["fallback_dto"]:
        add(f"DTO собран из order_results (заказа нет в запуске): {dedup['fallback_dto']}")
    add("")
    add(f"По уверенности (без повторов, возвраты с комментарием: {c['returns']}):")
    add(f"  {'уверенность':24}{'подсказано':>11}{'дословно':>10}{'точн.':>7}{'по смыслу':>11}{'точн.':>7}")
    for confidence, item in c["by_confidence"].items():
        add(
            f"  {_CONFIDENCE_LABELS[confidence]:24}{item['suggested']:>11}{item['used']:>10}"
            f"{_pct(item['precision']):>7}{item['used_semantic']:>11}{_pct(item['precision_semantic']):>7}"
        )
    add("")
    add(f"Чего не подсказали (что пишут операторы, без повторов; топ {top}):")
    for item in c["missed"][:top]:
        add(f"  {item['count']:>3} × (запусков {item['groups']}) {_short(item['text'])}")
        add(f"        → {_missed_note(item)}")
    if not c["missed"]:
        add("  —")
    add("")
    add(f"Подсказано, но оператор не написал (без повторов; топ {top}):")
    for item in c["unused"][:top]:
        add(f"  {item['count']:>3} × (запусков {item['groups']}) [{item['signal']}] {_short(item['text'], 80)}")
    if not c["unused"]:
        add("  —")
    if c["fuzzy"]:
        add("")
        add("Нечёткие совпадения со справочником (проверьте):")
        for item in c["fuzzy"]:
            add(f"  {item['score']:.2f} {_short(item['raw'], 60)!r} → {_short(item['text'], 60)!r}")
    add("")
    u_raw, u = raw["uncommented"], dedup["uncommented"]
    add(
        f"Возвраты без комментария: {u_raw['returns']} (без повторов {u['returns']}); "
        f"теперь хоть что-то отмечено заранее в {u['with_preselect']}, предложено в {u['with_any']}."
    )
    add("  Отмечено заранее по сигналам (без повторов):")
    for signal, count in u["preselected_by_signal"].items():
        add(f"    {count:>3} × {signal}")
    if not u["preselected_by_signal"]:
        add("    —")
    if u["offered_by_signal"]:
        add("  Только предложено (medium/low):")
        for signal, count in u["offered_by_signal"].items():
            add(f"    {count:>3} × {signal}")
    return "\n".join(lines)


def format_orders(records: Sequence[ReturnRecord]) -> str:
    lines: list[str] = ["", "По возвратам (все, по времени):"]
    for record in records:
        comparison = compare_return(record.operator, record.suggestions)
        hits = {line.match_key for line in comparison.strict_hits}
        semantic = {line.match_key for line in comparison.semantic_hits}
        lines.append(
            f"#{record.order_id} действие {record.action_id} {record.created_at[:16]} "
            f"[{record.status}] запуск {record.run_id[:8]}"
            + ("" if record.dto_source == "repository" else f" (DTO: {record.dto_source})")
        )
        if not record.operator:
            lines.append("    оператор: — (без комментария)")
        for line in record.operator:
            mark = "+" if line.match_key in hits else ("~" if line.match_key in semantic else "-")
            source = "" if line.text is not None and line.score == 1.0 else (
                f" (≈{line.score:.2f})" if line.text is not None else " (свободный текст)"
            )
            lines.append(f"    оператор {mark} {_short(line.label, 100)}{source}")
        used = {id(item) for item in comparison.used_semantic}
        for item in record.suggestions:
            mark = "+" if id(item) in used else " "
            box = "[x]" if item["preselect"] else "[ ]"
            lines.append(
                f"    подсказка {mark} {box} {item['confidence']:6} {item['signal']:22} "
                f"{_short(item['evidence'], 70)}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--db", type=Path, default=Path("image_magic.db"), help="база SQLite (только чтение)")
    parser.add_argument("--limit", type=int, default=None, help="только N последних возвратов")
    parser.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    parser.add_argument("--show-orders", action="store_true", help="подробности по каждому возврату")
    parser.add_argument("--top", type=int, default=15, help="длина топов в отчёте")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if not args.db.is_file():
        parser.error(f"база не найдена: {args.db}")

    records = load_returns(args.db.resolve(), args.limit)
    deduped = dedupe(records)
    raw_stats, dedup_stats = build_stats(records), build_stats(deduped)
    if args.json:
        payload: dict[str, Any] = {"all": raw_stats, "deduped": dedup_stats}
        if args.show_orders:
            payload["orders"] = [
                {
                    "action_id": r.action_id, "run_id": r.run_id, "order_id": r.order_id,
                    "created_at": r.created_at, "status": r.status, "comment": r.comment,
                    "dto_source": r.dto_source,
                    "operator": [
                        {"raw": line.raw, "text": line.text, "score": line.score, "signal": line.signal}
                        for line in r.operator
                    ],
                    "suggestions": r.suggestions,
                }
                for r in records
            ]
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    print(format_report(raw_stats, dedup_stats, top=args.top))
    if args.show_orders:
        print(format_orders(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
