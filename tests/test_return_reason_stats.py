"""Статистика подсказок причин возврата: разбор комментариев, сравнение, регрессия по реальным возвратам.

Форматы комментариев — дословно из production (``order_actions.comment``); фикстуры
``tests/fixtures/return_suggestions/*.json`` — анонимизированные DTO реальных возвратов
(только поля, которые читает ``suggest_return_reasons``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.return_reason_rules import RULES_BY_SIGNAL
from core.return_reasons import load_return_reasons
from scripts.return_reason_stats import ReturnRecord, build_stats, dedupe, format_report
from services.return_suggestion_stats import (
    CatalogueMatcher,
    OperatorLine,
    aggregate,
    comment_key,
    compare_return,
    normalize_reason,
    prefill_summary,
    semantic_signal,
    split_comment,
)
from services.return_suggestions import suggest_return_reasons

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "Цифра - Загальне.html"
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "return_suggestions").glob("*.json"))

LOW_DPI = "Низька роздільна здатність, має бути 300 dpi (точок на Inch/Дюйм)"
MIN_INK = "Мінімальна барвистість - 20%"
ICC_ALT = "Вбудовані кольорові профілі неприпустимі. При друці колір може спотворитися"
ICC = "Вбудовані колірні профілі неприпустимі. При друці колір може спотворитися"
RGB_ALT = "Всі елементи повинні бути в CMYK, присутні RGB об'єкти"
TRANSPARENCY_ALT = (
    "Складні спецефекти (тіні, лінзи, текстури, градієнтні заливки і ін.), растрові зображення на "
    "прозорому тлі неприпустимі - раструються разом з фоном без прозорості"
)
SPOT_ALT = "Всі елементи повинні бути в CMYK, Panton'и неприпустимі. При друці може вийти абсолютно інший колір"

#: Нумерованный список, за которым идёт его же склеенная (неполная) копия — действие 128.
NUMBERED_WITH_COPY = (
    f"1) {LOW_DPI};\n2) {MIN_INK};\n3) {ICC_ALT}.\n{LOW_DPI}; {MIN_INK}"
)
JOINED = f"{LOW_DPI}; {MIN_INK}; {ICC}"
FIVE_JOINED = (
    "Кількість сторінок не збігається з оформленним; Всі елементи повинні бути в CMYK, присутні RGB "
    "об'єкти; Прозорості і растрові зображення з прозорим фоном неприпустимі; Складні спецефекти "
    "(тіні, лінзи, текстури, градієнтні заливки і ін.), растрові зображення на прозорому тлі "
    "неприпустимі - раструються разом з фоном без прозорості; " + ICC
)


@pytest.fixture(scope="module")
def reasons():
    if not TEMPLATE.is_file():
        pytest.skip("справочник «Цифра - Загальне.html» отсутствует в рабочей копии")
    return load_return_reasons(TEMPLATE)


@pytest.fixture(scope="module")
def matcher(reasons):
    return CatalogueMatcher(reasons)


# --------------------------------------------------------------------------- разбор комментария


def test_single_line_with_trailing_period():
    comment = "Битий файл. Макет не відкривається. Збережіть макет ще раз і перезавантажте його."
    assert split_comment(comment) == [comment[:-1]]


def test_numbered_list_followed_by_joined_copy_is_deduplicated():
    assert split_comment(NUMBERED_WITH_COPY) == [LOW_DPI, MIN_INK, ICC_ALT]


def test_reasons_joined_by_semicolon_on_one_line():
    assert split_comment(JOINED) == [LOW_DPI, MIN_INK, ICC]
    lines = split_comment(FIVE_JOINED)
    assert len(lines) == 5
    assert lines[1] == RGB_ALT
    assert lines[3].startswith("Складні спецефекти (тіні, лінзи") and lines[3].endswith("без прозорості")


def test_numbered_list_without_copy():
    comment = (
        f"1) {LOW_DPI};\n2) Максимальна барвистість - 300%;\n"
        "3) Всі елементи повинні бути в CMYK, Panton'и неприпустимі. При друці може вийти абсолютно інший колір."
    )
    assert split_comment(comment) == [
        LOW_DPI,
        "Максимальна барвистість - 300%",
        "Всі елементи повинні бути в CMYK, Panton'и неприпустимі. При друці може вийти абсолютно інший колір",
    ]


def test_empty_comment():
    assert split_comment(None) == []
    assert split_comment("  \n ") == []


def test_catalogue_item_with_semicolons_is_not_split(matcher):
    item = next(key for key in matcher.keys if "fex.net; dropmefiles.com" in key)
    text = matcher.by_key[item]
    assert split_comment(f"{text}; {LOW_DPI}", matcher.keys) == [text, LOW_DPI]
    assert len(split_comment(text)) > 1  # без справочника — разрезается


def test_normalize_reason_unifies_variants():
    assert normalize_reason("2) Всі елементи … Panton’и неприпустимі;") == normalize_reason(
        "всі елементи … panton'и неприпустимі."
    )
    assert normalize_reason("Розмір 94х54мм") == normalize_reason("розмір 94x54мм")
    assert normalize_reason("Шрифти вбудовані некоректно , при друці") == "шрифти вбудовані некоректно, при друці"


def test_comment_key_equates_repeated_returns():
    numbered = f"1) {LOW_DPI};\n2) {MIN_INK};\n3) {ICC_ALT}."
    assert comment_key(NUMBERED_WITH_COPY) == comment_key(numbered)
    assert comment_key(JOINED) != comment_key(numbered)  # «колірні» ≠ «кольорові»
    assert comment_key(None) == comment_key("")


# --------------------------------------------------------------------------- справочник


def test_matcher_exact_fuzzy_and_free_text(matcher):
    exact = matcher.match(f"1) {LOW_DPI};")
    assert (exact.text, exact.score) == (LOW_DPI, 1.0)
    assert exact.signal == "low_dpi"

    typo = matcher.match("Низька роздільна здатнысть, має бути 300 dpi (точок на Inch/Дюим")
    assert typo.text == LOW_DPI and 0.85 <= typo.score < 1.0

    free = matcher.match("Будь ласка, зателефонуйте менеджеру")
    assert free.text is None and free.signal is None and free.label == free.raw


def test_semantic_signal_aliases_and_sizes():
    assert semantic_signal(ICC) == "icc"
    assert semantic_signal(ICC_ALT) == "icc"
    assert semantic_signal(RGB_ALT) == "rgb_left"
    # Прежний текст RGB-правила (теперь — ``color_model``) — тот же смысл «RGB / CMYK».
    assert semantic_signal("Макет має бути виконаний в кольоровій моделі CMYK") == "rgb_left"
    # Варианты правил (``alternatives``) — смысл их правила.
    assert semantic_signal(TRANSPARENCY_ALT) == "transparency"
    assert semantic_signal(SPOT_ALT) == "spot_colors"
    assert semantic_signal("Розмір макета з урахуванням доливок повинен бути 94х54мм") == "size"
    assert semantic_signal(RULES_BY_SIGNAL["size_reference_sra3"].text) == "size"
    assert semantic_signal(MIN_INK) == "ink_min"
    assert semantic_signal("Мінімальна відстань від краю до лінії біговки - 10 мм") is None


def test_converted_rgb_suggestion_matches_rgb_line():
    operator = [line(RGB_ALT)]
    converted = {**suggestion("rgb_converted", "medium"), "text": RGB_ALT}
    result = compare_return(operator, [converted])
    assert len(result.strict_hits) == 1 and result.used_semantic == [converted]
    legacy = compare_return([line("Макет має бути виконаний в кольоровій моделі CMYK")],
                            [suggestion("rgb_left")])
    assert legacy.strict_hits == [] and len(legacy.semantic_hits) == 1


# --------------------------------------------------------------------------- сравнение


def line(text: str | None, raw: str | None = None) -> OperatorLine:
    raw = raw if raw is not None else str(text)
    return OperatorLine(raw=raw, key=normalize_reason(raw), text=text, score=1.0, signal=semantic_signal(text))


def suggestion(signal: str, confidence: str = "high", text: str | None = None) -> dict:
    return {
        "text": text or RULES_BY_SIGNAL[signal].text,
        "signal": signal,
        "confidence": confidence,
        "preselect": confidence == "high",
    }


def test_compare_return_strict_and_semantic():
    operator = [line(LOW_DPI), line(ICC_ALT), line(MIN_INK)]
    suggestions = [suggestion("low_dpi"), suggestion("icc"), suggestion("transparency", "medium")]
    result = compare_return(operator, suggestions)
    assert [item.text for item in result.strict_hits] == [LOW_DPI]
    assert [item.text for item in result.semantic_hits] == [LOW_DPI, ICC_ALT]
    assert [item.text for item in result.missed] == [ICC_ALT, MIN_INK]
    assert [item["signal"] for item in result.used] == ["low_dpi"]
    assert [item["signal"] for item in result.used_semantic] == ["low_dpi", "icc"]
    assert [item["signal"] for item in result.unused] == ["transparency"]


def test_size_suggestions_match_any_size_line_semantically():
    operator = [line(RULES_BY_SIGNAL["size_mismatch"].text)]
    result = compare_return(operator, [suggestion("size_reference_sra3")])
    assert result.strict_hits == [] and len(result.semantic_hits) == 1


def test_aggregate_maths():
    first = compare_return(
        [line(LOW_DPI), line(ICC_ALT), line(None, "свободный текст")],
        [suggestion("low_dpi"), suggestion("icc"), suggestion("transparency", "medium")],
    )
    second = compare_return([line(LOW_DPI)], [suggestion("low_dpi"), suggestion("page_count")])
    third = compare_return([line(MIN_INK)], [])
    stats = aggregate([first, second, third], groups=["run-a", "run-a", "run-b"])

    assert stats["returns"] == 3
    assert stats["operator_lines"] == 5
    assert stats["operator_catalogue_lines"] == 4
    assert stats["recall_hits"] == 2 and stats["recall"] == 0.4
    assert stats["recall_catalogue"] == 0.5
    assert stats["recall_semantic_hits"] == 3 and stats["recall_semantic"] == 0.6
    assert stats["returns_fully_covered"] == 1  # только second
    assert stats["preselected"] == 4 and stats["preselected_used"] == 2
    assert stats["preselect_precision"] == 0.5
    assert stats["preselected_used_semantic"] == 3 and stats["preselect_precision_semantic"] == 0.75
    assert stats["by_confidence"]["high"] == {
        "suggested": 4, "used": 2, "used_semantic": 3, "precision": 0.5, "precision_semantic": 0.75,
    }
    assert stats["by_confidence"]["medium"]["suggested"] == 1
    assert stats["by_confidence"]["medium"]["precision"] == 0.0
    assert stats["by_confidence"]["low"]["precision"] is None

    missed = {item["text"]: item for item in stats["missed"]}
    assert missed[ICC_ALT]["semantic_hits"] == 1 and missed[ICC_ALT]["signal"] == "icc"
    assert missed["свободный текст"]["catalogue"] is False
    assert missed[MIN_INK]["rule_signal"] == "ink_min" and missed[MIN_INK]["groups"] == 1
    unused = {item["signal"]: item for item in stats["unused"]}
    assert unused["page_count"]["count"] == 1 and unused["transparency"]["groups"] == 1
    assert stats["free_text"] == [{"text": "свободный текст", "count": 1}]


def test_aggregate_empty():
    stats = aggregate([])
    assert stats["recall"] is None and stats["preselect_precision"] is None


def test_prefill_summary_counts_by_signal():
    summary = prefill_summary([
        [suggestion("low_dpi"), suggestion("transparency", "medium")],
        [suggestion("low_dpi"), suggestion("icc")],
        [],
    ])
    assert summary["returns"] == 3
    assert summary["with_preselect"] == 2 and summary["with_any"] == 2
    assert summary["preselected_by_signal"] == {"low_dpi": 2, "icc": 1}
    assert summary["offered_by_signal"] == {"transparency": 1}


# --------------------------------------------------------------------------- скрипт


def record(action_id: int, order_id: str, comment: str | None, operator, suggestions) -> ReturnRecord:
    return ReturnRecord(
        action_id=action_id, run_id="run-1", order_id=order_id, created_at="2026-11-22 12:00:00",
        status="prepared", comment=comment, dto_source="repository",
        operator=list(operator), suggestions=list(suggestions),
    )


def test_dedupe_and_report():
    records = [
        record(1, "100", NUMBERED_WITH_COPY, [line(LOW_DPI)], [suggestion("low_dpi")]),
        record(2, "100", f"1) {LOW_DPI};\n2) {MIN_INK};\n3) {ICC_ALT}.", [line(LOW_DPI)], [suggestion("low_dpi")]),
        record(3, "100", None, [], [suggestion("low_dpi")]),
        record(4, "200", JOINED, [line(LOW_DPI)], []),
    ]
    deduped = dedupe(records)
    assert [item.action_id for item in deduped] == [2, 3, 4]
    raw_stats, dedup_stats = build_stats(records), build_stats(deduped)
    assert (raw_stats["returns"], raw_stats["with_comment"], raw_stats["without_comment"]) == (4, 3, 1)
    assert dedup_stats["returns"] == 3
    report = format_report(raw_stats, dedup_stats)
    assert "Полнота дословно" in report and "Возвраты без комментария" in report
    assert "оценка снизу" in report


# --------------------------------------------------------------------------- регрессия


def test_fixture_set_is_present():
    assert len(FIXTURES) >= 6


@pytest.mark.parametrize("path", FIXTURES, ids=[path.stem for path in FIXTURES])
def test_real_return_fixture(path, reasons):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    got = suggest_return_reasons(fixture["dto"], reasons)
    assert [
        {"signal": item["signal"], "confidence": item["confidence"], "preselect": item["preselect"]}
        for item in got
    ] == fixture["expected"], fixture["description"]
