"""Public API for report-only PitStop Server integration."""

from .catalog import PitStopProfile, PitStopProfileCatalog
from .models import (
    PitStopCheckResult,
    PitStopCounts,
    PitStopExecutionStatus,
    PitStopIssue,
    PitStopLocation,
    PitStopReport,
    PitStopSeverity,
    PitStopVerdict,
)
from .parser import PitStopReportError, parse_pitstop_payload, parse_pitstop_report
from .paths import SharedPathError, mac_shared_path_to_windows
from .service import PitStopService, PitStopServiceSettings
from .transport import (
    PitStopTransport,
    PitStopTransportError,
    SSHSettings,
    SSHTransport,
    TransportResult,
)

__all__ = [
    "PitStopCheckResult",
    "PitStopCounts",
    "PitStopExecutionStatus",
    "PitStopIssue",
    "PitStopLocation",
    "PitStopProfile",
    "PitStopProfileCatalog",
    "PitStopReport",
    "PitStopReportError",
    "PitStopService",
    "PitStopServiceSettings",
    "PitStopSeverity",
    "PitStopTransport",
    "PitStopTransportError",
    "PitStopVerdict",
    "SSHSettings",
    "SSHTransport",
    "SharedPathError",
    "TransportResult",
    "mac_shared_path_to_windows",
    "parse_pitstop_payload",
    "parse_pitstop_report",
]
