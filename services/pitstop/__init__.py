"""Public API for report-only PitStop Server integration."""

from .catalog import PitStopProfile, PitStopProfileCatalog
from .concurrency import GateConfig, PitStopGate, shared_gate_for
from .failures import FailureKind
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
from .parser import (
    PitStopReportError,
    PitStopReportUnreadableError,
    parse_occurrences,
    parse_pitstop_payload,
    parse_pitstop_report,
)
from .paths import SharedPathError, mac_shared_path_to_windows
from .service import PitStopService, PitStopServiceSettings
from .transport import (
    PitStopLaunchError,
    PitStopTimeoutError,
    PitStopTransport,
    PitStopTransportError,
    SSHSettings,
    SSHTransport,
    TransportResult,
)

__all__ = [
    "FailureKind",
    "GateConfig",
    "PitStopGate",
    "PitStopLaunchError",
    "PitStopReportUnreadableError",
    "PitStopTimeoutError",
    "shared_gate_for",
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
    "parse_occurrences",
    "parse_pitstop_payload",
    "parse_pitstop_report",
]
