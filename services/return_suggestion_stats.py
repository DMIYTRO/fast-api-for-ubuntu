"""Сравнение подсказок причин возврата с тем, что операторы реально написали в комментарии.

Чистые функции для ``scripts/return_reason_stats.py`` (без ввода-вывода):

* ``split_comment`` — комментарий возврата → отдельные причины (форматы из production:
  одна строка с «.» в конце; нумерованный список «1) …;\\n2) …»; несколько причин через
  «; » в одной строке; нумерованный список, за которым идёт его же склеенная копия);
* ``normalize_reason`` — ключ сравнения (без нумерации и «.;» в конце, casefold, единые
  апострофы и «х»/«x»);
* ``CatalogueMatcher`` — строка оператора → пункт справочника (точное совпадение ключа,
  иначе лучшее нечёткое ≥ ``FUZZY_THRESHOLD``; без совпадения — «свободный текст»);
* ``compare_return`` / ``aggregate`` — полнота и точность подсказок.

Сравнение двоякое: *дословно* (тот же пункт справочника) и *по смыслу* (тот же сигнал:
в справочнике есть синонимичные пункты, например «колірні»/«кольорові профілі», см.
``SIGNAL_ALIASES``). Комментарий оператора — не эталон: операторы пишут не все причины,
поэтому точность — оценка снизу.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from config.return_reason_rules import CONFIDENCES, RULES

FUZZY_THRESHOLD = 0.85

#: Сигналы размера сводятся к одному смыслу «размер»: оператор пишет любой из пунктов размера.
SIZE_SIGNAL = "size"
_SIZE_SIGNALS = frozenset(
    rule.signal for rule in RULES if rule.signal.startswith("size_")
)

#: Синонимичные пункты справочника (не те, что подставляют правила, в т.ч. их
#: ``alternatives``) → сигнал. Только для сравнения «по смыслу»; подсказки по-прежнему
#: выдают текст правила.
SIGNAL_ALIASES: Mapping[str, str] = {
    "Вбудовані кольорові профілі неприпустимі. При друці колір може спотворитися": "icc",
    "Складні спецефекти неприпустимі. Тіні, лінзи, контури, прозорості, текстури, градієнтні "
    "заливки і ін. Потрібно конвертувати в растрове / векторне зображення": "transparency",
    "Усі шрифти повинні бути переведені у векторні криві, при друці текст може злетіти": "fonts",
    "Текст злетів. Усі шрифти повинні бути переведені у векторні криві": "fonts",
}

#: Сигналы одного смысла: RGB, переведённый в CMYK, — тот же пункт «RGB», что и оставшийся.
SIGNAL_SEMANTICS: Mapping[str, str] = {"rgb_converted": "rgb_left", "color_model": "rgb_left"}

_SIZE_TEXT_RE = re.compile(r"^розмір (?:макета|всіх макетів)", re.IGNORECASE)
_NUMBERING_RE = re.compile(r"^\s*(?:\d{1,2}\s*[).]|[-•*–])\s*")
_SEGMENT_SPLIT_RE = re.compile(r";\s+")
_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "‘": "'", "′": "'", "´": "'"})
_X_LETTERS = str.maketrans({"х": "x", "Х": "x", "×": "x", "X": "x"})
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?)])")
_TRAILING_RE = re.compile(r"[\s.;]+$")


def normalize_reason(text: str) -> str:
    """Ключ сравнения строки причины (см. описание модуля)."""
    value = _NUMBERING_RE.sub("", str(text or ""))
    value = value.translate(_APOSTROPHES).casefold().translate(_X_LETTERS)
    value = " ".join(value.split())
    value = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", value)
    return _TRAILING_RE.sub("", value).strip()


def split_comment(comment: str | None, known_keys: Iterable[str] = ()) -> list[str]:
    """Комментарий → причины (без нумерации и «.;» в конце), повторы убраны.

    Причины разделяются переводом строки и «; ». Пункты справочника, в тексте которых сами
    есть «; » (список файлообменников), передаются ключами в ``known_keys``: соседние куски,
    склеенные в такой пункт, остаются одной причиной.
    """
    if not comment:
        return []
    known = frozenset(known_keys)
    result: list[str] = []
    seen: set[str] = set()
    for line in str(comment).splitlines():
        segments = [part for part in _SEGMENT_SPLIT_RE.split(line.strip())]
        index = 0
        while index < len(segments):
            end = index + 1
            for candidate in range(len(segments), index + 1, -1):
                if normalize_reason("; ".join(segments[index:candidate])) in known:
                    end = candidate
                    break
            piece = "; ".join(segments[index:end])
            index = end
            text = _TRAILING_RE.sub("", _NUMBERING_RE.sub("", piece)).strip()
            key = normalize_reason(text)
            if key and key not in seen:
                seen.add(key)
                result.append(text)
    return result


def comment_key(comment: str | None) -> str:
    """Ключ комментария для повторных возвратов: те же причины в том же порядке."""
    return "\n".join(normalize_reason(line) for line in split_comment(comment))


def _rule_signals_by_text() -> dict[str, str]:
    """Текст правила или его варианта → сигнал (первое правило выигрывает: ``rgb_left``)."""
    result: dict[str, str] = {}
    for rule in RULES:
        texts = rule.alternatives if rule.dynamic else (rule.text, *rule.alternatives)
        for text in texts:
            result.setdefault(normalize_reason(text), rule.signal)
    return result


_RULE_SIGNAL_BY_TEXT: Mapping[str, str] = _rule_signals_by_text()
_ALIAS_SIGNAL_BY_KEY: Mapping[str, str] = {
    normalize_reason(text): signal for text, signal in SIGNAL_ALIASES.items()
}


def semantic_signal(text: str | None) -> str | None:
    """Смысл пункта справочника: сигнал правила, синонима или «размер»; ``None`` — нет правила."""
    if not text:
        return None
    key = normalize_reason(text)
    signal = _RULE_SIGNAL_BY_TEXT.get(key) or _ALIAS_SIGNAL_BY_KEY.get(key)
    if signal is None and _SIZE_TEXT_RE.search(key):
        return SIZE_SIGNAL
    return suggestion_semantic(signal) if signal is not None else None


def suggestion_semantic(signal: str) -> str:
    signal = SIGNAL_SEMANTICS.get(signal, signal)
    return SIZE_SIGNAL if signal in _SIZE_SIGNALS else signal


def rule_signal(text: str | None) -> str | None:
    """Сигнал правила, которое подставляет именно этот текст (размер по справочнику — динамический)."""
    if not text:
        return None
    key = normalize_reason(text)
    signal = _RULE_SIGNAL_BY_TEXT.get(key)
    if signal is None and key.startswith("розмір макета з урахуванням доливок повинен бути"):
        return "size_exact_catalogue"
    return signal


@dataclass(frozen=True)
class OperatorLine:
    """Причина из комментария оператора и её пункт справочника (``text=None`` — свободный текст)."""

    raw: str
    key: str
    text: str | None
    score: float
    signal: str | None

    @property
    def label(self) -> str:
        return self.text if self.text is not None else self.raw

    @property
    def match_key(self) -> str:
        return normalize_reason(self.text) if self.text is not None else self.key


class CatalogueMatcher:
    """Строка оператора → пункт справочника."""

    def __init__(self, reasons: Sequence[Mapping[str, Any]], threshold: float = FUZZY_THRESHOLD) -> None:
        self.threshold = threshold
        self.by_key: dict[str, str] = {}
        for reason in reasons:
            text = str(reason.get("text") or "").strip()
            if text:
                self.by_key.setdefault(normalize_reason(text), text)
        self._keys = list(self.by_key)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.by_key)

    def match(self, line: str) -> OperatorLine:
        key = normalize_reason(line)
        text = self.by_key.get(key)
        score = 1.0
        if text is None:
            best_key, score = None, 0.0
            matcher = difflib.SequenceMatcher(autojunk=False)
            matcher.set_seq2(key)
            for candidate in self._keys:
                matcher.set_seq1(candidate)
                if matcher.real_quick_ratio() < self.threshold or matcher.quick_ratio() < self.threshold:
                    continue
                ratio = matcher.ratio()
                if ratio > score:
                    best_key, score = candidate, ratio
            text = self.by_key[best_key] if best_key is not None and score >= self.threshold else None
        return OperatorLine(raw=line, key=key, text=text, score=round(score, 3), signal=semantic_signal(text))

    def split(self, comment: str | None) -> list[OperatorLine]:
        lines: list[OperatorLine] = []
        seen: set[str] = set()
        for raw in split_comment(comment, self.keys):
            line = self.match(raw)
            if line.match_key not in seen:  # «кольорові»/«колірні» с опечаткой → один пункт
                seen.add(line.match_key)
                lines.append(line)
        return lines


# --------------------------------------------------------------------------- сравнение


@dataclass
class ReturnComparison:
    """Один возврат: строки оператора против подсказок."""

    operator: list[OperatorLine]
    suggestions: list[Mapping[str, Any]]
    strict_hits: list[OperatorLine] = field(default_factory=list)
    semantic_hits: list[OperatorLine] = field(default_factory=list)
    missed: list[OperatorLine] = field(default_factory=list)
    used: list[Mapping[str, Any]] = field(default_factory=list)
    used_semantic: list[Mapping[str, Any]] = field(default_factory=list)
    unused: list[Mapping[str, Any]] = field(default_factory=list)


def compare_return(operator: Sequence[OperatorLine], suggestions: Sequence[Mapping[str, Any]]) -> ReturnComparison:
    """Дословно: пункт оператора = текст подсказки. По смыслу: совпал сигнал."""
    suggested_keys = {normalize_reason(str(item.get("text") or "")) for item in suggestions}
    suggested_signals = {suggestion_semantic(str(item.get("signal") or "")) for item in suggestions}
    operator_keys = {line.match_key for line in operator}
    operator_signals = {line.signal for line in operator if line.signal}
    result = ReturnComparison(list(operator), list(suggestions))
    for line in operator:
        if line.match_key in suggested_keys:
            result.strict_hits.append(line)
            result.semantic_hits.append(line)
        elif line.signal and line.signal in suggested_signals:
            result.semantic_hits.append(line)
            result.missed.append(line)
        else:
            result.missed.append(line)
    for item in suggestions:
        if normalize_reason(str(item.get("text") or "")) in operator_keys:
            result.used.append(item)
            result.used_semantic.append(item)
        elif suggestion_semantic(str(item.get("signal") or "")) in operator_signals:
            result.used_semantic.append(item)
        else:
            result.unused.append(item)
    return result


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def aggregate(
    comparisons: Sequence[ReturnComparison], groups: Sequence[str] | None = None
) -> dict[str, Any]:
    """Сводка по возвратам с комментарием: полнота, точность (в т.ч. по уверенности), топы.

    ``groups`` — ключ группы каждого возврата (запуск проверки): в топах рядом с числом
    возвратов — число разных групп, ведь пачка заказов одного клиента обычно возвращается
    одним комментарием.
    """
    if groups is None:
        groups = [str(index) for index in range(len(comparisons))]
    if len(groups) != len(comparisons):
        raise ValueError("groups: по одному ключу на возврат")
    operator_lines = sum(len(item.operator) for item in comparisons)
    catalogue_lines = sum(1 for item in comparisons for line in item.operator if line.text is not None)
    strict = sum(len(item.strict_hits) for item in comparisons)
    semantic = sum(len(item.semantic_hits) for item in comparisons)

    by_confidence: dict[str, dict[str, Any]] = {}
    for confidence in CONFIDENCES:
        suggested = [s for item in comparisons for s in item.suggestions if s.get("confidence") == confidence]
        used = [s for item in comparisons for s in item.used if s.get("confidence") == confidence]
        used_sem = [s for item in comparisons for s in item.used_semantic if s.get("confidence") == confidence]
        by_confidence[confidence] = {
            "suggested": len(suggested),
            "used": len(used),
            "used_semantic": len(used_sem),
            "precision": _ratio(len(used), len(suggested)),
            "precision_semantic": _ratio(len(used_sem), len(suggested)),
        }
    preselected = [s for item in comparisons for s in item.suggestions if s.get("preselect")]
    pre_used = [s for item in comparisons for s in item.used if s.get("preselect")]
    pre_used_sem = [s for item in comparisons for s in item.used_semantic if s.get("preselect")]

    missed: Counter[str] = Counter()
    missed_groups: dict[str, set[str]] = {}
    missed_info: dict[str, dict[str, Any]] = {}
    for item, group in zip(comparisons, groups):
        semantic_keys = {line.match_key for line in item.semantic_hits}
        for line in item.missed:
            missed[line.label] += 1
            missed_groups.setdefault(line.label, set()).add(group)
            info = missed_info.setdefault(line.label, {
                "catalogue": line.text is not None,
                "signal": line.signal,
                "rule_signal": rule_signal(line.text),
                "semantic_hits": 0,
            })
            if line.match_key in semantic_keys:
                info["semantic_hits"] += 1
    unused: Counter[tuple[str, str]] = Counter()
    unused_groups: dict[tuple[str, str], set[str]] = {}
    for item, group in zip(comparisons, groups):
        for suggestion in item.unused:
            key = (str(suggestion.get("text") or ""), str(suggestion.get("signal") or ""))
            unused[key] += 1
            unused_groups.setdefault(key, set()).add(group)
    free_text = Counter(line.raw for item in comparisons for line in item.operator if line.text is None)
    fuzzy = Counter(
        (line.raw, line.text, line.score)
        for item in comparisons for line in item.operator if line.text is not None and line.score < 1.0
    )
    return {
        "returns": len(comparisons),
        "returns_with_suggestions": sum(1 for item in comparisons if item.suggestions),
        "returns_fully_covered": sum(
            1 for item in comparisons if item.operator and len(item.strict_hits) == len(item.operator)
        ),
        "operator_lines": operator_lines,
        "operator_catalogue_lines": catalogue_lines,
        "recall": _ratio(strict, operator_lines),
        "recall_hits": strict,
        "recall_catalogue": _ratio(strict, catalogue_lines),
        "recall_semantic": _ratio(semantic, operator_lines),
        "recall_semantic_hits": semantic,
        "suggested": sum(len(item.suggestions) for item in comparisons),
        "preselected": len(preselected),
        "preselected_used": len(pre_used),
        "preselected_used_semantic": len(pre_used_sem),
        "preselect_precision": _ratio(len(pre_used), len(preselected)),
        "preselect_precision_semantic": _ratio(len(pre_used_sem), len(preselected)),
        "by_confidence": by_confidence,
        "missed": [
            {"text": text, "count": count, "groups": len(missed_groups[text]), **missed_info[text]}
            for text, count in missed.most_common()
        ],
        "unused": [
            {"text": text, "signal": signal, "count": count, "groups": len(unused_groups[(text, signal)])}
            for (text, signal), count in unused.most_common()
        ],
        "free_text": [{"text": text, "count": count} for text, count in free_text.most_common()],
        "fuzzy": [
            {"raw": raw, "text": text, "score": score, "count": count}
            for (raw, text, score), count in fuzzy.most_common()
        ],
    }


def prefill_summary(suggestion_lists: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Возвраты без комментария: что интерфейс теперь отметил бы сам (и что только предложил)."""
    preselected: Counter[str] = Counter()
    offered: Counter[str] = Counter()
    for items in suggestion_lists:
        for item in items:
            (preselected if item.get("preselect") else offered)[str(item.get("signal") or "")] += 1
    return {
        "returns": len(suggestion_lists),
        "with_preselect": sum(1 for items in suggestion_lists if any(i.get("preselect") for i in items)),
        "with_any": sum(1 for items in suggestion_lists if items),
        "preselected_by_signal": dict(preselected.most_common()),
        "offered_by_signal": dict(offered.most_common()),
    }
