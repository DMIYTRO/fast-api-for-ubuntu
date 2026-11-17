"""Автоматические поля вылета планом Callas ``size_correction`` (режим «Автоматически»).

В режиме «Автоматически принимать малые коррекции» размер файла приводится к формату
с вылетами наименее разрушительным способом. Второй шаг лестницы (после «показать
скрытые вылеты» PDF) — дорисовать поля вылета планом Callas «Коррекция размера макета»
без участия оператора: то же, что «Размер» → «Принять» на карточке ждущего заказа,
только при осмотре файла и только если сравнение подтвердило ``bleed_only`` (макет
внутри формата не изменён, ``services/callas_bleed.py``).

Кандидат — файл заказа (PDF без /Rotate с одинаковыми страницами или растр с известным
размером), чей размер в любой ориентации (50×90 == 90×50) лежит между форматом из имени
(минус допуск) и форматом + 2 вылета, но ещё не совпадает с форматом + 2 вылета. Если
PDF можно раздвинуть до формата по скрытым вылетам (``reveal`` автоматически), Callas
не запускается.

Результат:

* ``bleed_only`` → решение «принять» (``CallasFixService.decide``), копия
  ``accepted.pdf`` и новая проверка файла по ней (геометрия — по исправлению,
  содержимое — по исходнику клиента), ``callas_source_fix`` как у ручного принятия
  (``baseline_pdf`` — для PitStop) и пометка ``size_fix`` ``{"method": "bleed",
  "auto": true}``; заказ не ждёт решения по этому файлу;
* не только поля, сбой Callas, занятая папка исправления — исходная проверка не
  меняется (дальше решает обычная лестница коррекции), причина пишется в журнал.

Кэш: исправление того же исходника (подпись ``size``/``mtime_ns``) с тем же форматом
и вылетом не повторяется — принятое или готовое с ``bleed_only`` используется сразу,
неудачное или «не только поля» пропускается без нового запуска Callas.

Одновременно выполняется не больше ``IMAGE_MAGIC_CALLAS_AUTO_BLEED_MAX_CONCURRENT``
(по умолчанию 2) исправлений на процесс. Хук никогда не выбрасывает исключений.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from processing.models import FileCheck, make_size_fix
from processing.resample_policy import ResampleDecision

from .callas_fix import (
    RASTER_SOURCE_SUFFIXES,
    STATUS_ACCEPTED,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_READY,
    STATUS_REJECTED,
    CallasFixError,
    CallasFixService,
    source_signature_matches,
)
from .callas_source_fix import SIZE_PLAN_KEY, mark_applied

if TYPE_CHECKING:  # processing.batch_processor импортирует services лениво
    from processing.batch_processor import BatchProcessor

logger = logging.getLogger("image_magic.processing")

DEFAULT_MAX_CONCURRENT = 2

RESULT_ACCEPTED = "accepted"
RESULT_CACHED = "cached"
RESULT_NOT_BLEED_ONLY = "not_bleed_only"
RESULT_FAILED = "failed"
RESULT_BUSY = "busy"
RESULT_SKIPPED = "skipped"

#: Совпадение размеров страниц одного PDF и формата вылета в сохранённом вердикте, мм.
_SAME_SIZE_MM = 0.05
#: Причины пропуска, которые пишутся в журнал только на уровне debug.
_QUIET_SKIPS = frozenset({"no_format", "already_fixed", "matches", "out_of_range", "unsupported"})

_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_SEMAPHORES_LOCK = threading.Lock()


def shared_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Общий на процесс семафор исправлений с пределом ``limit`` (не меньше 1)."""
    limit = max(1, int(limit))
    with _SEMAPHORES_LOCK:
        semaphore = _SEMAPHORES.get(limit)
        if semaphore is None:
            semaphore = _SEMAPHORES[limit] = threading.BoundedSemaphore(limit)
        return semaphore


def _fmt(size: tuple[float, float]) -> str:
    return f"{size[0]:.1f}×{size[1]:.1f}"


def _orientations(size: tuple[float, float]) -> tuple[tuple[float, float], tuple[float, float]]:
    return size, (size[1], size[0])


