"""Применение политики проверки к DTO PitStop (см. ``config/check_policies.py``).

Политика решает, какие находки PitStop влияют на статус заказа. Находка, которую
политика считает справочной, понижается до ``information`` (с пометкой
``policy_demoted``): она остаётся видна, но не меняет ни число ошибок, ни вердикт.

Порядок в конвейере: сырой DTO PitStop → сравнение с исходником
(``services/pitstop/baseline.py``) → политика → итоговый статус
(``services/pdf_routing.completed_status``). Политика не трогает находки,
которые сравнение уже скрыло (``hidden_by_baseline``), и технические сбои проверки.

Функции чистые: входной DTO не меняется, результат — глубокая копия. Применение
идемпотентно: к уже обработанному DTO сначала возвращается состояние до политики.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.check_policies import (
    CATEGORIES,
    CHECK_POLICY_FIELDS,
    DEFAULT_INPUT_CHECK_POLICY,
    DEFAULT_STRICT_SOURCE_DIRS,
    INFO,
    ISSUE_POLICY_FIELDS,
    POLICIES,
    POLICY_FULL,
    RUN_OPTION_FULL,
    CheckCategory,
    CheckPolicy,
)
from services.pitstop.baseline import verdict_from_counts

logger = logging.getLogger("image_magic.check_policy")

# Уровни находок, которые влияют на вердикт (те же, что в baseline.py).
_COUNT_KEYS = {
    "critical_failure": "critical_failures",
    "error": "errors",
    "warning": "warnings",
    "noncritical_failure": "noncritical_failures",
}
_INFO_SEVERITY = "information"
_INFO_COUNT_KEY = "informations"

_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def _compiled(pattern: str) -> re.Pattern[str]:
    compiled = _PATTERN_CACHE.get(pattern)
    if compiled is None:
        compiled = _PATTERN_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
    return compiled


def classify(issue: Mapping[str, Any]) -> CheckCategory | None:
    """Категория находки PitStop или ``None``, если ни одна не подошла."""
    raw_action = issue.get("action_id")
    action = "" if raw_action is None or isinstance(raw_action, bool) else str(raw_action).strip()
    message = str(issue.get("message") or "")
    if not action and not message:
        return None
    for category in CATEGORIES:
        for match in category.matches:
            if match.action_id != action:
                continue
            # Находка без action_id подходит только под шаблон сообщения.
            if match.message_pattern is None:
                if action:
                    return category
                continue
            if _compiled(match.message_pattern).search(message):
                return category
    return None


def get_policy(key: str | CheckPolicy | None) -> CheckPolicy:
    """Политика по ключу; неизвестный ключ — ``full`` (поведение «как раньше»)."""
    if isinstance(key, CheckPolicy):
        return key
    policy = POLICIES.get(str(key or ""))
    if policy is None:
        if key:
            logger.warning("check_policy.unknown key=%s fallback=%s", key, POLICY_FULL)
        return POLICIES[POLICY_FULL]
    return policy


def restore_pre_policy(dto: Mapping[str, Any]) -> dict[str, Any]:
    """Глубокая копия DTO без следов политики (состояние после сравнения с исходником)."""
    result = deepcopy(dict(dto))
    if "counts_before_policy" in result and isinstance(result["counts_before_policy"], dict):
        result["counts"] = result["counts_before_policy"]
    if "verdict_before_policy" in result:
        result["verdict"] = result["verdict_before_policy"]
    for name in CHECK_POLICY_FIELDS:
        result.pop(name, None)
    for issue in result.get("issues") or ():
        if not isinstance(issue, dict):
            continue
        if issue.get("policy_demoted") and issue.get("policy_original_severity"):
            issue["severity"] = issue["policy_original_severity"]
        for name in ISSUE_POLICY_FIELDS:
            issue.pop(name, None)
    return result


def apply_check_policy(
    pitstop: Mapping[str, Any] | None, policy: str | CheckPolicy | None
) -> dict[str, Any] | None:
    """DTO PitStop с учётом политики: справочные находки не влияют на вердикт.

    ``full`` — только метки категорий, уровни не меняются. Иначе находки категорий с
    правилом ``info`` и уровнем error/warning/critical/noncritical_failure понижаются до
    ``information`` (``policy_demoted``, ``policy_original_severity``), счётчики и
    ``verdict`` пересчитываются (исходные — ``counts_before_policy`` /
    ``verdict_before_policy``). Находка без категории в политике с правилами сохраняет
    уровень и получает ``policy_unclassified``. Незавершённая проверка PitStop
    (технический сбой) не меняется.
    """
    if pitstop is None:
        return None
    resolved = get_policy(policy)
    result = restore_pre_policy(pitstop)
    result["check_policy"] = resolved.key
    issues = [issue for issue in result.get("issues") or () if isinstance(issue, dict)]
    categories = [classify(issue) for issue in issues]
    for issue, category in zip(issues, categories):
        if category is not None:
            issue["policy_category"] = category.key
            issue["policy_category_label"] = category.label
    if result.get("execution_status") != "completed":
        return result
    counts = dict(result.get("counts") or {})
    raw_counts = dict(counts)
    verdict_before = result.get("verdict")
    demoted = False
    for issue, category in zip(issues, categories):
        severity = str(issue.get("severity") or "")
        if severity not in _COUNT_KEYS or issue.get("hidden_by_baseline"):
            continue
        rule = resolved.rule_for(category.key if category else None)
        if rule == INFO:
            issue["policy_demoted"] = True
            issue["policy_original_severity"] = severity
            issue["severity"] = _INFO_SEVERITY
            source_key = _COUNT_KEYS[severity]
            counts[source_key] = max(0, int(counts.get(source_key) or 0) - 1)
            counts[_INFO_COUNT_KEY] = int(counts.get(_INFO_COUNT_KEY) or 0) + 1
            demoted = True
        elif category is None and resolved.rules:
            issue["policy_unclassified"] = True
    if demoted:
        result["counts_before_policy"] = raw_counts
        result["verdict_before_policy"] = verdict_before
        result["counts"] = counts
        result["verdict"] = verdict_from_counts(counts)
    return result


@dataclass(frozen=True)
class PolicySettings:
    """Настройки политики проверки (то, что ``resolve_order_policy`` читает из ``Settings``)."""

    input_check_policy: str = DEFAULT_INPUT_CHECK_POLICY
    strict_source_dirs: tuple[str, ...] = DEFAULT_STRICT_SOURCE_DIRS


def resolve_order_policy(
    source_folders: Iterable[str],
    run_option: str | None,
    settings: Any,
) -> dict[str, str]:
    """Политика заказа: ``{"key", "label", "reason"}``.

    ``source_folders`` — папка каждого файла заказа (``""`` — корень входной папки).
    Любой файл из строгой папки → ``full`` (``strict_folder``); выбор «полная» при запуске
    → ``full`` (``run_option``); политика настроек ``full`` → ``full`` (``disabled``); иначе —
    политика корня входной папки (``input_root``).
    """
    folders = [str(folder) for folder in source_folders]
    strict = next((folder for folder in folders if folder), None)
    if strict is not None:
        return {
            "key": POLICY_FULL,
            "label": f"Полная проверка ({strict})",
            "reason": "strict_folder",
        }
    if run_option == RUN_OPTION_FULL:
        return {
            "key": POLICY_FULL,
            "label": "Полная проверка (выбрана при запуске)",
            "reason": "run_option",
        }
    configured = get_policy(
        getattr(settings, "input_check_policy", DEFAULT_INPUT_CHECK_POLICY)
    )
    if configured.key == POLICY_FULL:
        return {"key": POLICY_FULL, "label": configured.label, "reason": "disabled"}
    return {"key": configured.key, "label": configured.label, "reason": "input_root"}


def stored_order_policy(
    order: Mapping[str, Any], run_options: Mapping[str, Any] | None, settings: Any
) -> dict[str, str]:
    """Политика сохранённого заказа: записанная при проверке, иначе вычисленная заново."""
    stored = order.get("check_policy")
    if isinstance(stored, Mapping) and stored.get("key") in POLICIES:
        return dict(stored)  # type: ignore[arg-type]
    folders = [str(item.get("source_folder") or "") for item in order.get("files") or ()
               if isinstance(item, Mapping)]
    return resolve_order_policy(
        folders, (run_options or {}).get("check_policy"), settings
    )


_strict_names: frozenset[str] = frozenset(
    name.casefold() for name in DEFAULT_STRICT_SOURCE_DIRS
)


def set_strict_source_dirs(names: Sequence[str]) -> None:
    """Запомнить папки полной проверки из настроек (вызывает ``create_app``).

    Нужно только для вывода корня входной папки из пути исходника
    (``source_output_root``), когда под рукой нет ни настроек, ни запуска.
    """
    global _strict_names
    _strict_names = frozenset(str(name).casefold() for name in names)


def source_output_root(source: Path | str) -> Path:
    """Папка, под которой лежат ``PDF/``, ``Previews/`` и прочие результаты для исходника.

    Обычно это папка исходника; у файла из папки полной проверки (``CorelDRAW``) —
    входная папка над ней (результаты не пишутся внутрь папки исходников). Папка
    считается папкой исходников, только если рядом с ней уже есть ``PDF/``.
    """
    parent = Path(source).parent
    if parent.name.casefold() in _strict_names and (parent.parent / "PDF").is_dir():
        return parent.parent
    return parent


__all__ = [
    "PolicySettings",
    "apply_check_policy",
    "classify",
    "get_policy",
    "resolve_order_policy",
    "restore_pre_policy",
    "set_strict_source_dirs",
    "source_output_root",
    "stored_order_policy",
]
