"""Single deterministic reduction from execution facts to a terminal decision."""

from __future__ import annotations

from enum import Enum

from pydantic import ConfigDict

from codemigrator.core import CheckStatus
from codemigrator.core._base import CoreModel


class TerminationCause(str, Enum):
    OutputLimit = "OUTPUT_LIMIT"
    Timeout = "TIMEOUT"
    Infrastructure = "INFRASTRUCTURE"
    SeccompDenied = "SECCOMP_DENIED"
    ProcessExit = "PROCESS_EXIT"
    Cancelled = "CANCELLED"


class TerminationDecision(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: TerminationCause
    status: CheckStatus | None
    returncode: int | None = None


def reduce_termination(
    *,
    cancelled: bool,
    output_limit: bool,
    timed_out: bool,
    infrastructure_failure: bool,
    seccomp_denied: bool,
    returncode: int | None,
) -> TerminationDecision:
    if cancelled:
        return TerminationDecision(
            cause=TerminationCause.Cancelled, status=None, returncode=returncode
        )
    if output_limit:
        return TerminationDecision(
            cause=TerminationCause.OutputLimit,
            status=CheckStatus.OutputLimitExceeded,
            returncode=returncode,
        )
    if timed_out:
        return TerminationDecision(
            cause=TerminationCause.Timeout, status=CheckStatus.TimedOut, returncode=returncode
        )
    if infrastructure_failure:
        return TerminationDecision(
            cause=TerminationCause.Infrastructure,
            status=CheckStatus.InfrastructureError,
            returncode=returncode,
        )
    if seccomp_denied:
        return TerminationDecision(
            cause=TerminationCause.SeccompDenied, status=CheckStatus.Failed, returncode=returncode
        )
    return TerminationDecision(
        cause=TerminationCause.ProcessExit,
        status=CheckStatus.Passed if returncode == 0 else CheckStatus.Failed,
        returncode=returncode,
    )