def candidate_reason(check: FileCheck, processor: "BatchProcessor") -> str | None:
    """``None`` — файл можно отдать Callas; иначе короткая причина пропуска (для журнала)."""
    parsed = check.parsed
    if parsed is None:
        return "no_format"
    if check.content_path is not None or check.callas_source_fix is not None:
        return "already_fixed"
    suffix = check.path.suffix.lower()
    if suffix != ".pdf" and suffix not in RASTER_SOURCE_SUFFIXES:
        return "unsupported"
    if check.actual_width_mm is None or check.actual_height_mm is None:
        return "size_unknown"
    actual = (float(check.actual_width_mm), float(check.actual_height_mm))
    if suffix == ".pdf":
        pages = tuple(check.pdf_pages or ())
        if not pages:
            return "no_pages"
        if any(getattr(page, "rotation", 0) for page in pages):
            return "rotated"
        if any(
            abs(page.width_mm - actual[0]) > _SAME_SIZE_MM or abs(page.height_mm - actual[1]) > _SAME_SIZE_MM
            for page in pages
        ):
            return "mixed_page_sizes"
        if _reveal_applies(check):
            return "reveal"
    elif not (check.dpi_x and check.dpi_y):
        return "dpi_unknown"
    fmt = (float(parsed.width_mm), float(parsed.height_mm))
    bleed = float(processor.size_extra_mm) / 2.0
    tolerance = float(processor.tolerance_mm)
    for width, height in _orientations(fmt):
        expected = (width + 2 * bleed, height + 2 * bleed)
        if all(abs(a - e) <= tolerance for a, e in zip(actual, expected)):
            return "matches"
    for width, height in _orientations(fmt):
        if all(
            low - tolerance <= value <= low + 2 * bleed + tolerance
            for value, low in zip(actual, (width, height))
        ):
            return None
    return "out_of_range"


def _reveal_applies(check: FileCheck) -> bool:
    """Шаг 1 лестницы: каждая страница, которую нужно менять, раздвигается автоматически.

    Решение принимает ``processing/pdf_resize_policy.py`` (``check.pdf_resize_plans``,
    заполняется только при включённой коррекции размера PDF QuickFix callas).
    """
    plans = [
        plan for plan in (check.pdf_resize_plans or {}).values()
        if getattr(plan, "decision", None) != ResampleDecision.ACCEPT
    ]
    return bool(plans) and all(
        getattr(plan, "method", None) == "reveal"
        and getattr(plan, "decision", None) == ResampleDecision.AUTO_CORRECT
        for plan in plans
    )


def _bleed_check_matches(state: Mapping[str, Any], fmt: tuple[float, float], bleed: float) -> bool:
    """Сохранённый вердикт посчитан для того же формата (любая ориентация) и вылета."""
    summary = state.get("summary") or {}
    check = summary.get("bleed_check") if isinstance(summary, Mapping) else None
    if not isinstance(check, Mapping):
        return False
    stored_format = check.get("format_mm")
    stored_bleed = check.get("bleed_mm")
    if not isinstance(stored_format, (list, tuple)) or len(stored_format) != 2 or stored_bleed is None:
        return False
    try:
        stored = (float(stored_format[0]), float(stored_format[1]))
        if abs(float(stored_bleed) - bleed) > _SAME_SIZE_MM:
            return False
    except (TypeError, ValueError):
        return False
    return any(
        all(abs(a - b) <= _SAME_SIZE_MM for a, b in zip(stored, orientation))
        for orientation in _orientations(fmt)
    )


def _state_plan(state: Mapping[str, Any]) -> str | None:
    plan = state.get("plan")
    return plan.get("key") if isinstance(plan, Mapping) else None


def _bleed_only(state: Mapping[str, Any]) -> bool:
    summary = state.get("summary")
    return isinstance(summary, Mapping) and summary.get("bleed_only") is True


