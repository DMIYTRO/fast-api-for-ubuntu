"""A conservative, per-order facade over the existing BatchProcessor."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
from typing import Callable

from config.profiles import get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck
from services.pitstop import (
    PitStopCheckResult,
    PitStopExecutionStatus,
    PitStopService,
)


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
    current_pdf_revision: int | None = None
    current_pdf_sha256: str | None = None

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
    ) -> None:
        self.options = options
        self.input_dir = Path(options.input_path)
        self.pdf_dir = self.input_dir / "PDF"
        self.preview_dir = self.input_dir / "Previews"
        self.troubles_dir = self.input_dir / "Troubles"
        self.pitstop_service = pitstop_service
        self.processor = processor_factory(
            self.input_dir,
            self.pdf_dir,
            profile=get_profile(options.direction),
        )

    @property
    def pitstop_enabled(self) -> bool:
        return self.pitstop_service is not None

    def scan_and_inspect(self) -> list[OrderCheck]:
        return self.processor.inspect_orders()

    def iter_inspect_orders(self):
        return self.processor.iter_inspect_orders()

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
        result = OrderArtifacts()
        try:
            # Previews are review artifacts, not production artifacts.  Make
            # them for every readable source file, even when the order is
            # invalid, PDF creation is disabled, or another file fails.
            if self.options.generate_previews:
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

            if not order.passed:
                if self.options.copy_failures:
                    copies = self.processor.copy_failed_to_troubles(
                        [order], self.troubles_dir
                    )
                    result.errors.extend(
                        error for _, _, error in copies if error is not None
                    )
                return result

            if not self.options.create_pdfs:
                return result

            pdf_results = self.processor.create_pdfs([order])
            if not pdf_results:
                result.errors.append("BatchProcessor не вернул результат создания PDF")
                return result
            _, pdf_path, pdf_error = pdf_results[0]
            if pdf_error:
                logger.error(
                    "order.pdf_failed order_id=%s error=%s",
                    order.order_id,
                    pdf_error,
                )
                result.errors.append(f"PDF: {pdf_error}")
                if self.options.copy_failures:
                    copies = self.processor.copy_pdf_failure_to_troubles(
                        order, self.troubles_dir, pdf_error
                    )
                    result.errors.extend(
                        error for _, _, error in copies if error is not None
                    )
                return result
            result.pdf_path = pdf_path

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
                    preview_results = self.processor.generate_previews_for_all(
                        [pdf_path],
                        production_dir,
                        pdf_page_names_map={
                            pdf_path: self._production_page_names(order)
                        },
                    )
                    if preview_results:
                        _, previews, preview_error = preview_results[0]
                        if previews:
                            result.preview_paths = list(previews)
                        if preview_error:
                            result.errors.append(
                                f"Производственное превью: {preview_error}"
                            )
                    else:
                        result.errors.append(
                            "BatchProcessor не вернул производственное превью"
                        )
        except Exception as exc:
            # ImageMagick/Ghostscript/programming failures terminate this order,
            # never the worker or the web process.
            logger.exception(
                "order.processing_failed order_id=%s", order.order_id
            )
            result.errors.append(f"{type(exc).__name__}: {exc}")
        return result

    @staticmethod
    def _production_page_names(order: OrderCheck) -> list[str]:
        if len(order.files) == 1 and order.files[0].page_count == 2:
            stem = order.files[0].path.stem
            return [f"{stem}_page1", f"{stem}_page2"]
        ordered = sorted(
            order.files,
            key=lambda item: 0 if item.parsed and item.parsed.side == "face" else 1,
        )
        return [item.path.stem for item in ordered]


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
