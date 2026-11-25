"""A conservative, per-order facade over the existing BatchProcessor."""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import Future
import hashlib
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Sequence

from config.check_policies import POLICY_FULL
from config.profiles import get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck, has_page_rule_violation
from services.pitstop import (
    PitStopCheckResult,
    PitStopExecutionStatus,
    PitStopService,
)
from services.pitstop.baseline import (
    FixBaselinePolicy,
    apply_fix_baseline,
    merge_source_checks,
)
from services.check_policy import PolicySettings, apply_check_policy, resolve_order_policy
from services.domain import OrderStatus
from services.callas_fix import source_signature_matches
from services.callas_source_fix import STALE_SOURCE_WARNING, mark_applied
from services.forced_print import (
    ForcedPrintBuild,
    ForcedPrintBuildError,
    assess_order_check,
    blocking_message,
)
from services.pdf_routing import CHECKED_FOLDER, FAILED_FOLDER, PRINT_FOLDER, completed_status, route_pdf
from services.order_kits import kits_from_order_info
from services.customer_preview import (
    CustomerPreviewFetcher,
    derive_filename,
    has_postpress,
    valid_filename,
)
from services.postpress import normalize_customer_comment, normalize_postpress
from services.sborka_cancel import cancelled_orders, move_cancelled_order


logger = logging.getLogger("image_magic.processing")



#: Пауза перед повторным запросом orderinfo после сбоя.
ORDER_INFO_RETRY_PAUSE_SECONDS = 2.0

@dataclass(frozen=True)
class ProcessingOptions:
    input_path: str
    direction: str
    approve_corrections: bool = False
    correction_policy: str = "ask"
    create_pdfs: bool = True
    generate_previews: bool = True
    copy_failures: bool = True
    # "auto" — политика по папке файла (config/check_policies.py); "full" — полная
    # проверка всего запуска.
    check_policy: str = "auto"

    def as_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "direction": self.direction,
            "approve_corrections": self.approve_corrections,
            "correction_policy": self.correction_policy,
            "create_pdfs": self.create_pdfs,
            "generate_previews": self.generate_previews,
            "copy_failures": self.copy_failures,
            "check_policy": self.check_policy,
        }


@dataclass
class OrderArtifacts:
    pdf_path: Path | None = None
    preview_paths: list[Path] | None = None
    errors: list[str] | None = None
    pitstop: dict[str, object] | None = None
    # DTO ``ink_coverage`` (services.callas.ink_coverage.to_dto) или None,
    # если проверка красочности не подключена.
    ink_coverage: dict[str, object] | None = None
    current_pdf_revision: int | None = None
    current_pdf_sha256: str | None = None
    # "production" (routed into PDF/Print|Rework) or "diagnostic" (built "as is"
    # for a first-stage failure, lives in PDF/Failed, never printed).
    pdf_kind: str | None = None
    # Operator-visible reason when PitStop could not run (Russian).
    pitstop_skipped_reason: str | None = None
    # Проверка исходных PDF клиента, если сторона собрана из принятого исправления
    # Callas исходника (services/pitstop/baseline.py); хранится как order["pitstop_baseline"].
    pitstop_baseline: dict[str, object] | None = None

    def __post_init__(self) -> None:
        self.preview_paths = list(self.preview_paths or [])
        self.errors = list(self.errors or [])


def completed_order_status(order: OrderCheck, artifacts: OrderArtifacts) -> str:
    """Единый итог для карточки заказа и папки собранного PDF."""
    if order.sborka_cancelled is not None:
        return OrderStatus.CANCELLED.value
    return completed_status(
        local_errors=(
            bool(artifacts.errors)
            or (not order.passed and artifacts.pdf_kind != "diagnostic")
            or has_page_rule_violation(order)
            or any(item.pdf_size_mismatch for item in order.files)
        ),
        local_warnings=bool(
            not order.passed or order.warnings or any(item.warnings for item in order.files)
        ),
        pitstop=artifacts.pitstop,
        prepress=order.prepress,
    )


