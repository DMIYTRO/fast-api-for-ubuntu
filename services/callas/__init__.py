"""Клиент удалённой службы callas (merge PDF и render страниц в PNG)."""

from services.callas.client import CallasClient, CallasConfig, deterministic_job_id
from services.callas.merger import PdfMerger, make_callas_pdf_merger
from services.callas.errors import (
    CallasAuthError,
    CallasCancelledError,
    CallasConfigError,
    CallasConflictError,
    CallasError,
    CallasJobFailedError,
    CallasNotFoundError,
    CallasProtocolError,
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
    "CallasProtocolError",
    "CallasResultError",
    "CallasTimeoutError",
    "CallasUnavailableError",
    "CallasValidationError",
    "deterministic_job_id",
]
