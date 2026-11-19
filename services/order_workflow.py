"""Operator order transitions independent from the HTTP transport."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib

import pymupdf
import logging
import json
import os
import shutil

from core.share_tempdir import share_temp_dir
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.models import OrderAction, OrderResult

from .coordinator import RunCoordinator
from .domain import validate_operator_transition
from .order_kits import KitResolver, run_orders
from .file_lifecycle import FileConflictError, FileLifecycle, FileLifecycleError
from .callas_fix import read_state_file as read_callas_fix_state
from .print_readiness import is_forced_print, pitstop_ready, print_blockers
from .pdf_routing import completed_status, pdf_root_for, route_pdf
from .pdf_revision_validation import readable_pdf, validate_pdf_revision
from .return_preview import (
    prepare_return_preview_name, selected_custom_return_preview,
)
from .callas.renderer import dpi_for_longest_side, render_pdf_to_pngs
from .pitstop.baseline import FixBaselinePolicy, apply_fix_baseline, is_original_check
from .layered_tiff_print import (
    ReviewedLayeredPdfError,
    build_reviewed_layered_print_pdf,
)


logger = logging.getLogger("image_magic.order_workflow")


@dataclass(frozen=True)
class OrderActionCommand:
    order_ids: tuple[str, ...]
    run_id: str | None = None
    comment: str | None = None
    design: bool = True
    design_cost: str = "0"
    conflict_strategy: str = "replace"
    confirm_failed_processing: bool = False


#: Проверка завершена: заказ, оставшийся в ожидании коррекции, уже никто не подтвердит.
FINISHED_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
#: Сколько помним возврат комплекта: позже подзаказы снова идут через Sborka
#: (клиент мог исправить и отправить комплект заново).
KIT_RETURN_MEMORY = timedelta(days=3)
#: Статусы заказа комплекта, которые возврат комплекта уже не меняет.
TERMINAL_KIT_STATUSES = frozenset({"returned_for_rework", "accepted_for_print"})


class OrderWorkflowService:
    def __init__(
        self,
        coordinator: RunCoordinator,
        session_factory: Callable[[], Session],
        order_finder: Callable[
            [str, str | None], tuple[dict[str, Any], dict[str, Any]]
        ],
        *,
        lifecycle_factory: Callable[[Path], FileLifecycle] = FileLifecycle,
        prepress_sender: Callable[[str | list[str], str | None], dict[str, Any]] | None = None,
        rework_sender: Callable[[str, str, str, bool, str], dict[str, Any]] | None = None,
        preview_uploader: Callable[[list[Path]], list[str]] | None = None,
        reviewed_pdf_pitstop_service_factory: Callable[[Path, str], Any] | None = None,
        pdf_merger: Callable[[Any, Path, str], None] | None = None,
        pdf_page_extractor: Callable[[Path, int, Path, str], None] | None = None,
        accepted_pdf_provider: Callable[[Path], Path | None] | None = None,
        pdf_color_converter: Callable[[Path, Path, str], Any] | None = None,
        pdf_prepress_checker: Callable[..., dict[str, Any]] | None = None,
        pitstop_fix_baseline: FixBaselinePolicy | None = None,
        kit_resolver: KitResolver | None = None,
    ) -> None:
        # Комплекты Sborka (services/order_kits.py); None — заказы без комплектов.
        self.kit_resolver = kit_resolver
        self._kit_locks_guard = Lock()
        self._kit_locks: dict[tuple[str, str], Lock] = {}
        self.pdf_color_converter = pdf_color_converter
        # None — сравнение с исходной проверкой PitStop не подключено (тесты, CLI).
        self.pitstop_fix_baseline = pitstop_fix_baseline
        self.pdf_prepress_checker = pdf_prepress_checker
        self.coordinator = coordinator
        self.session_factory = session_factory
        self.order_finder = order_finder
        self.lifecycle_factory = lifecycle_factory
        self.prepress_sender = prepress_sender
        self.rework_sender = rework_sender
        self.preview_uploader = preview_uploader
        self.reviewed_pdf_pitstop_service_factory = reviewed_pdf_pitstop_service_factory
        self.pdf_merger = pdf_merger
        self.pdf_page_extractor = pdf_page_extractor
        self.accepted_pdf_provider = accepted_pdf_provider
        self._action_locks_guard = Lock()
        self._action_locks: dict[int, tuple[Lock, str]] = {}
        self._background_guard = Lock()
        self._background_actions: set[tuple[str, str, str]] = set()
        self._background_executor: ThreadPoolExecutor | None = None

    @contextmanager
    def correction_guard(self, run_id: str, order: dict[str, Any]):
        """Serialize correction acceptance with print/return for the same order."""
        with self.session_factory() as session:
            query = select(OrderResult.id).where(
                OrderResult.run_id == run_id,
                OrderResult.order_id == str(order["order_id"]),
            )
            if order.get("customer_id") is not None:
                query = query.where(OrderResult.customer_id == str(order["customer_id"]))
            stored_id = session.scalar(query)
        if stored_id is None:
            raise FileLifecycleError("Заказ для исправления не найден.")
        if self._claim_action(stored_id, "correct") is not None:
            raise FileLifecycleError("Над заказом уже выполняется другое действие.")
        try:
            yield
        finally:
            self._release_action(stored_id)

    def recheck_pdf_revision(
        self,
        run: dict[str, Any],
        order: dict[str, Any],
        candidate: Path,
        *,
        callas_fix: bool = False,
        bleed_only: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int, str]:
        """Validate a candidate before replacing the current PDF and its status.

        ``callas_fix`` — кандидат создан принятием исправления Callas: только тогда
        находки PitStop сравниваются с проверкой исходного PDF клиента (см.
        ``services/pitstop/baseline.py``). Ручная замена PDF и сборка из TIFF не
        сравниваются. ``bleed_only`` — исправление только дорисовало вылеты
        (``summary["bleed_only"]`` состояния исправления): сравниваются все действия
        PitStop, а не только ``IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS``.
        """
        direction = str(run["options"].get("direction") or "digital")
        captured_baseline, baseline = self._pitstop_baseline_for(order, callas_fix=callas_fix)
        conversion = None
        if self.pdf_color_converter is not None:
            from .callas.color_conversion import convert_rgb_to_cmyk

            conversion = convert_rgb_to_cmyk(
                candidate, self.pdf_color_converter, str(order.get("order_id") or "order")
            )
        prepress = None
        if self.pdf_prepress_checker is not None:
            # Повторяем допечатные проверки на кандидате (после исправления и перевода в CMYK):
            # число шрифтов, ppi, плашка K100 пересчитываются заново.
            try:
                prepress = self.pdf_prepress_checker(
                    candidate, str(order.get("order_id") or "order"), order.get("prepress")
                )
            except Exception as exc:  # noqa: BLE001
                prepress = {"status": "failed", "error": str(exc).strip() or exc.__class__.__name__}
        inspection = validate_pdf_revision(candidate, order, direction)
        if conversion is not None and conversion.get("status") != "converted":
            inspection = replace(
                inspection,
                warnings=(
                    *inspection.warnings,
                    f"Не удалось перевести RGB в CMYK: {conversion.get('error')}. PDF оставлен в RGB.",
                ),
            )
        revision = int(order.get("current_pdf_revision") or 0) + 1
        pitstop = None
        if readable_pdf(inspection) and self.reviewed_pdf_pitstop_service_factory is not None:
            try:
                service = self.reviewed_pdf_pitstop_service_factory(
                    Path(run["options"]["input_path"]), direction
                )
                result = service.check_pdf(candidate, profile_id=direction)
                from .batch_adapter import _pitstop_result_to_dto
                pitstop = _pitstop_result_to_dto(result, checked_revision=revision)
            except Exception as exc:
                pitstop = {
                    "execution_status": "failed", "verdict": "error",
                    "technical_error": f"Повторная проверка PitStop: {type(exc).__name__}: {exc}",
                    "issues": [], "reports": {},
                }
        elif order.get("pitstop"):
            pitstop = {
                "execution_status": "failed", "verdict": "error",
                "technical_error": "Для повторной проверки исправленного PDF не настроен PitStop.",
                "issues": [], "reports": {},
            }
        if callas_fix and pitstop is not None and self.pitstop_fix_baseline is not None:
            # Только в памяти: отчёты PitStop не меняются, повторных запусков нет.
            pitstop = apply_fix_baseline(
                pitstop,
                baseline,
                self.pitstop_fix_baseline,
                profile_key=direction,
                all_actions=bleed_only,
            )
            logger.info(
                "order.pitstop_fix_baseline order_id=%s revision=%s bleed_only=%s "
                "status=%s verdict=%s->%s",
                order.get("order_id"),
                revision,
                bleed_only,
                pitstop.get("baseline_status") if pitstop else None,
                pitstop.get("verdict_raw") if pitstop else None,
                pitstop.get("verdict") if pitstop else None,
            )
        status = completed_status(
            local_errors=not readable_pdf(inspection),
            local_warnings=bool(inspection.errors or inspection.warnings),
            pitstop=pitstop,
        )
        previews: list[str] = []
        if readable_pdf(inspection):
            preview_dir = Path(run["options"]["input_path"]) / "Previews" / "Final" / str(order.get("aggregate_id") or order["order_id"]) / f"r{revision:04d}"
            preview_dir.mkdir(parents=True, exist_ok=True)
            try:
                with pymupdf.open(candidate) as document:
                    page_count = document.page_count
                    longest = max(
                        (max(page.rect.width, page.rect.height) for page in document), default=1.0
                    )
                dpi = dpi_for_longest_side(longest, longest, 480)
                targets = [preview_dir / f"page-{index + 1}_preview.png" for index in range(page_count)]
                if targets:
                    render_pdf_to_pngs(candidate, dpi, targets)
                previews = [str(target) for target in targets]
            except Exception as exc:
                status = "error"
                inspection = replace(inspection, errors=(*inspection.errors, f"Превью исправленного PDF: {exc}"))
                previews = []
        previous = Path(str(order["pdf_path"])) if order.get("pdf_path") else None
        root = pdf_root_for(previous) if previous else Path(run["options"]["input_path"]) / "PDF"
        root.mkdir(parents=True, exist_ok=True)
        with share_temp_dir(prefix=".revision-", dir=root) as backup_dir:
            backup = Path(backup_dir) / "previous.pdf"
            if previous and previous.is_file():
                shutil.copy2(previous, backup)
            try:
                routed = route_pdf(candidate, status, pdf_dir=root, name=previous.name if previous else None)
            except OSError as exc:
                raise FileLifecycleError(f"Не удалось сохранить исправленный PDF: {exc}") from exc
            sha256 = hashlib.sha256(routed.read_bytes()).hexdigest()
            try:
                updated = self.coordinator.apply_pdf_revision(
                    run["id"], str(order.get("aggregate_id") or order["order_id"]),
                    status=status, pdf_path=str(routed), pitstop=pitstop,
                    current_pdf_revision=revision, current_pdf_sha256=sha256,
                    errors=list(inspection.errors), warnings=list(inspection.warnings),
                    preview_paths=previews,
                    color_conversion=conversion,
                    prepress=prepress,
                    pitstop_baseline=captured_baseline,
                    invalidate_pitstop_baseline=not callas_fix,
                )
            except Exception:
                # Storage did not commit the new status: restore its previous bytes.
                if previous and backup.is_file():
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, previous)
                if previous is None or routed != previous:
                    routed.unlink(missing_ok=True)
                raise
        return updated, pitstop, revision, sha256

    def _pitstop_baseline_for(
        self, order: dict[str, Any], *, callas_fix: bool
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(baseline to store now, baseline to compare with) for a PDF revision.

        The baseline is the PitStop check of the order's first PDF revision. It
        is captured once, on the first accepted Callas fix, and never replaced;
        chained fixes compare against it. After a revision that was not a
        Callas fix (manual replacement, reviewed TIFF) there is nothing to
        compare against: the PDF is no longer the client's original plus fixes.
        """
        if not callas_fix or self.pitstop_fix_baseline is None:
            return None, None
        if order.get("pitstop_baseline_invalid_since_revision") is not None:
            return None, None
        stored = order.get("pitstop_baseline")
        if stored:
            return None, stored
        current = order.get("pitstop")
        if is_original_check(current, order.get("current_pdf_revision")):
            captured = deepcopy(current)
            return captured, captured
        return None, None

    def _claim_action(self, order_result_id: int, action: str) -> str | None:
        """Claim an order without waiting; return the action already in progress."""
        with self._action_locks_guard:
            current = self._action_locks.get(order_result_id)
            if current is not None:
                return current[1]
            lock = Lock()
            lock.acquire()
            self._action_locks[order_result_id] = (lock, action)
            return None

    def _release_action(self, order_result_id: int) -> None:
        with self._action_locks_guard:
            current = self._action_locks.pop(order_result_id, None)
            if current is not None:
                current[0].release()

    @staticmethod
    def _busy_result(
        order_id: str, requested_action: str, active_action: str
    ) -> dict[str, Any]:
        if requested_action == active_action:
            return {
                "order_id": order_id,
                "status": "pending",
                "idempotent": True,
                "message": "Это действие над заказом уже выполняется.",
            }
        return {
            "order_id": order_id,
            "status": "conflict",
            "message": "Над заказом уже выполняется другое действие.",
        }

    @staticmethod
    def _terminal_status_for_action(action: str) -> str:
        return (
            "accepted_for_print"
            if action == "print"
            else "returned_for_rework"
        )

    @staticmethod
    def _invoke_lifecycle(
        method, order: dict[str, Any], conflict_strategy: str, **extra: Any
    ):
        try:
            return method(order, conflict_strategy=conflict_strategy, **extra)
        except TypeError as exc:
            if "conflict_strategy" not in str(exc) and not any(
                key in str(exc) for key in extra
            ):
                raise
            return method(order)

    @staticmethod
    def _return_preview_upload_paths(
        input_path: Path, transition: Any, preview_name: str
    ) -> list[Path]:
        """Find the previews after a return transition for one batch upload."""
        paths = [Path(path) for path in transition.preview_paths if Path(path).is_file()]
        matching = [path for path in paths if path.name == preview_name]
        if matching:
            return matching
        if preview_name:
            collage = input_path / "Previews" / "Return" / preview_name
            if not collage.is_file():
                raise FileLifecycleError(
                    f"Не найдено превью для загрузки: {preview_name}"
                )
            else:
                return [collage]
        raise FileLifecycleError("Не сформировано превью для возврата в Sborka.")

    @staticmethod
    def _remove_uploaded_previews(paths: list[Path]) -> None:
        """Remove local copies only after the remote action is committed."""
        for path in dict.fromkeys(paths):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                # The Sborka action has already completed at this point.  A
                # cleanup failure must not make a completed return retryable.
                logger.warning(
                    "preview.local_cleanup_failed file=%s error=%s", path.name, exc
                )

    def submit(self, command: OrderActionCommand, action: str) -> dict[str, list[dict[str, Any]]]:
        """Queue an operator action and return without waiting for FTP/HTTP I/O."""
        run_key = command.run_id or "active"
        keys = {(run_key, str(order_id), action) for order_id in command.order_ids}
        with self._background_guard:
            already_queued = keys & self._background_actions
            queued = keys - already_queued
            self._background_actions.update(queued)
            if queued and self._background_executor is None:
                self._background_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="image-magic-actions"
                )
            executor = self._background_executor

        if queued and executor is not None:
            future = executor.submit(self._run_background, command, action, queued)
            future.add_done_callback(self._log_background_failure)
        return {
            "items": [
                {
                    "order_id": str(order_id),
                    "status": "pending",
                    "message": (
                        "Действие уже выполняется в фоне."
                        if (run_key, str(order_id), action) in already_queued
                        else "Задача поставлена в очередь. Загрузка продолжается в фоне."
                    ),
                }
                for order_id in command.order_ids
            ]
        }

    def _run_background(
        self,
        command: OrderActionCommand,
        action: str,
        keys: set[tuple[str, str, str]],
    ) -> None:
        try:
            self.prepare(command, action)
        finally:
            with self._background_guard:
                self._background_actions.difference_update(keys)

    @staticmethod
    def _log_background_failure(future: Future[None]) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.error(
                "order.background_action_failed error=%s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    def shutdown(self) -> None:
        """Finish queued actions during a graceful server shutdown."""
        with self._background_guard:
            executor, self._background_executor = self._background_executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    def prepare(
        self, command: OrderActionCommand, action: str
    ) -> dict[str, list[dict[str, Any]]]:
        """Отправить в печать или вернуть; заказы комплекта Sborka — как одна операция."""
        if self.kit_resolver is not None and action == "reject" and self.rework_sender is not None:
            return self._prepare_reject_with_kits(command)
        if self.kit_resolver is not None and action == "print":
            return self._prepare_print_with_kits(command)
        return self._prepare(command, action)

    @contextmanager
    def _kit_lock(self, run_id: str, main_id: str):
        """Один комплект обрабатывается одним запросом за раз (UI шлёт возвраты параллельно)."""
        with self._kit_locks_guard:
            lock = self._kit_locks.setdefault((run_id, main_id), Lock())
        with lock:
            yield

    def _kit_of(self, order_ref: str, run_id: str | None, *, live: bool):
        """``(run, order, kit)``; kit — ``None`` вне комплекта или если заказ не найден."""
        try:
            run, order = self.order_finder(order_ref, run_id)
        except Exception:
            return None, None, None
        kits = self.kit_resolver.for_run(run, live=live) if self.kit_resolver else {}
        return run, order, kits.get(str(order.get("order_id") or order.get("id") or ""))

    @staticmethod
    def _order_ref(order: dict[str, Any]) -> str:
        return str(order.get("aggregate_id") or order.get("order_id") or order.get("id"))

    def _prepare_print_with_kits(self, command: OrderActionCommand) -> dict[str, list[dict[str, Any]]]:
        """Заказ комплекта, главный заказ которого уже возвращён, в печать не уходит."""
        allowed: list[str] = []
        blocked: list[dict[str, Any]] = []
        for order_ref in command.order_ids:
            run, order, kit = self._kit_of(order_ref, command.run_id, live=False)
            if kit is None:
                allowed.append(order_ref)
                continue
            main = next(
                (item for item in run_orders(run) if str(item.get("order_id")) == kit["main_id"]),
                None,
            )
            if main is not None and main.get("status") == "returned_for_rework":
                blocked.append({
                    "order_id": str(order.get("order_id")),
                    "status": "rejected",
                    "code": "kit_returned",
                    "message": (
                        f"Заказ входит в комплект {kit['main_id']}, который возвращён клиенту "
                        "на доработку; в печать не отправлен."
                    ),
                })
            else:
                allowed.append(order_ref)
        items = list(blocked)
        if allowed:
            items = self._prepare(replace(command, order_ids=tuple(allowed)), "print")["items"] + items
        return {"items": items}

    def _prepare_reject_with_kits(self, command: OrderActionCommand) -> dict[str, list[dict[str, Any]]]:
        """Возврат: комплект — через главный заказ в Sborka, остальные — только локально.

        Sborka при возврате главного заказа возвращает весь комплект, а возврат подзаказа
        не подтверждает. Поэтому в Sborka уходит только главный заказ (с комментарием
        этого запроса и превью главного), а все остальные заказы комплекта из этого
        запуска переносятся в Troubles без запроса в Sborka. Если главный уже возвращён
        (например, старый рассинхрон), остальные просто догоняются локально.
        """
        plain: list[str] = []
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for order_ref in command.order_ids:
            run, order, kit = self._kit_of(order_ref, command.run_id, live=True)
            if kit is None:
                plain.append(order_ref)
                continue
            key = (str(run.get("id") or ""), str(kit["main_id"]))
            group = groups.setdefault(key, {"kit": kit, "refs": []})
            group["refs"].append(order_ref)
        items: list[dict[str, Any]] = []
        if plain:
            items.extend(self._prepare(replace(command, order_ids=tuple(plain)), "reject")["items"])
        for (run_id, main_id), group in groups.items():
            with self._kit_lock(run_id, main_id):
                items.extend(self._reject_kit(command, run_id, main_id, group["kit"], group["refs"]))
        return {"items": items}

    def _reject_kit(
        self,
        command: OrderActionCommand,
        run_id: str,
        main_id: str,
        kit: dict[str, Any],
        selected_refs: list[str],
    ) -> list[dict[str, Any]]:
        """Возврат комплекта (вызывается под блокировкой комплекта).

        1. Комплект уже возвращён (главный — в этом запуске или недавно у нас) — все
           заказы комплекта этого запуска возвращаются локально.
        2. Главный в запуске — в Sborka уходит главный, остальные — локально.
        3. Главного в запуске нет (придёт позже или уже в Troubles без записи) — в Sborka
           уходит номер главного с комментарием и превью выбранного заказа, остальные —
           локально. Заказы комплекта из следующих проверок догоняются по п. 1.
        Заказы, которые ещё проверяются, не трогаются: их догоняют после проверки.
        """
        run, _ = self.order_finder(selected_refs[0], command.run_id)  # свежее состояние
        by_id = {str(order.get("order_id")): order for order in run_orders(run)}
        selected_ids = [
            str((self._kit_of(ref, command.run_id, live=False)[1] or {}).get("order_id") or ref)
            for ref in selected_refs
        ]
        size = kit.get("size") or len(kit.get("member_ids") or [])
        members = [by_id[m] for m in kit.get("member_ids") or [] if m in by_id]
        main = by_id.get(main_id)
        items: list[dict[str, Any]] = []
        busy = {
            str(order.get("order_id")) for order in members
            if self._order_in_progress(run_id, self._order_ref(order))
        }
        kit_returned = (
            main is not None and main.get("status") == "returned_for_rework"
        ) or self._kit_returned_in_history(main_id)

        if not kit_returned:
            if main is not None:
                if main_id in busy:
                    return [self._kit_busy_item(order_id, main_id) for order_id in selected_ids]
                carrier, send_as = main, {}
            else:
                # Главного нет в проверке: возврат комплекта — по номеру главного заказа,
                # с превью и комментарием первого выбранного готового заказа.
                carrier = next(
                    (
                        by_id[order_id] for order_id in selected_ids
                        if order_id in by_id and order_id not in busy
                        and by_id[order_id].get("status") not in TERMINAL_KIT_STATUSES
                    ),
                    None,
                )
                if carrier is None:
                    return [self._kit_busy_item(order_id, main_id) for order_id in selected_ids]
                send_as = {str(carrier.get("order_id")): main_id}
            carrier_items = self._prepare(
                replace(command, order_ids=(self._order_ref(carrier),)), "reject", kit_send_as=send_as,
            )["items"]
            items.extend(carrier_items)
            if not any(item.get("status") == "prepared" for item in carrier_items):
                failed = next((item.get("message") for item in carrier_items if item.get("message")), "")
                items.extend(
                    {
                        "order_id": order_id,
                        "status": "rejected",
                        "code": "kit_main_failed",
                        "message": (
                            f"Комплект {main_id} не возвращён: не удалось вернуть главный заказ "
                            f"{carrier.get('order_id')}. {failed}"
                        ).strip(),
                    }
                    for order_id in selected_ids if order_id != str(carrier.get("order_id"))
                )
                return items
            logger.info(
                "order.kit_returned run_id=%s main_id=%s size=%s via=%s",
                run_id, main_id, size, carrier.get("order_id"),
            )
            run, _ = self.order_finder(selected_refs[0], command.run_id)
            by_id = {str(order.get("order_id")): order for order in run_orders(run)}
            members = [by_id[m] for m in kit.get("member_ids") or [] if m in by_id]
        else:
            logger.info("order.kit_already_returned run_id=%s main_id=%s", run_id, main_id)

        pending = [
            order for order in members
            if order.get("status") not in TERMINAL_KIT_STATUSES
            and str(order.get("order_id")) not in busy
        ]
        if pending:
            local = {str(order.get("order_id")): main_id for order in pending}
            items.extend(
                self._prepare(
                    replace(command, order_ids=tuple(self._order_ref(order) for order in pending)),
                    "reject",
                    kit_local=local,
                )["items"]
            )
        reported = {str(item.get("order_id")) for item in items}
        for order in members:
            order_id = str(order.get("order_id"))
            if order_id in reported:
                continue
            if order_id in busy:
                items.append(self._kit_busy_item(order_id, main_id))
            elif order.get("status") == "returned_for_rework" and order_id in selected_ids:
                items.append({"order_id": order_id, "status": "prepared", "idempotent": True})
            elif order.get("status") == "accepted_for_print":
                items.append({
                    "order_id": order_id,
                    "status": "rejected",
                    "code": "kit_member_printed",
                    "message": (
                        f"Заказ уже отправлен в печать, а Sborka вернула клиенту весь комплект "
                        f"{main_id}: проверьте этот заказ вручную."
                    ),
                })
        return items

    @staticmethod
    def _kit_busy_item(order_id: str, main_id: str) -> dict[str, Any]:
        return {
            "order_id": order_id,
            "status": "rejected",
            "code": "order_in_progress",
            "message": (
                f"Заказ комплекта {main_id} ещё проверяется: дождитесь окончания проверки и "
                "верните его снова — повторного запроса в Sborka не будет."
            ),
        }

    def _order_in_progress(self, run_id: str, order_ref: str) -> bool:
        probe = getattr(self.coordinator, "order_in_progress", None)
        return bool(probe(run_id, order_ref)) if probe is not None else False

    def _kit_returned_in_history(self, main_id: str) -> bool:
        """Комплект недавно возвращён у нас: главный в другой проверке или возврат
        «в составе комплекта» с этим главным (за ``KIT_RETURN_MEMORY``)."""
        since = datetime.now(timezone.utc).replace(tzinfo=None) - KIT_RETURN_MEMORY
        marker = f'"kit_main_id": "{main_id}"'
        with self.session_factory() as session:
            main_returned = session.scalar(
                select(OrderResult.id).where(
                    OrderResult.order_id == main_id,
                    OrderResult.status == "returned_for_rework",
                    OrderResult.updated_at >= since,
                ).limit(1)
            )
            if main_returned is not None:
                return True
            return session.scalar(
                select(OrderAction.id).where(
                    OrderAction.action == "reject",
                    OrderAction.status == "prepared",
                    OrderAction.created_at >= since,
                    OrderAction.cms_response_json.contains(marker),
                ).limit(1)
            ) is not None

    def _prepare(
        self,
        command: OrderActionCommand,
        action: str,
        kit_local: Mapping[str, str] | None = None,
        kit_send_as: Mapping[str, str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """``kit_local`` — заказы комплекта, возвращаемые без запроса в Sborka:
        {номер заказа: главный заказ, уже возвращённый в Sborka}. ``kit_send_as`` —
        {номер заказа: главный заказ}: возврат в Sborka уходит под номером главного
        (главного нет в проверке), превью и комментарий — этого заказа."""
        kit_local = kit_local or {}
        kit_send_as = kit_send_as or {}
        results: list[dict[str, Any]] = []
        current_identity = {"order_id": "", "aggregate_id": "", "include_aggregate_id": False}

        def add_result(item: dict[str, Any]) -> None:
            item.setdefault("order_id", current_identity["order_id"])
            if current_identity["include_aggregate_id"]:
                item.setdefault("aggregate_id", current_identity["aggregate_id"])
            results.append(item)

        batch_prepress_result = None
        resolved_orders = [
            (order_ref, *self.order_finder(order_ref, command.run_id))
            for order_ref in command.order_ids
        ]
        external_order_ids = [
            str(order.get("order_id") or order.get("id") or order_ref)
            for order_ref, _run, order in resolved_orders
        ]
        ambiguous_external_refs: set[tuple[str, str]] = set()
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            run_orders = run.get("orders") or {}
            order_values = run_orders.values() if isinstance(run_orders, dict) else run_orders
            matching_order_identities = [
                str(candidate.get("aggregate_id") or candidate.get("order_id") or candidate.get("id") or "")
                for candidate in order_values
                if str(candidate.get("order_id") or candidate.get("id") or "") == real_id
            ]
            if len(set(matching_order_identities)) > 1:
                ambiguous_external_refs.add(
                    (str(run.get("id") or ""), str(order.get("aggregate_id") or order_ref))
                )
        selected_identities: dict[str, set[tuple[str, str]]] = {}
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            selected_identities.setdefault(real_id, set()).add(
                (str(run.get("id") or ""), str(order.get("customer_id") or ""))
            )
        duplicate_external_ids = {
            order_id
            for order_id, identities in selected_identities.items()
            if len(identities) > 1
        }
        for order_ref, run, order in resolved_orders:
            real_id = str(order.get("order_id") or order.get("id") or order_ref)
            if real_id in duplicate_external_ids:
                ambiguous_external_refs.add(
                    (str(run.get("id") or ""), str(order.get("aggregate_id") or order_ref))
                )
        sends_to_external_service = (
            (action == "print" and self.prepress_sender is not None)
            or (action == "reject" and self.rework_sender is not None)
        )
        has_reviewed_layered_files = any(
            file.get("has_unflattened_layers")
            and str(file.get("path") or "").lower().endswith((".tif", ".tiff"))
            for _, _, order in resolved_orders
            for file in order.get("files") or []
        )
        if (
            action == "print"
            and self.prepress_sender is not None
            and len(external_order_ids) > 1
            and not duplicate_external_ids
            and not has_reviewed_layered_files
        ):
            # Send only orders that will really pass the local validation:
            # the external status change cannot be rolled back if a later
            # local check rejects an order.
            eligible_external_ids = [
                str(order.get("order_id") or order.get("id") or order_ref)
                for order_ref, _run, order in resolved_orders
                if self._batch_print_eligible(order, command)
            ]
            if len(eligible_external_ids) > 1:
                batch_prepress_result = self.prepress_sender(
                    eligible_external_ids, None
                )
        with self.session_factory() as session:
            for order_ref, run, order in resolved_orders:
                order_id = str(order.get("order_id") or order.get("id") or order_ref)
                aggregate_id = str(order.get("aggregate_id") or order_ref)
                customer_id = order.get("customer_id")
                if customer_id is not None:
                    customer_id = str(customer_id)
                current_identity["order_id"] = order_id
                current_identity["aggregate_id"] = aggregate_id
                current_identity["include_aggregate_id"] = order_ref == aggregate_id and aggregate_id != order_id
                if self._order_in_progress(str(run.get("id") or ""), aggregate_id):
                    # Проверка ещё пишет результат этого заказа: перенос файлов сейчас
                    # перезаписался бы её итогом (возврат комплекта 26234881, 18.11).
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "code": "order_in_progress",
                            "message": "Заказ ещё проверяется: дождитесь окончания обработки и повторите.",
                        }
                    )
                    continue
                if sends_to_external_service and (str(run.get("id") or ""), aggregate_id) in ambiguous_external_refs:
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "code": "ambiguous_external_order_id",
                            "message": (
                                "Внешняя система не различает клиентов с одинаковым "
                                "номером заказа; действие не выполнено."
                            ),
                        }
                    )
                    continue
                order_status = order.get("status")
                if (
                    action == "reject"
                    and order_status == "waiting_confirmation"
                    and str(run.get("status") or "") not in FINISHED_RUN_STATUSES
                ):
                    # Проверка ещё ждёт решения по коррекции этого заказа: её итог перезаписал
                    # бы возврат (комплект 26237111, 19.11: «недопустимый переход заказа»).
                    # В завершённой (упавшей, прерванной) проверке решить уже нельзя — такой
                    # заказ возвращается как есть.
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            "code": "correction_pending",
                            "message": (
                                f"Заказ {order_id} ждёт решения по коррекции размера: сначала "
                                "нажмите «Применить» или «Отклонить» в карточке заказа, дождитесь "
                                "окончания его проверки и повторите возврат."
                            ),
                        }
                    )
                    continue
                fix_state = (
                    read_callas_fix_state(order["pdf_path"])
                    if order.get("pdf_path")
                    else None
                )
                # Красный заказ уходит в печать только с явным подтверждением оператора
                # и только если готов итоговый PDF (см. print_readiness).
                forced_print = (
                    action == "print"
                    and order_status == "error"
                    and command.confirm_failed_processing
                    and is_forced_print(order, fix_state)
                )
                if (
                    action == "print"
                    and order_status
                    not in {"accepted_for_print", "returned_for_rework"}
                    and not (
                        (
                            order_status in {"passed", "warning"}
                            and pitstop_ready(order)
                        )
                        or forced_print
                    )
                ):
                    blockers = print_blockers(order, fix_state)
                    add_result(
                        {
                            "order_id": order_id,
                            "status": "rejected",
                            **(
                                {"code": "forced_print_confirmation"}
                                if blockers
                                and all(item["kind"] == "confirm" for item in blockers)
                                else {}
                            ),
                            "message": " ".join(item["message"] for item in blockers)
                            or "Заказ не прошёл проверку.",
                            "blockers": blockers,
                        }
                    )
                    continue
                if action == "print" and order.get("pdf_path"):
                    approval = [
                        item
                        for item in print_blockers(order, fix_state)
                        if item["kind"] == "approval"
                    ]
                    if approval:
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "rejected",
                                "code": approval[0]["code"],
                                "message": " ".join(item["message"] for item in approval),
                                "blockers": approval,
                            }
                        )
                        continue
                stored_query = select(OrderResult).where(
                    OrderResult.run_id == run["id"],
                    OrderResult.order_id == order_id,
                    OrderResult.customer_id == customer_id,
                )
                stored = session.scalar(
                    stored_query.order_by(desc(OrderResult.updated_at))
                )
                if stored is None and customer_id is not None:
                    legacy_candidates = session.scalars(
                        select(OrderResult).where(
                            OrderResult.run_id == run["id"],
                            OrderResult.order_id == order_id,
                        )
                    ).all()
                    if (
                        len(legacy_candidates) == 1
                        and legacy_candidates[0].customer_id is None
                    ):
                        stored = legacy_candidates[0]
                if stored is None:
                    add_result({"order_id": order_id, "status": "not_found"})
                    continue
                active_action = self._claim_action(stored.id, action)
                if active_action is not None:
                    add_result(
                        self._busy_result(order_id, action, active_action)
                    )
                    continue
                try:
                    pending_action = session.scalar(
                        select(OrderAction)
                        .where(
                            OrderAction.order_result_id == stored.id,
                            OrderAction.status == "pending",
                        )
                        .order_by(desc(OrderAction.created_at), desc(OrderAction.id))
                    )
                    if pending_action is not None:
                        add_result(
                            self._busy_result(
                                order_id, action, pending_action.action
                            )
                        )
                        continue
                    expected_status = self._terminal_status_for_action(action)
                    if stored.status == "prepared" and order.get("status") == expected_status:
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                "idempotent": True,
                            }
                        )
                        continue
                    existing_action = session.scalar(
                        select(OrderAction)
                        .where(
                            OrderAction.order_result_id == stored.id,
                            OrderAction.action == action,
                            OrderAction.status == "prepared",
                        )
                        .order_by(desc(OrderAction.created_at), desc(OrderAction.id))
                    )
                    if (
                        existing_action is not None
                        and order.get("status") == expected_status
                    ):
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                "idempotent": True,
                            }
                        )
                        continue
                    try:
                        validate_operator_transition(
                            str(order.get("status", "")),
                            expected_status,
                            confirm_failed_processing=command.confirm_failed_processing,
                        )
                    except ValueError as exc:
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "rejected",
                                "message": str(exc),
                            }
                        )
                        continue
                    previous_order = deepcopy(order)
                    return_preview_name = None
                    if (
                        action == "reject"
                        and self.rework_sender is not None
                        and order_id not in kit_local
                    ):
                        try:
                            return_preview_name = prepare_return_preview_name(
                                order_id,
                                input_path=Path(run["options"]["input_path"]),
                                preview_paths=previous_order.get("preview_paths"),
                                files=previous_order.get("files"),
                            )
                        except Exception as exc:
                            add_result(
                                {
                                    "order_id": order_id,
                                    "status": "error",
                                    "message": str(exc),
                                }
                            )
                            continue
                    action_record = OrderAction(
                        order_result_id=stored.id,
                        action=action,
                        comment=(
                            None
                            if batch_prepress_result is not None
                            else (command.comment or "").strip() or None
                        ),
                        status="pending",
                    )
                    session.add(action_record)
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        pending_action = session.scalar(
                            select(OrderAction)
                            .where(
                                OrderAction.order_result_id == stored.id,
                                OrderAction.status == "pending",
                            )
                            .order_by(
                                desc(OrderAction.created_at), desc(OrderAction.id)
                            )
                        )
                        active = (
                            pending_action.action
                            if pending_action is not None
                            else "unknown"
                        )
                        add_result(self._busy_result(order_id, action, active))
                        continue
                    try:
                        transition_order = order
                        reviewed_pitstop = None
                        reviewed_revision = None
                        reviewed_sha256 = None
                        if action == "print":
                            input_root = Path(run["options"]["input_path"]).resolve()
                            current_pdf = Path(str(order.get("pdf_path") or ""))
                            if current_pdf.is_file() and order.get("current_pdf_sha256") and hashlib.sha256(current_pdf.read_bytes()).hexdigest() != order["current_pdf_sha256"]:
                                order, reviewed_pitstop, reviewed_revision, reviewed_sha256 = self.recheck_pdf_revision(run, order, current_pdf)
                                previous_order = deepcopy(order)
                                transition_order = order
                                if order["status"] not in {"passed", "warning"}:
                                    raise FileLifecycleError("PDF изменился и не прошёл повторную проверку.")
                            accepted_callas_fix = False
                            accepted_bleed_only = False
                            reviewed_pdf = build_reviewed_layered_print_pdf(
                                order,
                                input_root,
                                pdf_merger=self.pdf_merger,
                                pdf_page_extractor=self.pdf_page_extractor,
                            )
                            if (
                                reviewed_pdf is None
                                and self.accepted_pdf_provider is not None
                                and order.get("pdf_path")
                            ):
                                # Принятое оператором исправление Callas идёт в печать
                                # тем же путём, что и PDF из просмотренных TIFF:
                                # проверка PDF, PitStop, новая ревизия.
                                accepted_bleed_only = _fix_is_bleed_only(
                                    read_callas_fix_state(order["pdf_path"])
                                )
                                reviewed_pdf = self.accepted_pdf_provider(
                                    Path(str(order["pdf_path"]))
                                )
                                accepted_callas_fix = reviewed_pdf is not None
                            if reviewed_pdf is not None:
                                order, reviewed_pitstop, reviewed_revision, reviewed_sha256 = self.recheck_pdf_revision(
                                    run, order, reviewed_pdf,
                                    callas_fix=accepted_callas_fix,
                                    bleed_only=accepted_callas_fix and accepted_bleed_only,
                                )
                                previous_order = deepcopy(order)
                                if order["status"] not in {"passed", "warning"}:
                                    raise FileLifecycleError(
                                        "Исправленный PDF содержит ошибки и не отправлен в печать."
                                    )
                                transition_order = order

                        lifecycle = self.lifecycle_factory(
                            Path(run["options"]["input_path"])
                        )
                        transition = self._invoke_lifecycle(
                            lifecycle.accept_for_print
                            if action == "print"
                            else lifecycle.return_for_rework,
                            transition_order,
                            command.conflict_strategy,
                            **(
                                {"allow_failed": True}
                                if forced_print and transition_order.get("status") == "error"
                                else {}
                            ),
                        )
                    except ReviewedLayeredPdfError as exc:
                        action_record.status = "failed"
                        session.commit()
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "review_required"
                                if exc.generated_file_ids
                                else "error",
                                "message": str(exc),
                                **(
                                    {"generated_layered_file_ids": exc.generated_file_ids}
                                    if exc.generated_file_ids
                                    else {}
                                ),
                            }
                        )
                        continue
                    except FileLifecycleError as exc:
                        action_record.status = "failed"
                        if reviewed_pitstop is not None:
                            action_record.cms_response_json = json.dumps(
                                {"pitstop": reviewed_pitstop}, ensure_ascii=False
                            )
                        session.commit()
                        conflict = None
                        if isinstance(exc, FileConflictError):
                            conflict = {
                                "source_path": str(exc.source),
                                "destination_path": str(exc.destination),
                                "suggested_name": self._suggested_conflict_name(
                                    exc.destination
                                ),
                            }
                        missing = (
                            [item for item in print_blockers(order) if item["code"] == "pdf_missing"]
                            if action == "print" and conflict is None
                            else []
                        )
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "conflict"
                                if conflict is not None
                                else "error",
                                "message": missing[0]["message"] if missing else str(exc),
                                **({"code": "pdf_missing", "blockers": missing} if missing else {}),
                                **({"conflict": conflict} if conflict else {}),
                                **(
                                    {"pitstop": reviewed_pitstop}
                                    if reviewed_pitstop is not None
                                    else {}
                                ),
                            }
                        )
                        continue
                    except Exception as exc:
                        action_record.status = "failed"
                        session.commit()
                        logger.exception(
                            "order.transition_preparation_failed run_id=%s order_id=%s",
                            run["id"],
                            order_id,
                        )
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "error",
                                "message": str(exc),
                            }
                        )
                        continue
                    try:
                        upload_paths: list[Path] = []
                        self.coordinator.apply_file_transition(
                            run["id"],
                            aggregate_id,
                            status=expected_status,
                            source_paths=transition.source_paths,
                            pdf_path=transition.pdf_path,
                            preview_paths=transition.preview_paths,
                            pitstop=reviewed_pitstop,
                            current_pdf_revision=reviewed_revision,
                            current_pdf_sha256=reviewed_sha256,
                            confirm_failed_processing=command.confirm_failed_processing,
                        )
                        prepress_result = None
                        if batch_prepress_result is not None:
                            prepress_result = batch_prepress_result
                        elif action == "print" and self.prepress_sender is not None:
                            prepress_result = self.prepress_sender(
                                order_id, command.comment
                            )
                        elif action == "reject" and order_id in kit_local:
                            # Sborka уже вернула этот заказ вместе с главным заказом комплекта.
                            prepress_result = {
                                "kit_main_id": kit_local[order_id],
                                "returned_with_kit": True,
                                "message": (
                                    f"Возвращён в составе комплекта {kit_local[order_id]}; "
                                    "запрос в Sborka не отправлялся."
                                ),
                            }
                        elif action == "reject" and self.rework_sender is not None:
                            input_path = Path(run["options"]["input_path"])
                            with selected_custom_return_preview(
                                order_id, input_path=input_path
                            ) as custom_preview:
                                if custom_preview is not None:
                                    return_preview_name = custom_preview.name
                                if self.preview_uploader is not None:
                                    upload_paths = (
                                        [custom_preview] if custom_preview is not None
                                        else self._return_preview_upload_paths(
                                            input_path, transition, return_preview_name or ""
                                        )
                                    )
                                    if upload_paths:
                                        self.preview_uploader(upload_paths)
                            prepress_result = self.rework_sender(
                                kit_send_as.get(order_id, order_id),
                                (command.comment or "").strip(),
                                return_preview_name or "",
                                command.design,
                                command.design_cost,
                            )
                        if order_id in kit_send_as and isinstance(prepress_result, dict):
                            prepress_result = {
                                **prepress_result,
                                "kit_main_id": kit_send_as[order_id],
                                "sent_as_kit_main": True,
                            }
                        action_record.status = "prepared"
                        if prepress_result is not None:
                            action_record.cms_response_json = json.dumps(
                                prepress_result, ensure_ascii=False
                            )
                        session.commit()
                        if forced_print and previous_order.get("status") == "error":
                            self._record_forced_print(run["id"], previous_order, aggregate_id)
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "prepared",
                                **({"prepress": prepress_result} if prepress_result else {}),
                            }
                        )
                    except Exception as exc:
                        session.rollback()
                        try:
                            lifecycle.rollback(transition)
                            self.coordinator.restore_order_snapshot(
                                run["id"], aggregate_id, previous_order
                            )
                        except Exception:
                            logger.exception(
                                "order.transition_compensation_failed run_id=%s "
                                "order_id=%s",
                                run["id"],
                                order_id,
                            )
                        action_record = session.get(OrderAction, action_record.id)
                        if action_record is not None:
                            action_record.status = "failed"
                            session.commit()
                        logger.exception(
                            "order.transition_persistence_failed run_id=%s order_id=%s",
                            run["id"],
                            order_id,
                        )
                        add_result(
                            {
                                "order_id": order_id,
                                "status": "error",
                                "message": str(exc),
                            }
                        )
                finally:
                    self._release_action(stored.id)
        return {"items": results}

    def _record_forced_print(
        self, run_id: str, order: dict[str, Any], aggregate_id: str
    ) -> None:
        """Журнал и лог: красный заказ отправлен в печать по решению оператора."""
        logger.warning(
            "order.forced_print run_id=%s order_id=%s revision=%s sha256=%s",
            run_id,
            order.get("order_id"),
            order.get("current_pdf_revision"),
            order.get("current_pdf_sha256"),
        )
        repository = getattr(self.coordinator, "repository", None)
        if repository is None:
            return
        try:
            repository.append_event(
                run_id,
                "order.forced_print",
                {
                    "aggregate_id": aggregate_id,
                    "order_id": order.get("order_id"),
                    "customer_id": order.get("customer_id"),
                    "status": "error",
                    "forced": True,
                    "errors": list(order.get("errors") or [])[:20],
                    "pdf_revision": order.get("current_pdf_revision"),
                    "pdf_sha256": order.get("current_pdf_sha256"),
                },
            )
        except Exception:
            logger.exception(
                "order.forced_print_record_failed run_id=%s order_id=%s",
                run_id,
                order.get("order_id"),
            )

    @staticmethod
    def _batch_print_eligible(
        order: dict[str, Any], command: OrderActionCommand
    ) -> bool:
        """Mirror the per-order checks that must pass before an external send."""
        status = str(order.get("status") or "")
        if status in {"accepted_for_print", "returned_for_rework"}:
            return False
        fix_state = (
            read_callas_fix_state(order["pdf_path"]) if order.get("pdf_path") else None
        )
        if not (
            (status in {"passed", "warning"} and pitstop_ready(order))
            or (
                command.confirm_failed_processing
                and status == "error"
                and is_forced_print(order, fix_state)
            )
        ):
            return False
        try:
            validate_operator_transition(
                status,
                OrderWorkflowService._terminal_status_for_action("print"),
                confirm_failed_processing=command.confirm_failed_processing,
            )
        except ValueError:
            return False
        return True

    @staticmethod
    def _suggested_conflict_name(destination: Path) -> str:
        stem = destination.stem
        suffix = destination.suffix
        if suffix:
            return f"{stem} (новый){suffix}"
        return f"{stem} (новый)"


def _fix_is_bleed_only(state: dict[str, Any] | None) -> bool:
    """Исправление Callas только дорисовало вылеты (``summary["bleed_only"]``)."""
    summary = (state or {}).get("summary")
    return isinstance(summary, dict) and summary.get("bleed_only") is True