class BatchProcessorAdapter:
    """Incremental facade that isolates output failures per order."""

    def __init__(
        self,
        options: ProcessingOptions,
        *,
        processor_factory: Callable[..., BatchProcessor] = BatchProcessor,
        pitstop_service: PitStopService | None = None,
        order_info_fetcher: Callable[[Sequence[str]], list[dict[str, Any]]] | None = None,
        order_info_timeout_seconds: float = 30.0,
        ink_coverage_checker: Callable[[Path, str], dict[str, Any]] | None = None,
        correction_limit_mm: float | None = None,
        correction_confirm_max_mm: float | None = None,
        max_dpi: float | None = None,
        downsample_high_dpi: bool | None = None,
        no_scale_name_markers: Sequence[str] | None = None,
        pitstop_fix_baseline: FixBaselinePolicy | None = None,
        auto_bleed: Callable[[FileCheck, BatchProcessor], FileCheck | None] | None = None,
        input_check_policy: str = POLICY_FULL,
        strict_source_dirs: Sequence[str] = (),
        customer_preview_fetcher: CustomerPreviewFetcher | None = None,
    ) -> None:
        self.options = options
        # Политика проверки заказов (services/check_policy.py). По умолчанию — полная,
        # как раньше (CLI, тесты); control_panel передаёт значения из настроек.
        self.policy_settings = PolicySettings(
            input_check_policy=input_check_policy,
            strict_source_dirs=tuple(strict_source_dirs),
        )
        # None — сравнение с проверкой исходников не подключено (тесты, CLI).
        self.pitstop_fix_baseline = pitstop_fix_baseline
        self.input_dir = Path(options.input_path)
        self.pdf_dir = self.input_dir / "PDF"
        self.preview_dir = self.input_dir / "Previews"
        self.troubles_dir = self.input_dir / "Troubles"
        self.pitstop_service = pitstop_service
        # (pdf_path, direction) -> DTO ``ink_coverage``; подключается снаружи
        # (control_panel) только при включённом callas inkcoverage.
        self.ink_coverage_checker = ink_coverage_checker
        self.order_info_fetcher = order_info_fetcher
        self.order_info_timeout_seconds = order_info_timeout_seconds
        self._order_info_future: Future[list[dict[str, Any]]] | None = None
        self._order_info_deadline = 0.0
        self._order_info_by_id: dict[str, dict[str, Any]] | None = None
        self._kits_by_id: dict[str, dict[str, Any]] = {}
        self._order_info_failed = False
        self._order_info_lock = threading.Lock()
        # Вызывается (из потока Sborka), когда ответ orderinfo получен: проверка
        # дополняет уже показанные карточки комментарием заказчика.
        self._order_info_listener: Callable[[], None] | None = None
        # Схема постпечатки от заказчика (services/customer_preview.py): скачивается в
        # фоне для заказов с постпечаткой; None — функция выключена.
        self.customer_preview_fetcher = customer_preview_fetcher
        self._customer_preview_lock = threading.Lock()
        # order_id -> событие «запрос завершён» (None — запрашивать не нужно).
        self._customer_preview_jobs: dict[str, threading.Event | None] = {}
        self._customer_previews: dict[str, dict[str, Any]] = {}
        self._customer_preview_listener: Callable[[], None] | None = None
        profile = get_profile(options.direction)
        if correction_limit_mm is not None or correction_confirm_max_mm is not None:
            profile = profile.with_correction_limit(
                profile.auto_crop_mm if correction_limit_mm is None else correction_limit_mm,
                correction_confirm_max_mm,
            )
        if max_dpi is not None or downsample_high_dpi is not None:
            profile = profile.with_downsample(max_dpi, downsample_high_dpi)
        if no_scale_name_markers is not None:
            # Метки «без масштабирования» из настроек (IMAGE_MAGIC_NO_SCALE_NAME_MARKERS).
            profile = profile.with_no_scale_markers(tuple(no_scale_name_markers))
        # «Запрашивать решение» и «Не применять» не должны молча менять размер:
        # коррекция уходит на решение оператора. «Автоматически» применяет её
        # сама в пределах профиля (IMAGE_MAGIC_AUTO_CORRECT_MAX_MM).
        correction_policy = (
            "auto" if options.approve_corrections else options.correction_policy
        )
        processor_options: dict[str, Any] = {}
        if auto_bleed is not None and correction_policy == "auto":
            # Поля вылета Callas (services/callas_auto_bleed.py) — только в режиме
            # «Автоматически»; в остальных режимах «Размер» запускает оператор.
            processor_options["auto_bleed"] = auto_bleed
        if strict_source_dirs:
            processor_options["strict_source_dirs"] = tuple(strict_source_dirs)
        self.processor = processor_factory(
            self.input_dir,
            self.pdf_dir,
            profile=profile,
            correction_policy=correction_policy,
            **processor_options,
        )
        # Sborka is queried once, in the background, as soon as the processor
        # knows all order ids (before the slow artwork inspection).
        self.processor.on_orders_grouped = self._start_order_info_fetch
        # Отменённые в Sborka заказы отбрасываются до осмотра (services/sborka_cancel.py).
        self.processor.cancelled_orders = self._cancelled_orders

    # Автоматический режим применяет малые коррекции ещё при проверке, поэтому
    # ожидающие подтверждения коррекции (выше предела) не одобряются сами.
    auto_approves_pending = False

    @property
    def pitstop_enabled(self) -> bool:
        return self.pitstop_service is not None

    def scan_and_inspect(self) -> list[OrderCheck]:
        orders = list(self.iter_inspect_orders())
        for order in orders:
            self._apply_postpress(order, wait=True)
        return orders

    def iter_inspect_orders(self):
        # Orders are published right after inspection. Sborka metadata is
        # applied if it has already arrived; otherwise process_order waits
        # for it (bounded by order_info_timeout_seconds) before using it.
        self._order_info_future = None
        self._order_info_by_id = None
        self._kits_by_id = {}
        self._order_info_failed = False
        for order in self.processor.iter_inspect_orders():
            if self._order_info_future is None:
                # Processor did not report the order ids up front.
                self._start_order_info_fetch([order.order_id])
            self._assign_check_policy(order)
            self._apply_postpress(order, wait=False)
            yield order

    def _assign_check_policy(self, order: OrderCheck) -> dict[str, str]:
        """Политика проверки заказа — по папкам его файлов и опции запуска (один раз)."""
        if order.check_policy is None:
            order.check_policy = resolve_order_policy(
                [item.source_folder for item in order.files],
                self.options.check_policy,
                self.policy_settings,
            )
        return order.check_policy

    def _finalize_pitstop(self, order: OrderCheck, result: OrderArtifacts) -> None:
        """Вердикт PitStop заказа: сравнение с исходником, затем политика проверки.

        До итогового статуса, раскладки PDF и превью — в КАЖДОЙ ветке с проверкой PitStop.
        """
        self._apply_source_fix_baseline(order, result)
        if result.pitstop:
            policy = self._assign_check_policy(order)
            result.pitstop = apply_check_policy(result.pitstop, policy["key"])

    def set_order_info_listener(self, listener: Callable[[], None] | None) -> None:
        """Register a callback for the moment the Sborka orderinfo answer arrives."""
        self._order_info_listener = listener

    def customer_comment_for(self, order: OrderCheck) -> str | None:
        """Customer comment of *order* if the orderinfo answer is already here.

        Never waits and never starts a request: opening a card must not call Sborka.
        Works even after the bounded wait of ``process_order`` gave up on a slow
        answer, as long as the answer eventually arrived successfully.
        """
        future = self._order_info_future
        if future is None or not future.done() or future.exception() is not None:
            return None
        record = self._order_info_index(future.result()).get(order.order_id)
        if record is None:
            return None
        return normalize_customer_comment(record.get("comment_client"))

    def set_customer_preview_listener(self, listener: Callable[[], None] | None) -> None:
        """Register a callback for the moment a customer postpress scheme was saved."""
        self._customer_preview_listener = listener

    def customer_preview_for(self, order: OrderCheck) -> dict[str, Any] | None:
        """Saved customer scheme of *order*, if already fetched (never waits).

        Once the orderinfo answer is here, also starts the background fetch for an
        eligible order that has none yet, so a waiting order is not left out.
        """
        future = self._order_info_future
        if (
            self.customer_preview_fetcher is not None
            and future is not None
            and future.done()
            and future.exception() is None
        ):
            record = self._order_info_index(future.result()).get(order.order_id)
            if record is not None:
                self._ensure_customer_preview(order, record)
        with self._customer_preview_lock:
            preview = self._customer_previews.get(order.order_id)
        return dict(preview) if preview else None

    def wait_customer_previews(self, timeout: float) -> None:
        """Wait (bounded) for running scheme fetches, e.g. before a run is completed."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._customer_preview_lock:
            pending = [event for event in self._customer_preview_jobs.values() if event is not None]
        for event in pending:
            if not event.wait(max(0.0, deadline - time.monotonic())):
                return

    def _ensure_customer_preview(self, order: OrderCheck, record: dict[str, Any]) -> None:
        """Start the background fetch of the customer scheme once per order."""
        fetcher = self.customer_preview_fetcher
        if fetcher is None:
            return
        with self._customer_preview_lock:
            if order.order_id in self._customer_preview_jobs:
                return
            filename = None
            if has_postpress(normalize_postpress(record.get("post_text"))):
                filename = (
                    valid_filename(record.get("filename"))
                    or order.sborka_filename
                    or derive_filename(item.path for item in order.files)
                )
            if filename is None:
                self._customer_preview_jobs[order.order_id] = None
                return
            done = threading.Event()
            self._customer_preview_jobs[order.order_id] = done

        def finished(result: dict[str, Any] | None) -> None:
            try:
                if result:
                    with self._customer_preview_lock:
                        self._customer_previews[order.order_id] = result
                    listener = self._customer_preview_listener
                    if listener is not None:
                        listener()
            finally:
                done.set()

        fetcher.submit(order.order_id, filename, self.input_dir, finished)

    def _order_info_index(self, records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        with self._order_info_lock:
            if self._order_info_by_id is None:
                self._order_info_by_id = {
                    str(item["id"]): item
                    for item in self._order_info_records(records)
                    if item.get("id") is not None
                }
                self._kits_by_id = kits_from_order_info(records)
            return self._order_info_by_id

    def _start_order_info_fetch(self, order_ids: list[str]) -> None:
        if self.order_info_fetcher is None or self._order_info_future is not None:
            return
        future: Future[list[dict[str, Any]]] = Future()
        self._order_info_future = future
        # Одна повторная попытка после паузы: ожидание рассчитано на обе.
        retry_pause = min(ORDER_INFO_RETRY_PAUSE_SECONDS, self.order_info_timeout_seconds)
        self._order_info_deadline = (
            time.monotonic() + 2 * self.order_info_timeout_seconds + retry_pause
        )
        fetcher = self.order_info_fetcher

        def run() -> None:
            try:
                try:
                    records = fetcher(list(order_ids))
                except Exception as exc:  # noqa: BLE001 — повтор, затем без данных Sborka
                    logger.warning("sborka.orderinfo_retry error=%s", exc or type(exc).__name__)
                    time.sleep(retry_pause)
                    records = fetcher(list(order_ids))
                future.set_result(records)
            except BaseException as exc:
                future.set_exception(exc)
                return
            listener = self._order_info_listener
            if listener is not None and self._order_info_future is future:
                try:
                    listener()
                except Exception:  # noqa: BLE001 — never affects the check
                    logger.exception("sborka.orderinfo_listener_failed")

        threading.Thread(target=run, name="sborka-orderinfo", daemon=True).start()

    def _cancelled_orders(self, order_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Заказы, отменённые в Sborka; ждёт ответ orderinfo не дольше общего срока.

        Без ответа (Sborka не настроена, сбой после повтора, срок вышел) — пусто:
        заказы проверяются как обычно.
        """
        future = self._order_info_future
        if future is None or self._order_info_failed:
            return {}
        try:
            records = future.result(
                timeout=max(0.0, self._order_info_deadline - time.monotonic())
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "sborka.cancelled_check_skipped error=%s", exc or type(exc).__name__
            )
            return {}
        found = cancelled_orders(self._order_info_index(records), order_ids)
        if found:
            logger.info("sborka.cancelled_orders ids=%s", ",".join(sorted(found)))
        return found

    def _apply_postpress(self, order: OrderCheck, *, wait: bool) -> None:
        """Apply Sborka metadata to *order*; failures are logged, never raised."""
        future = self._order_info_future
        if future is None or self._order_info_failed:
            return
        if not wait and not future.done():
            return
        try:
            records = future.result(
                timeout=max(0.0, self._order_info_deadline - time.monotonic())
            )
            record = self._order_info_index(records).get(order.order_id)
            with self._order_info_lock:
                kit = self._kits_by_id.get(order.order_id)
            if record is not None:
                order.postpress = normalize_postpress(record.get("post_text"))
                comment = normalize_customer_comment(record.get("comment_client"))
                if comment:
                    order.customer_comment = comment
                filename = valid_filename(record.get("filename"))
                if filename:
                    order.sborka_filename = filename
                self._ensure_customer_preview(order, record)
            if kit is not None:
                order.kit = dict(kit)
        except Exception as exc:
            # Metadata failure must never block artwork validation.
            with self._order_info_lock:
                first_failure = not self._order_info_failed
                self._order_info_failed = True
            if first_failure:
                logger.warning(
                    "sborka.orderinfo_failed error=%s", exc or type(exc).__name__
                )

    @staticmethod
    def _order_info_records(records: Sequence[dict[str, Any]]):
        """Flatten Sborka order sets so each scanned suborder keeps its metadata."""
        for record in records:
            yield record
            sets = record.get("sets_array")
            if isinstance(sets, list):
                yield from BatchProcessorAdapter._order_info_records(
                    [item for item in sets if isinstance(item, dict)]
                )

    @staticmethod
    def _fold_overlay(order: OrderCheck) -> dict[str, object] | None:
        """Convert normalized Sborka metadata into renderer-only settings."""
        fold = (order.postpress or {}).get("fold")
        if not isinstance(fold, dict) or fold.get("needs_confirmation"):
            return None
        fold_type = fold.get("type")
        if fold_type not in {"half-fold", "c-fold", "z-fold"}:
            return None
        count = 1 if fold_type == "half-fold" else fold.get("count")
        if not isinstance(count, int) or count < 1:
            return None
        return {"type": fold_type, "count": count, "confirmed": True}

    @classmethod
    def _fold_overlays_for_files(cls, order: OrderCheck) -> dict[Path, dict[str, object]]:
        overlay = cls._fold_overlay(order)
        return {item.path: overlay for item in order.files} if overlay else {}

    @classmethod
    def _fold_overlay_for_pdf(cls, order: OrderCheck) -> dict[str, object] | None:
        overlay = cls._fold_overlay(order)
        if overlay is None:
            return None
        ordered = sorted(
            order.files,
            key=lambda item: 0 if item.parsed and item.parsed.side == "face" else 1,
        )
        if ordered and ordered[0].parsed:
            overlay["span_mm"] = ordered[0].parsed.width_mm
        overlay["page_sides"] = {
            index: (item.parsed.side if item.parsed else "face")
            for index, item in enumerate(ordered, start=1)
        }
        return overlay

    @property
    def total_orders(self) -> int:
        return self.processor.scanned_order_count

    @staticmethod
    def pending_files(order: OrderCheck) -> list[FileCheck]:
        return [
            item
            for item in order.files
            if item.resample_decision == "ask_confirmation"
        ]

    def decide(self, order: OrderCheck, approved: bool) -> None:
        for item in self.pending_files(order):
            self.processor.confirm_resample(item, approved)

    def reinspect_file(self, item: FileCheck, content_path: Path | None) -> FileCheck:
        """Проверить PDF-файл заказа заново по ``content_path`` (``None`` — по исходнику)."""
        return self.processor.reinspect_pdf_file(item, content_path)

    def revalidate_order(self, order: OrderCheck) -> None:
        self.processor.revalidate_order(order)

    def _drop_stale_source_fixes(self, order: OrderCheck) -> None:
        """Не собирать из исправления Callas, если исходник изменился после него.

        Такой файл проверяется заново по исходнику со всеми обычными правилами
        (обычно ему снова нужно решение по коррекции, и заказ не собирается).
        """
        changed = False
        for index, item in enumerate(order.files):
            fix = item.callas_source_fix
            if fix is None or item.content_path is None:
                continue
            if item.content_path.is_file() and source_signature_matches(
                item.path, fix.get("source")  # type: ignore[arg-type]
            ):
                continue
            logger.warning(
                "order.callas_source_fix_stale order_id=%s file=%s",
                order.order_id,
                item.path.name,
            )
            replacement = self.processor.reinspect_pdf_file(item, None)
            replacement.warnings.insert(0, STALE_SOURCE_WARNING)
            order.files[index] = replacement
            changed = True
        if changed:
            self.processor.revalidate_order(order)

    def build_forced_print_pdf(
        self, order_dto: dict[str, Any], output_dir: Path
    ) -> ForcedPrintBuild:
        """Итоговый PDF красного заказа с одним лишь низким разрешением — при печати.

        Файлы заказа осматриваются заново (принятые исправления Callas исходников —
        как в проверке, по ``callas_source_fix``), разбор ошибок повторяется по свежему
        осмотру, затем PDF собирается производственным путём ``BatchProcessor``
        (``create_forced_print_pdf``) в ``output_dir``. PitStop здесь не запускается:
        его выполняет регистрация новой ревизии (``OrderWorkflowService``).
        """
        files = [item for item in order_dto.get("files") or [] if isinstance(item, dict)]
        paths = [Path(str(item.get("path") or "")) for item in files]
        missing = [path.name or "?" for path in paths if not path.is_file()]
        if not files or missing:
            raise ForcedPrintBuildError(
                "Итоговый PDF не собран: не найден исходный файл заказа "
                f"({', '.join(missing) or 'нет файлов'}). Запустите проверку заново."
            )
        try:
            order = self.processor.inspect_order_files(paths)
        except ValueError as exc:
            raise ForcedPrintBuildError(f"Итоговый PDF не собран: {exc}.") from exc
        changed = False
        for index, item in enumerate(files):
            fix = item.get("callas_source_fix")
            content = Path(str(fix.get("pdf_path"))) if isinstance(fix, dict) and fix.get("pdf_path") else None
            if content is None or not content.is_file():
                continue
            replacement = self.processor.reinspect_pdf_file(order.files[index], content, fix=fix)
            order.files[index] = mark_applied(replacement, fix)
            changed = True
        if changed:
            self.processor.revalidate_order(order)
        self._drop_stale_source_fixes(order)
        assessment = assess_order_check(order)
        if not assessment.eligible:
            raise ForcedPrintBuildError(blocking_message(assessment))
        try:
            pdf_path = self.processor.create_forced_print_pdf(order, Path(output_dir))
        except ForcedPrintBuildError:
            raise
        except Exception as exc:
            logger.exception("order.forced_print_build_failed order_id=%s", order.order_id)
            raise ForcedPrintBuildError(
                f"Итоговый PDF не собран: {exc}. Заказ не отправлен в печать."
            ) from exc
        warnings = [*order.warnings, *(warning for item in order.files for warning in item.warnings)]
        return ForcedPrintBuild(
            pdf_path=pdf_path,
            prepress=order.prepress,
            color_conversion=order.color_conversion,
            warnings=warnings,
            assessment=assessment,
        )

    def process_order(self, order: OrderCheck) -> OrderArtifacts:
        if order.sborka_cancelled is not None:
            # Отменён в Sborka: ни PDF, ни PitStop, ни превью — только перенос исходников.
            move_cancelled_order(order, self.input_dir)
            return OrderArtifacts()
        result = self._process_order_unrouted(order)
        self._route_production_pdf(order, result)
        return result

    def _route_production_pdf(self, order: OrderCheck, result: OrderArtifacts) -> None:
        """Move the built PDF into PDF/Print|Rework|Failed by its final status."""
        if result.pdf_path is None:
            if not order.passed or result.errors:
                self._quarantine_previous_print(order)
            return
        status = completed_order_status(order, result)
        try:
            result.pdf_path = route_pdf(result.pdf_path, status, pdf_dir=self.pdf_dir)
        except OSError as exc:
            logger.exception(
                "order.pdf_route_failed order_id=%s status=%s", order.order_id, status
            )
            result.errors.append(f"Раскладка PDF по папке статуса: {exc}")
            # The order is an error now; keep the file out of Print if we can.
            try:
                result.pdf_path = route_pdf(result.pdf_path, "error", pdf_dir=self.pdf_dir)
            except OSError:
                self._quarantine_previous_print(order)

    def _quarantine_previous_print(self, order: OrderCheck) -> None:
        """A failed new run must not leave its previous result marked ready."""
        ordered = sorted(order.files, key=lambda item: 0 if item.parsed and item.parsed.side == "face" else 1)
        if not ordered:
            return
        for folder in (CHECKED_FOLDER, PRINT_FOLDER):
            source = self.pdf_dir / folder / f"{ordered[0].path.stem}.pdf"
            if source.is_file():
                failed = self.pdf_dir / FAILED_FOLDER / source.name
                failed.parent.mkdir(parents=True, exist_ok=True)
                try:
                    source.replace(failed)
                except FileNotFoundError:
                    continue  # копию уже забрали из папки — помечать нечего

    def _build_diagnostic_pdf(self, order: OrderCheck, result: OrderArtifacts) -> None:
        """First-stage failure: build an "as is" PDF into PDF/Failed and run PitStop on it."""
        builder = getattr(self.processor, "create_diagnostic_pdf", None)
        if builder is None:
            result.pitstop_skipped_reason = (
                "PitStop не запускался: диагностический PDF для этого режима не собирается."
            )
            return
        try:
            pdf_path, note = builder(order, self.pdf_dir / FAILED_FOLDER)
        except Exception as exc:
            logger.exception("order.diagnostic_pdf_failed order_id=%s", order.order_id)
            result.pitstop_skipped_reason = (
                "PitStop не запускался: не удалось собрать диагностический PDF "
                f"({type(exc).__name__}: {exc})."
            )
            return
        if pdf_path is None:
            result.pitstop_skipped_reason = f"PitStop не запускался: {note or 'PDF не собран'}."
            return
        if note:
            result.errors.append(f"Диагностический PDF: {note}")
        result.pdf_path = pdf_path
        result.pdf_kind = "diagnostic"
        result.current_pdf_revision = 1
        # The digest of this revision is computed once: PitStop reports the
        # sha256 of the very file it read, so it is hashed here only when
        # PitStop is not going to supply it (not configured, or it failed).
        if self.ink_coverage_checker is not None:
            result.ink_coverage = self._check_ink_coverage(order, pdf_path)
        if self.pitstop_service is None:
            result.pitstop_skipped_reason = "PitStop не запускался: проверка PitStop не настроена."
            result.current_pdf_sha256 = _sha256_file(pdf_path)
            return
        try:
            pitstop_result = self.pitstop_service.check_pdf(
                pdf_path, profile_id=self.options.direction
            )
        except Exception as exc:
            result.current_pdf_sha256 = _sha256_file(pdf_path)
            logger.exception("order.diagnostic_pitstop_failed order_id=%s", order.order_id)
            result.pitstop_skipped_reason = f"PitStop не завершил проверку: {type(exc).__name__}: {exc}"
            result.pitstop = {"execution_status": "failed", "verdict": "error", "technical_error": str(exc)}
            return
        result.pitstop = _pitstop_result_to_dto(pitstop_result, checked_revision=1)
        result.current_pdf_revision = 1
        result.current_pdf_sha256 = pitstop_result.input_sha256
        # Как и в производственной ветке: до итогового статуса и раскладки PDF —
        # вердикт без находок, добавленных принятым исправлением Callas исходника.
        self._finalize_pitstop(order, result)

    def _process_order_unrouted(self, order: OrderCheck) -> OrderArtifacts:
        # Fold overlays and previews depend on postpress; wait for Sborka here
        # (bounded) rather than when the order is first shown.
        self._apply_postpress(order, wait=True)
        self._drop_stale_source_fixes(order)
        result = OrderArtifacts()
        source_previews_created = False

        def create_source_previews() -> None:
            """Create fallback previews once, only when they are needed."""
            nonlocal source_previews_created
            if source_previews_created or not self.options.generate_previews:
                return
            source_previews_created = True
            fold_overlays = self._fold_overlays_for_files(order)
            if fold_overlays:
                preview_results = self.processor.generate_previews_for_files(
                    order.files,
                    self.preview_dir,
                    fold_overlays_by_file=fold_overlays,
                )
            else:
                preview_results = self.processor.generate_previews_for_files(
                    order.files, self.preview_dir
                )
            for file_check, previews, preview_error in preview_results:
                result.preview_paths.extend(previews)
                if preview_error:
                    logger.error(
                        "file.preview_failed order_id=%s file=%s error=%s",
                        order.order_id,
                        file_check.path.name,
                        preview_error,
                    )
                    result.errors.append(
                        f"Превью {file_check.path.name}: {preview_error}"
                    )

        try:
            if not order.passed:
                create_source_previews()
                if self.options.copy_failures:
                    copies = self.processor.copy_failed_to_troubles(
                        [order], self.troubles_dir
                    )
                    result.errors.extend(
                        error for _, _, error in copies if error is not None
                    )
                if self.options.create_pdfs:
                    self._build_diagnostic_pdf(order, result)
                else:
                    result.pitstop_skipped_reason = (
                        "PitStop не запускался: создание PDF отключено настройками проверки."
                    )
                return result

            if not self.options.create_pdfs:
                create_source_previews()
                return result

            pdf_results = self.processor.create_pdfs([order])
            if not pdf_results:
                result.errors.append("BatchProcessor не вернул результат создания PDF")
                create_source_previews()
                return result
            _, pdf_path, pdf_error = pdf_results[0]
            if pdf_error:
                logger.error(
                    "order.pdf_failed order_id=%s error=%s",
                    order.order_id,
                    pdf_error,
                )
                result.errors.append(f"PDF: {pdf_error}")
                create_source_previews()
                if self.options.copy_failures:
                    copies = self.processor.copy_pdf_failure_to_troubles(
                        order, self.troubles_dir, pdf_error
                    )
                    result.errors.extend(
                        error for _, _, error in copies if error is not None
                    )
                return result
            result.pdf_path = pdf_path
            result.pdf_kind = "production"
            result.current_pdf_revision = 1

            if self.pitstop_service is not None:
                # PitStop returns the sha256 of the exact file it checked, so
                # the PDF is not read a second time here; if the check fails
                # the digest is still recorded for the revision.
                try:
                    pitstop_result = self.pitstop_service.check_pdf(
                        pdf_path, profile_id=self.options.direction
                    )
                except Exception:
                    result.current_pdf_sha256 = _sha256_file(pdf_path)
                    raise
                result.pitstop = _pitstop_result_to_dto(
                    pitstop_result, checked_revision=1
                )
                result.current_pdf_revision = 1
                result.current_pdf_sha256 = pitstop_result.input_sha256
                # До итогового статуса и превью: вердикт без находок, добавленных
                # принятым исправлением Callas исходника.
                self._finalize_pitstop(order, result)

                # The operator must review the exact production PDF checked by
                # PitStop. Replace source previews with revision-bound previews
                # only after the report-only check has finished.
                if self.options.generate_previews:
                    production_dir = (
                        self.preview_dir
                        / "Final"
                        / order.aggregate_id
                        / "r0001"
                    )
                    preview_options = {
                        "pdf_page_names_map": {
                            pdf_path: self._production_page_names(order)
                        }
                    }
                    if overlay := self._fold_overlay_for_pdf(order):
                        preview_options["fold_overlays_by_pdf"] = {pdf_path: overlay}
                    preview_results = self.processor.generate_previews_for_all(
                        [pdf_path], production_dir, **preview_options
                    )
                    if preview_results:
                        _, previews, preview_error = preview_results[0]
                        if previews:
                            result.preview_paths = list(previews)
                        if preview_error:
                            result.errors.append(
                                f"Производственное превью: {preview_error}"
                            )
                        if not previews:
                            create_source_previews()
                    else:
                        result.errors.append(
                            "BatchProcessor не вернул производственное превью"
                        )
                        create_source_previews()
            else:
                result.current_pdf_sha256 = _sha256_file(pdf_path)
                # Without PitStop there is no production PDF revision to
                # render, so source previews are the single compact variant.
                create_source_previews()

            # Красочность считается по тому же итоговому PDF после PitStop.
            # Сбой проверки никогда не роняет заказ: статус "failed" с текстом.
            if self.ink_coverage_checker is not None:
                result.ink_coverage = self._check_ink_coverage(order, pdf_path)
        except Exception as exc:
            # ImageMagick/Ghostscript/programming failures terminate this order,
            # never the worker or the web process.
            logger.exception(
                "order.processing_failed order_id=%s", order.order_id
            )
            result.errors.append(f"{type(exc).__name__}: {exc}")
            # If PDF/PitStop processing failed before revision-bound previews
            # were available, still leave the operator a visual fallback.
            try:
                create_source_previews()
            except Exception as preview_exc:
                result.errors.append(
                    f"Превью исходников: {type(preview_exc).__name__}: {preview_exc}"
                )
        return result

    def _apply_source_fix_baseline(self, order: OrderCheck, result: OrderArtifacts) -> None:
        """Сравнить PitStop PDF заказа с проверкой исходных PDF клиента.

        Вызывается в КАЖДОЙ ветке, где PitStop проверяет PDF заказа (производственный
        и диагностический PDF), до итогового статуса, раскладки и превью.

        Только если хотя бы одна сторона собрана из принятого исправления Callas
        исходника и профиль входит в ``IMAGE_MAGIC_PITSTOP_FIX_BASELINE_PROFILES``;
        остальные заказы не получают ни одного лишнего запуска PitStop.

        Объём сравнения: если каждое принятое исправление только дорисовало вылеты
        (``callas_source_fix["bleed_only"]``), сравниваются все действия PitStop —
        всё новое принесли вылеты. Если хоть одно исправление сделало что-то ещё,
        осторожно сравниваются только ``IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS``.

        Baseline — проверки исходников ВСЕХ файлов заказа (и исправленных, и нет):
        для стороны с исправлением — её PDF до исправления
        (``callas_source_fix["baseline_pdf"]``: исходный PDF или PDF, собранный из
        растрового исходника), для прочих — сам исходник, если это PDF. Сравнение идёт
        по суммам вхождений на ``action_id``, а находки стороны без исправления попадают
        в PDF заказа без изменений, поэтому они всегда есть в baseline и не могут быть
        скрыты. Скрывается только то, чего у клиента не было ни в одном исходнике.

        Сбой baseline (ошибка, таймаут, растровый файл без исправления, который PitStop
        не проверяет) ничего не вычитает (``baseline_status`` = ``failed``/``missing``)
        и не роняет заказ. PDF заказа, превью и отчёты основной проверки не меняются.
        """
        policy = self.pitstop_fix_baseline
        pitstop = result.pitstop
        direction = self.options.direction
        if policy is None or self.pitstop_service is None or not pitstop:
            return
        fixes = [
            item.callas_source_fix
            for item in order.files
            if item.callas_source_fix is not None and item.content_path is not None
        ]
        if not fixes:
            return
        bleed_only = all(
            isinstance(fix, dict) and fix.get("bleed_only") is True for fix in fixes
        )
        scope = "all" if policy.actions_for(all_actions=bleed_only) is None else "actions"
        branch = result.pdf_kind or "production"

        def log(status: Any, checks: str = "", reason: str = "", after: Any = None) -> None:
            logger.info(
                "order.pitstop_source_baseline order_id=%s branch=%s scope=%s "
                "status=%s reason=%s checks=%s verdict=%s->%s",
                order.order_id,
                branch,
                scope,
                status,
                reason or "-",
                checks,
                pitstop.get("verdict"),
                after,
            )

        if not policy.applies_to(direction, all_actions=bleed_only):
            log("skipped", reason="policy")
            return
        if pitstop.get("execution_status") != "completed":
            log("skipped", reason="main_check_failed")
            return
        sources: list[Path] = []
        missing: list[str] = []
        ordered = sorted(
            order.files,
            key=lambda item: 0 if item.parsed and item.parsed.side == "face" else 1,
        )
        for item in ordered:
            path = _baseline_source(item)
            if path is None:
                missing.append(item.path.name)
            elif path not in sources:
                sources.append(path)
        if missing:
            # Растровый исходник без PDF до исправления PitStop не проверяет:
            # сравнить не с чем, ничего не скрывается.
            adjusted = apply_fix_baseline(
                pitstop, None, policy, profile_key=direction, all_actions=bleed_only
            )
            log(
                adjusted.get("baseline_status") if adjusted else None,
                reason="raster_source:" + ",".join(missing),
                after=adjusted.get("verdict") if adjusted else None,
            )
            result.pitstop = adjusted
            return
        checks: list[tuple[str, dict[str, Any]]] = []
        for path in sources:
            try:
                check_result = self.pitstop_service.check_pdf(path, profile_id=direction)
                check = _pitstop_result_to_dto(check_result, checked_revision=1)
            except Exception as exc:  # noqa: BLE001 - baseline никогда не роняет заказ
                logger.warning(
                    "order.pitstop_source_baseline_failed order_id=%s file=%s error=%s",
                    order.order_id,
                    path.name,
                    exc,
                )
                check = {
                    "execution_status": "failed",
                    "verdict": "error",
                    "technical_error": f"Проверка PitStop исходника: {type(exc).__name__}: {exc}",
                    "issues": [],
                }
            checks.append((str(path), check))
            if check.get("execution_status") != "completed":
                break  # Неполный baseline всё равно не используется.
        baseline = merge_source_checks(checks)
        adjusted = apply_fix_baseline(
            pitstop, baseline, policy, profile_key=direction, all_actions=bleed_only
        )
        log(
            adjusted.get("baseline_status") if adjusted else None,
            checks=",".join(str(check.get("check_id")) for _, check in checks),
            after=adjusted.get("verdict") if adjusted else None,
        )
        result.pitstop = adjusted
        if baseline.get("execution_status") == "completed":
            result.pitstop_baseline = baseline

    def _check_ink_coverage(self, order: OrderCheck, pdf_path: Path) -> dict[str, Any]:
        try:
            dto = self.ink_coverage_checker(pdf_path, self.options.direction)
        except Exception as exc:  # noqa: BLE001 - сбой проверки не должен ронять заказ
            logger.warning(
                "order.ink_coverage_failed order_id=%s error=%s", order.order_id, exc
            )
            return _ink_coverage_failed_dto(f"{type(exc).__name__}: {exc}")
        if not isinstance(dto, dict):
            return _ink_coverage_failed_dto("Проверка красочности вернула неожиданный результат.")
        return dto

    @staticmethod
    def _production_page_names(order: OrderCheck) -> list[str]:
        if len(order.files) == 1 and order.files[0].page_count == 2:
            stem = order.files[0].path.stem
            return [f"{stem}_face", f"{stem}_back"]
        ordered = sorted(
            order.files,
            key=lambda item: 0 if item.parsed and item.parsed.side == "face" else 1,
        )
        return [item.path.stem for item in ordered]


