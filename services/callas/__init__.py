"""Клиент удалённой службы callas (merge PDF и render страниц в PNG)."""

from services.callas.client import (
    CallasClient,
    CallasConfig,
    ProcessResult,
    deterministic_job_id,
)
from services.callas.merger import PdfMerger, make_callas_pdf_merger
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
    "deterministic_job_id",
    "parse_report_bytes",
    "parse_report_xml",
]
