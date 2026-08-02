"""Domain types shared by the PitStop integration layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class PitStopSeverity(StrEnum):
    CRITICAL_FAILURE = "critical_failure"
    ERROR = "error"
    WARNING = "warning"
    NONCRITICAL_FAILURE = "noncritical_failure"
    INFORMATION = "information"
    FIX = "fix"
    SIGNOFF = "signoff"


class PitStopExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class PitStopVerdict(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PitStopLocation:
    """A report location; page numbers are one-based in this public model."""

    page: int
    min_x: float | None = None
    min_y: float | None = None
    max_x: float | None = None
    max_y: float | None = None


@dataclass(frozen=True, slots=True)
class PitStopIssue:
    severity: PitStopSeverity
    message: str
    action_id: int | str | None = None
    locations: tuple[PitStopLocation, ...] = ()


@dataclass(frozen=True, slots=True)
class PitStopCounts:
    errors: int = 0
    warnings: int = 0
    critical_failures: int = 0
    noncritical_failures: int = 0
    fixes: int = 0
    informations: int = 0
    signoffs: int = 0

    @property
    def has_errors(self) -> bool:
        return bool(self.errors or self.critical_failures)

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings or self.noncritical_failures)


@dataclass(frozen=True, slots=True)
class PitStopReport:
    counts: PitStopCounts
    issues: tuple[PitStopIssue, ...] = ()
    profile_name: str | None = None
    pages: int | None = None
    preflighted_at: datetime | None = None

    @property
    def verdict(self) -> PitStopVerdict:
        if self.counts.has_errors:
            return PitStopVerdict.ERROR
        if self.counts.has_warnings:
            return PitStopVerdict.WARNING
        return PitStopVerdict.PASSED


@dataclass(frozen=True, slots=True)
class PitStopCheckResult:
    status: PitStopExecutionStatus
    profile_id: str
    input_pdf: Path
    checked_at: datetime
    input_sha256: str
    report: PitStopReport | None = None
    report_json_path: Path | None = None
    report_xml_path: Path | None = None
    technical_error: str | None = None
    job_id: str | None = None

    @property
    def passed(self) -> bool:
        return bool(
            self.status is PitStopExecutionStatus.COMPLETED
            and self.report is not None
            and not self.report.counts.has_errors
        )
