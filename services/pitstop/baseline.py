"""Сравнение PitStop-проверки исправленного PDF с проверкой исходного PDF клиента.

Исправление Callas (например, ``size_correction``) само добавляет в PDF обтравочные
контуры и иногда прозрачность, и PitStop затем сообщает о них как о проблемах макета.
Здесь из DTO проверки исправленной ревизии вычитается то, что было в исходном PDF
(``baseline``): объекты клиента остаются ошибкой, добавленное исправлением — нет.

Функции чистые: входные DTO не меняются, результат — новый словарь. Отчёты PitStop
(JSON/XML) не трогаются, повторных запусков PitStop нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


BASELINE_APPLIED = "applied"
BASELINE_MISSING = "missing"
BASELINE_FAILED = "failed"
BASELINE_DISABLED = "disabled"

DEFAULT_FIX_BASELINE_ACTIONS = frozenset({"2079", "3004"})
DEFAULT_FIX_BASELINE_PROFILES = frozenset({"digital"})

# Поля, которые добавляет сравнение; при повторном применении снимаются, и расчёт
# идёт заново от «сырых» значений PitStop.
ISSUE_BASELINE_FIELDS = (
    "baseline_occurrences",
    "introduced_by_fix",
    "hidden_by_baseline",
    "original_severity",
)
CHECK_BASELINE_FIELDS = ("verdict_raw", "counts_raw", "baseline_status")

# Только эти уровни влияют на вердикт; сведения, исправления и подписи не трогаем.
_COUNT_KEYS = {
    "critical_failure": "critical_failures",
    "error": "errors",
    "warning": "warnings",
    "noncritical_failure": "noncritical_failures",
}
_HIDDEN_SEVERITY = "information"
_HIDDEN_COUNT_KEY = "informations"


@dataclass(frozen=True)
class FixBaselinePolicy:
    """Настройки сравнения с исходной проверкой (IMAGE_MAGIC_PITSTOP_FIX_BASELINE_*)."""

    enabled: bool = True
    actions: frozenset[str] = DEFAULT_FIX_BASELINE_ACTIONS
    profiles: frozenset[str] = DEFAULT_FIX_BASELINE_PROFILES

    def applies_to(self, profile_key: str | None) -> bool:
        return bool(self.enabled and self.actions and profile_key in self.profiles)


def is_original_check(pitstop: Mapping[str, Any] | None, current_revision: Any) -> bool:
    """Проверка первой ревизии PDF заказа (до любых исправлений и замен).

    Первая проверка всегда записывается как ревизия 1; каждая повторная проверка
    (исправление Callas, ручная замена PDF, сборка из TIFF) получает следующую.
    """
    if not pitstop:
        return False
    if current_revision is not None and str(current_revision) != "1":
        return False
    return str(pitstop.get("checked_revision")) == "1"


def profile_key_of(pitstop: Mapping[str, Any] | None) -> str | None:
    profile = (pitstop or {}).get("profile")
    key = profile.get("key") if isinstance(profile, Mapping) else None
    return str(key) if key not in (None, "") else None


def apply_fix_baseline(
    current: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
    policy: FixBaselinePolicy,
    *,
    profile_key: str | None = None,
) -> dict[str, Any] | None:
    """DTO проверки ревизии, созданной принятием исправления Callas, с учётом baseline.

    ``profile_key`` — профиль запуска, если в самом DTO его нет.
    """
    if current is None:
        return None
    key = profile_key_of(current) or profile_key
    if not policy.applies_to(key):
        result = _raw_copy(current)
        result["baseline_status"] = BASELINE_DISABLED
        return result
    baseline_key = profile_key_of(baseline)
    if baseline_key is not None and baseline_key != key:
        # Другой профиль — другие правила и уровни: сравнивать не с чем.
        baseline = None
    return diff_against_baseline(current, baseline, policy.actions)


def diff_against_baseline(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    actions: frozenset[str],
) -> dict[str, Any]:
    """Новый DTO ``current``, где учтено только добавленное после ``baseline``.

    Сопоставление — по ``action_id``. Для каждого действия из ``actions``: B — число
    вхождений в исходной проверке, C — в текущей.

    * C <= B — без изменений;
    * C > B > 0 — уровень сохраняется, ``baseline_occurrences = B``,
      ``introduced_by_fix = C - B``;
    * B == 0 — всё добавлено исправлением: ``hidden_by_baseline``, уровень
      ``information`` (исходный — в ``original_severity``), ``introduced_by_fix = C``.

    ``verdict`` и ``counts`` пересчитываются (исходные — ``verdict_raw``/``counts_raw``).
    Идемпотентно: у уже скорректированного DTO сначала восстанавливаются сырые
    значения PitStop, затем сравнение выполняется заново.
    """
    result = _raw_copy(current)
    if baseline is None:
        result["baseline_status"] = BASELINE_MISSING
        return result
    if (
        result.get("execution_status") != "completed"
        or baseline.get("execution_status") != "completed"
    ):
        result["baseline_status"] = BASELINE_FAILED
        return result

    wanted = {str(item) for item in actions}
    budget = _baseline_occurrences(baseline.get("issues") or (), wanted)
    raw_counts = dict(result.get("counts") or {})
    counts = dict(raw_counts)
    for issue in result.get("issues") or ():
        if not isinstance(issue, dict):
            continue
        action = _action_key(issue.get("action_id"))
        severity = str(issue.get("severity") or "")
        if action not in wanted or severity not in _COUNT_KEYS:
            continue
        occurrences = _occurrences(issue)
        allowed = min(occurrences, budget.get(action, 0))
        budget[action] = budget.get(action, 0) - allowed
        if occurrences <= allowed:
            continue
        issue["baseline_occurrences"] = allowed
        issue["introduced_by_fix"] = occurrences - allowed
        if allowed == 0:
            issue["hidden_by_baseline"] = True
            issue["original_severity"] = severity
            issue["severity"] = _HIDDEN_SEVERITY
            source_key = _COUNT_KEYS[severity]
            counts[source_key] = max(0, int(counts.get(source_key) or 0) - 1)
            counts[_HIDDEN_COUNT_KEY] = int(counts.get(_HIDDEN_COUNT_KEY) or 0) + 1

    result["verdict_raw"] = result.get("verdict")
    result["counts_raw"] = raw_counts
    result["counts"] = counts
    result["verdict"] = verdict_from_counts(counts)
    result["baseline_status"] = BASELINE_APPLIED
    return result


def verdict_from_counts(counts: Mapping[str, Any]) -> str:
    """Тот же вывод, что у PitStopReport.verdict: ошибки → предупреждения → прошёл."""
    if int(counts.get("errors") or 0) or int(counts.get("critical_failures") or 0):
        return "error"
    if int(counts.get("warnings") or 0) or int(counts.get("noncritical_failures") or 0):
        return "warning"
    return "passed"


def _raw_copy(dto: Mapping[str, Any]) -> dict[str, Any]:
    """Глубокая копия DTO с восстановленными значениями PitStop (без следов сравнения)."""
    result = deepcopy(dict(dto))
    if "verdict_raw" in result:
        result["verdict"] = result.get("verdict_raw")
    if isinstance(result.get("counts_raw"), dict):
        result["counts"] = result["counts_raw"]
    for field_name in CHECK_BASELINE_FIELDS:
        result.pop(field_name, None)
    for issue in result.get("issues") or ():
        if not isinstance(issue, dict):
            continue
        if issue.get("original_severity"):
            issue["severity"] = issue["original_severity"]
        for field_name in ISSUE_BASELINE_FIELDS:
            issue.pop(field_name, None)
    return result


def _baseline_occurrences(
    issues: Iterable[Any], wanted: set[str]
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for issue in issues:
        if not isinstance(issue, Mapping):
            continue
        action = _action_key(issue.get("action_id"))
        if action in wanted:
            totals[action] = totals.get(action, 0) + _occurrences(issue)
    return totals


def _action_key(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _occurrences(issue: Mapping[str, Any]) -> int:
    try:
        return max(0, int(issue.get("occurrences") or 0))
    except (TypeError, ValueError):
        return 0