def _baseline_source(item: FileCheck) -> Path | None:
    """PDF стороны до исправления Callas для baseline или ``None``, если его нет.

    У стороны с принятым исправлением — ``callas_source_fix["baseline_pdf"]``
    (исходный PDF или PDF, собранный из растрового исходника); иначе — сам исходник,
    если это PDF. Растровый исходник без такого PDF PitStop не проверяет.
    """
    fix = item.callas_source_fix
    if isinstance(fix, dict) and item.content_path is not None:
        baseline_pdf = fix.get("baseline_pdf")
        if baseline_pdf:
            path = Path(str(baseline_pdf))
            if path.is_file():
                return path
    return item.path if item.path.suffix.lower() == ".pdf" else None


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file through SHA-256 without loading it into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ink_coverage_failed_dto(error: str) -> dict[str, Any]:
    from services.callas.ink_coverage import InkCoverageResult, to_dto

    return to_dto(InkCoverageResult.failed(error))


def _pitstop_result_to_dto(
    result: PitStopCheckResult, *, checked_revision: int
) -> dict[str, object]:
    report = result.report
    verdict = report.verdict.value if report is not None else "error"
    counts = report.counts if report is not None else None
    issues = []
    for issue in report.issues if report is not None else ():
        locations = [
            {
                "page": location.page,
                "bbox": {
                    "min_x": location.min_x,
                    "min_y": location.min_y,
                    "max_x": location.max_x,
                    "max_y": location.max_y,
                },
                "units": "pt",
            }
            for location in issue.locations
        ]
        fingerprint_source = "|".join(
            [issue.severity.value, str(issue.action_id or ""), issue.message]
        )
        issues.append(
            {
                "id": hashlib.sha256(
                    fingerprint_source.encode("utf-8")
                ).hexdigest()[:32],
                "fingerprint": hashlib.sha256(
                    fingerprint_source.encode("utf-8")
                ).hexdigest(),
                "severity": issue.severity.value,
                "action_id": issue.action_id,
                "message": issue.message,
                # Отчёт содержит не более 100 локаций, точное число — в тексте сообщения.
                "occurrences": (
                    issue.total_occurrences
                    if issue.total_occurrences is not None
                    else max(1, len(locations))
                ),
                "locations": locations,
            }
        )
    dto: dict[str, object] = {
        "check_id": result.job_id,
        "execution_status": result.status.value,
        "verdict": verdict,
        "checked_at": result.checked_at.isoformat(),
        "checked_revision": checked_revision,
        # Хэш проверенного PDF: UI сверяет его с current_pdf_sha256 заказа, и без
        # этого поля готовый (зелёный) заказ нельзя отправить в печать.
        "input_sha256": result.input_sha256,
        "profile": {
            "key": result.profile_id,
            "name": report.profile_name if report is not None else None,
            "version": None,
        },
        "pages": report.pages if report is not None else None,
        "counts": {
            "errors": counts.errors if counts is not None else 0,
            "warnings": counts.warnings if counts is not None else 0,
            "fixes": counts.fixes if counts is not None else 0,
            "critical_failures": (
                counts.critical_failures if counts is not None else 0
            ),
            "noncritical_failures": (
                counts.noncritical_failures if counts is not None else 0
            ),
            "informations": counts.informations if counts is not None else 0,
        },
        "issues": issues,
        "reports": {
            "json_url": (
                str(result.report_json_path) if result.report_json_path else None
            ),
            "xml_url": (
                str(result.report_xml_path) if result.report_xml_path else None
            ),
        },
    }
    if result.status is PitStopExecutionStatus.FAILED:
        dto["technical_error"] = (
            result.technical_error or "Проверка PitStop не выполнена."
        )
    return dto
