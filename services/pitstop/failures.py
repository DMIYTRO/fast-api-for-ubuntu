"""Classification of PitStop check failures.

The only question the service asks about a failed attempt is "can a repeat
help?".  Failures that may be caused by several report-only jobs running at
once (or by a flaky SSH link) are ``TRANSIENT`` and get an exclusive retry.
Everything else is final: retrying a bad PDF, a changed input, a wrong
configuration or an authentication problem only wastes the server's time.
"""

from __future__ import annotations

from enum import StrEnum


class FailureKind(StrEnum):
    # Possibly concurrency related or a network blip: retry exclusively.
    TRANSIENT = "transient"
    # The command hung until its timeout.  Not retried (a second full-length
    # wait could stall a whole run) but counted as evidence for degradation.
    TIMEOUT = "timeout"
    # A repeat cannot help.
    DEFINITIVE = "definitive"


# Exit status the OpenSSH client uses for its own errors (the remote command's
# status is passed through unchanged).
SSH_CLIENT_ERROR_EXIT = 255
# PitStopServerCLI exit status seen as a transient failure when report-only
# jobs overlap: "exit code 1" without a usable report.
CLI_TRANSIENT_EXIT = 1

# Lower-case fragments of ssh stderr that mean "your configuration or
# credentials are wrong", never "try again".
_SSH_DEFINITIVE_MARKERS = (
    "permission denied",
    "host key verification failed",
    "remote host identification has changed",
    "no matching host key",
    "no matching key exchange",
    "could not resolve hostname",
    "no such identity",
    "bad configuration option",
    "bad permissions",
    "unprotected private key",
    "authentication failed",
    "too many authentication failures",
    "unknown host",
)


def is_ssh_definitive_failure(details: str) -> bool:
    lowered = details.lower()
    return any(marker in lowered for marker in _SSH_DEFINITIVE_MARKERS)


def classify_cli_exit(returncode: int, details: str) -> FailureKind:
    """Classify a non-zero exit status of the remote command.

    * ``1`` -- PitStopServerCLI failed without producing a verdict; the
      observed transient failure under concurrency.
    * ``255`` -- the SSH client itself failed (connection reset/refused/lost,
      broken pipe, ...) unless the output points to credentials, host keys
      or configuration.
    * anything else -- a status the CLI chose deliberately; not retried.
    """
    if returncode == SSH_CLIENT_ERROR_EXIT:
        if is_ssh_definitive_failure(details):
            return FailureKind.DEFINITIVE
        return FailureKind.TRANSIENT
    if returncode == CLI_TRANSIENT_EXIT:
        return FailureKind.TRANSIENT
    return FailureKind.DEFINITIVE
