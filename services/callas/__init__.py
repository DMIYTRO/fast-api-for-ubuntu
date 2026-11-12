"""Клиент удалённой службы callas (merge, render, process, info, split, inkcoverage)."""

from services.callas.client import (
    CallasClient,
    CallasConfig,
    ProcessResult,
    deterministic_job_id,
)
from services.callas.ink_coverage import (
    InkCoverageFlag,
    InkCoverageInk,
    InkCoveragePage,
    InkCoverageResult,
    InkCoverageThresholds,
    evaluate_ink_coverage,
    ink_coverage_to_dto,
    parse_ink_coverage,
)
from services.callas.merger import PdfMerger, make_callas_pdf_merger
from services.callas.splitter import PdfPageExtractor, make_callas_page_extractor
from services.callas.report import CallasFinding, parse_report_bytes, parse_report_xml
from services.callas.errors import (
    CallasAuthError,
    CallasCancelledError,
    CallasConfigError,
    CallasConflictError,
    CallasError,
    CallasJobFailedError,
    CallasNotFoundError,
    CallasProtocolError,
    CallasReportError,
    CallasResultError,
    CallasTimeoutError,
    CallasUnavailableError,
    CallasValidationError,
)

__all__ = [
    "PdfMerger",
    "make_callas_pdf_merger",
    "PdfPageExtractor",
    "make_callas_page_extractor",
    "CallasAuthError",
    "CallasCancelledError",
    "CallasClient",
    "CallasConfig",
    "CallasConfigError",
    "CallasConflictError",
    "CallasError",
    "CallasJobFailedError",
    "CallasNotFoundError",
    "CallasFinding",
    "CallasProtocolError",
    "CallasReportError",
    "CallasResultError",
    "CallasTimeoutError",
    "CallasUnavailableError",
    "CallasValidationError",
    "ProcessResult",
    "InkCoverageFlag",
    "InkCoverageInk",
    "InkCoveragePage",
    "InkCoverageResult",
    "InkCoverageThresholds",
    "evaluate_ink_coverage",
    "ink_coverage_to_dto",
    "parse_ink_coverage",
    "deterministic_job_id",
    "parse_report_bytes",
    "parse_report_xml",
]
