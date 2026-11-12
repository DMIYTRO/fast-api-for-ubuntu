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

from config.profiles import get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck
from services.pitstop import (
    PitStopCheckResult,
    PitStopExecutionStatus,
    PitStopService,
)
from services.pdf_routing import FAILED_FOLDER, completed_status, route_pdf
from services.postpress import normalize_postpress


logger = logging.getLogger("image_magic.processing")


@dataclass(frozen=True)
class ProcessingOptions:
    input_path: str
    direction: str
    approve_corrections: bool = False
    correction_policy: str = "ask"
    create_pdfs: bool = True
    generate_previews: bool = True
    copy_failures: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "direction": self.direction,
            "approve_corrections": self.approve_corrections,
            "correction_policy": self.correction_policy,
            "create_pdfs": self.create_pdfs,
            "generate_previews": self.generate_previews,
            "copy_failures": self.copy_failures,
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

    def __post_init__(self) -> None:
        self.preview_paths = list(self.preview_paths or [])
        self.errors = list(self.errors or [])


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
    ) -> None:
        self.options = options
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
        self._order_info_failed = False
        self._order_info_lock = threading.Lock()
        self.processor = processor_factory(
            self.input_dir,
            self.pdf_dir,
            profile=get_profile(options.direction),
        )
        # Sborka is queried once, in the background, as soon as the processor
        # knows all order ids (before the slow artwork inspection).
        self.processor.on_orders_grouped = self._start_order_info_fetch

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
        self._order_info_failed = False
        for order in self.processor.iter_inspect_orders():
            if self._order_info_future is None:
                # Processor did not report the order ids up front.
                self._start_order_info_fetch([order.order_id])
            self._apply_postpress(order, wait=False)
            yield order

    def _start_order_info_fetch(self, order_ids: list[str]) -> None:
        if self.order_info_fetcher is None or self._order_info_future is not None:
            return
        future: Future[list[dict[str, Any]]] = Future()
        self._order_info_future = future
        self._order_info_deadline = time.monotonic() + self.order_info_timeout_seconds
        fetcher = self.order_info_fetcher

        def run() -> None:
            try:
                future.set_result(fetcher(list(order_ids)))
            except BaseException as exc:
                future.set_exception(exc)

        threading.Thread(target=run, name="sborka-orderinfo", daemon=True).start()

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
            with self._order_info_lock:
                if self._order_info_by_id is None:
                    self._order_info_by_id = {
                        str(item["id"]): item
                        for item in self._order_info_records(records)
                        if item.get("id") is not None
                    }
                record = self._order_info_by_id.get(order.order_id)
            if record is not None:
                order.postpress = normalize_postpress(record.get("post_text"))
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

    def process_order(self, order: OrderCheck) -> OrderArtifacts:
        result = self._process_order_unrouted(order)
        self._route_production_pdf(order, result)
        return result

    def _route_production_pdf(self, order: OrderCheck, result: OrderArtifacts) -> None:
        """Move the built PDF into PDF/Print|Rework|Failed by its final status."""
        if result.pdf_path is None:
            if not order.passed or result.errors:
                self._quarantine_previous_print(order)
            return
        status = completed_status(
            local_errors=bool(result.errors) or (not order.passed and result.pdf_kind != "diagnostic"),
            local_warnings=bool(
                not order.passed or order.warnings or any(item.warnings for item in order.files)
            ),
            pitstop=result.pitstop,
        )
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
        source = self.pdf_dir / "Print" / f"{ordered[0].path.stem}.pdf"
        if source.is_file():
            failed = self.pdf_dir / FAILED_FOLDER / source.name
            failed.parent.mkdir(parents=True, exist_ok=True)
            source.replace(failed)

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
        result.current_pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if self.pitstop_service is None:
            result.pitstop_skipped_reason = "PitStop не запускался: проверка PitStop не настроена."
            return
        try:
            pitstop_result = self.pitstop_service.check_pdf(
                pdf_path, profile_id=self.options.direction
            )
        except Exception as exc:
            logger.exception("order.diagnostic_pitstop_failed order_id=%s", order.order_id)
            result.pitstop_skipped_reason = f"PitStop не завершил проверку: {type(exc).__name__}: {exc}"
            result.pitstop = {"execution_status": "failed", "verdict": "error", "technical_error": str(exc)}
            return
        result.pitstop = _pitstop_result_to_dto(pitstop_result, checked_revision=1)
        result.current_pdf_revision = 1
        result.current_pdf_sha256 = pitstop_result.input_sha256

    def _process_order_unrouted(self, order: OrderCheck) -> OrderArtifacts:
        # Fold overlays and previews depend on postpress; wait for Sborka here
        # (bounded) rather than when the order is first shown.
        self._apply_postpress(order, wait=True)
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
            result.current_pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()

            if self.pitstop_service is not None:
                pitstop_result = self.pitstop_service.check_pdf(
                    pdf_path, profile_id=self.options.direction
                )
                result.pitstop = _pitstop_result_to_dto(
                    pitstop_result, checked_revision=1
                )
                result.current_pdf_revision = 1
                result.current_pdf_sha256 = pitstop_result.input_sha256

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
                "occurrences": max(1, len(locations)),
                "locations": locations,
            }
        )
    dto: dict[str, object] = {
        "check_id": result.job_id,
        "execution_status": result.status.value,
        "verdict": verdict,
        "checked_at": result.checked_at.isoformat(),
        "checked_revision": checked_revision,
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