class CallasAutoBleed:
    """Хук ``BatchProcessor(auto_bleed=...)``: поля вылета Callas при осмотре файла.

    Вызов ``hook(check, processor)`` возвращает новую проверку файла, собранную из
    принятого исправления, или ``None`` — исходная проверка остаётся как есть.
    """

    def __init__(
        self,
        service: CallasFixService,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        semaphore: threading.Semaphore | None = None,
    ) -> None:
        self.service = service
        self.semaphore = semaphore if semaphore is not None else shared_semaphore(max_concurrent)

    def __call__(self, check: FileCheck, processor: "BatchProcessor") -> FileCheck | None:
        started = time.monotonic()
        result = RESULT_SKIPPED
        reason = ""
        try:
            reason = candidate_reason(check, processor) or ""
            if reason:
                return None
            replacement, result, reason = self._apply(check, processor)
            return replacement
        except Exception as exc:  # noqa: BLE001 — осмотр заказа не должен падать
            logger.warning("order.callas_auto_bleed_error file=%s", check.path.name, exc_info=True)
            result, reason = RESULT_FAILED, str(exc).strip() or type(exc).__name__
            return None
        finally:
            # Файлы, которым поля не нужны (размер верный, вне диапазона), — только debug:
            # иначе большая папка даёт строку журнала на каждый файл.
            quiet = result == RESULT_SKIPPED and reason in _QUIET_SKIPS
            logger.log(
                logging.DEBUG if quiet else logging.INFO,
                "order.callas_auto_bleed file=%s result=%s reason=%s seconds=%.1f",
                check.path.name, result, reason or "-", time.monotonic() - started,
            )

    # ----------------------------------------------------------------- steps
    def _apply(
        self, check: FileCheck, processor: "BatchProcessor"
    ) -> tuple[FileCheck | None, str, str]:
        source = check.path
        parsed = check.parsed
        assert parsed is not None
        fmt = (float(parsed.width_mm), float(parsed.height_mm))
        bleed = float(processor.size_extra_mm) / 2.0
        state = self.service.read_state(source)
        cached = False
        if state and source_signature_matches(source, state.get("source")):
            status = state.get("status")
            if status == STATUS_PROCESSING:
                return None, RESULT_BUSY, "fix_running"
            if _state_plan(state) != SIZE_PLAN_KEY:
                if status in {STATUS_READY, STATUS_ACCEPTED}:
                    # Результат другого плана, который оператор ещё может принять: не трогаем.
                    return None, RESULT_SKIPPED, f"other_plan:{_state_plan(state)}"
            elif status == STATUS_REJECTED:
                return None, RESULT_SKIPPED, "rejected_by_operator"
            elif status == STATUS_FAILED:
                # Сбой (в том числе временная недоступность Callas) не запоминается:
                # при следующей проверке исправление запускается снова.
                pass
            elif _bleed_check_matches(state, fmt, bleed):
                if not _bleed_only(state):
                    return None, RESULT_NOT_BLEED_ONLY, "cached"
                if status in {STATUS_READY, STATUS_ACCEPTED}:
                    cached = True
        if not cached:
            state = self._run_fix(source, fmt, bleed, processor)
            if state is None:
                return None, RESULT_BUSY, "fix_busy"
            if state.get("status") != STATUS_READY:
                return None, RESULT_FAILED, str(state.get("error") or "callas_failed")
            if not _bleed_only(state):
                reasons = (state.get("summary") or {}).get("bleed_only_reasons") or ()
                return None, RESULT_NOT_BLEED_ONLY, "; ".join(str(item) for item in reasons)
        replacement = self._accept(check, processor, state or {}, fmt, bleed)
        if replacement is None:
            return None, RESULT_NOT_BLEED_ONLY, "size_mismatch"
        return replacement, RESULT_CACHED if cached else RESULT_ACCEPTED, ""

    def _run_fix(
        self, source: Path, fmt: tuple[float, float], bleed: float, processor: "BatchProcessor"
    ) -> dict[str, Any] | None:
        """Запустить план синхронно; ``None`` — папка исправления занята."""
        with self.semaphore:
            try:
                self.service.start(source, SIZE_PLAN_KEY)
            except CallasFixError as exc:
                if exc.code == "callas_fix_busy":
                    return None
                raise
            return self.service.execute(
                source,
                SIZE_PLAN_KEY,
                format_mm=fmt,
                bleed_mm=bleed,
                strip_icc_cmyk=bool(getattr(processor, "strip_icc_cmyk", False)),
            )

    def _accept(
        self,
        check: FileCheck,
        processor: "BatchProcessor",
        state: Mapping[str, Any],
        fmt: tuple[float, float],
        bleed: float,
    ) -> FileCheck | None:
        """«Принять» без оператора; ``None`` — размер после исправления не совпал (решение снято)."""
        source = check.path
        previous = dict(state)
        decided = self.service.decide(source, "accept")
        try:
            content, fix = self.service.accepted_source_copy(source)
            fix = {**fix, "auto": True}
            replacement = processor.reinspect_pdf_file(check, content, fix=fix)
            if not self._size_fixed(replacement, fmt, bleed, processor):
                if previous.get("status") != STATUS_ACCEPTED:
                    self.service.restore_decision(source, previous)
                return None
        except Exception:
            if previous.get("status") != STATUS_ACCEPTED and decided.get("status") == STATUS_ACCEPTED:
                self.service.restore_decision(source, previous)
            raise
        mark_applied(replacement, fix)
        before = (float(check.actual_width_mm), float(check.actual_height_mm))
        after = (float(replacement.actual_width_mm), float(replacement.actual_height_mm))
        replacement.size_fix = make_size_fix(
            "bleed",
            True,
            before,
            after,
            f"Callas дорисовал поля вылета {bleed:g} мм: {_fmt(before)} → {_fmt(after)} мм, "
            "макет внутри формата не изменён",
        )
        return replacement

    @staticmethod
    def _size_fixed(
        replacement: FileCheck, fmt: tuple[float, float], bleed: float, processor: "BatchProcessor"
    ) -> bool:
        if replacement.resample_decision != ResampleDecision.ACCEPT.value or replacement.needs_resample:
            return False
        if replacement.actual_width_mm is None or replacement.actual_height_mm is None:
            return False
        actual = (replacement.actual_width_mm, replacement.actual_height_mm)
        tolerance = float(processor.tolerance_mm)
        return any(
            all(abs(a - (e + 2 * bleed)) <= tolerance for a, e in zip(actual, orientation))
            for orientation in _orientations(fmt)
        )
