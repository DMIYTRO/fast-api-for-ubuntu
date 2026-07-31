"""A conservative, per-order facade over the existing BatchProcessor."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Callable

from config.profiles import get_profile
from processing.batch_processor import BatchProcessor
from processing.models import FileCheck, OrderCheck


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
    ) -> None:
        self.options = options
        self.input_dir = Path(options.input_path)
        self.pdf_dir = self.input_dir / "PDF"
        self.preview_dir = self.input_dir / "Previews"
        self.troubles_dir = self.input_dir / "Troubles"
        self.processor = processor_factory(
            self.input_dir,
            self.pdf_dir,
            profile=get_profile(options.direction),
        )

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
        except Exception as exc:
            # ImageMagick/Ghostscript/programming failures terminate this order,
            # never the worker or the web process.
            logger.exception(
                "order.processing_failed order_id=%s", order.order_id
            )
            result.errors.append(f"{type(exc).__name__}: {exc}")
        return result
