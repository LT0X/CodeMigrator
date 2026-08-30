"""Pure execution-fact normalization and flaky reduction."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from codemigrator.core import ArtifactRef, CheckAction, CheckResult, CheckStatus, Sha256

from .checks import CheckResultEvidence

OUTPUT_LIMIT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ExecutionFacts:
    """Facts supplied by the trusted sandbox termination adapter."""

    exit_code: int | None = None
    cancelled: bool = False
    output_limit_exceeded: bool = False
    timed_out: bool = False
    infrastructure_error: bool = False
    seccomp_denied: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0

    def __post_init__(self) -> None:
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise TypeError("exit_code must be an integer or None")
        if self.stdout_bytes < 0 or self.stderr_bytes < 0:
            raise ValueError("captured output sizes cannot be negative")


@dataclass(frozen=True)
class LaunchReceipt:
    """Pre-registered empty artifact identities for both output streams."""

    check_id: str
    stdout: ArtifactRef
    stderr: ArtifactRef


@dataclass(frozen=True)
class NormalizedExecution:
    status: CheckStatus
    launch: LaunchReceipt | None = None


@dataclass(frozen=True)
class FlakyReduction:
    status: CheckStatus
    flaky: bool
    event_count: int = 0
    flaky_tests: tuple[str, ...] = ()
    failed_tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedTestAssessment:
    """Deterministic evidence annotation for a generated test file."""

    generated: bool
    low_quality: bool
    nontrivial_assertions: int
    confidence_tier: Literal["TRANSLATED_TESTS", "GENERATED_TESTS"]
    disclosure: str | None = None


def annotate_generated_result(
    result: CheckResult, assessment: GeneratedTestAssessment
) -> CheckResultEvidence:
    """Attach generated-test provenance without mutating the core result contract."""

    return CheckResultEvidence(
        result=result,
        generated=assessment.generated,
        low_quality=assessment.low_quality,
    )


def register_launch(check_id: str) -> LaunchReceipt:
    """Register canonical empty stdout/stderr artifacts before launch."""

    empty = ArtifactRef(
        sha256=Sha256(hashlib.sha256(b"").hexdigest()),
        size=0,
        media_type="application/octet-stream",
    )
    return LaunchReceipt(check_id=check_id, stdout=empty, stderr=empty)


def normalize_execution(
    facts: ExecutionFacts,
    *,
    launch: LaunchReceipt | None = None,
) -> NormalizedExecution | None:
    """Apply M-10's fixed precedence exactly once.

    Cancellation produces no CheckResult.  Resource facts take precedence over
    exit status; seccomp denial remains a semantic failure even with exit 0.
    """

    if facts.cancelled:
        return None
    if (
        facts.output_limit_exceeded
        or max(facts.stdout_bytes, facts.stderr_bytes) > OUTPUT_LIMIT_BYTES
    ):
        return NormalizedExecution(CheckStatus.OutputLimitExceeded, launch)
    if facts.timed_out:
        return NormalizedExecution(CheckStatus.TimedOut, launch)
    if facts.infrastructure_error:
        return NormalizedExecution(CheckStatus.InfrastructureError, launch)
    if facts.seccomp_denied:
        return NormalizedExecution(CheckStatus.Failed, launch)
    if facts.exit_code == 0:
        return NormalizedExecution(CheckStatus.Passed, launch)
    return NormalizedExecution(CheckStatus.Failed, launch)


def flaky_reduce(
    action: CheckAction,
    layer: Literal["LOCAL", "INTEGRATION", "FINAL"] | str,
    statuses: Sequence[CheckStatus] | Mapping[str, Sequence[CheckStatus]],
) -> FlakyReduction:
    """Reduce a Test retry sequence using a 2/3 majority.

    Only failed Test checks in Integration/Final are eligible.  A timeout is a
    resource fact, not flaky evidence, and therefore short-circuits reduction.
    """

    eligible = action is CheckAction.Test and str(layer) in {
        "INTEGRATION",
        "FINAL",
        "VerificationLayer.INTEGRATION",
        "VerificationLayer.FINAL",
    }
    if isinstance(statuses, Mapping):
        if not eligible:
            first_observation = next(iter(statuses.values()), (CheckStatus.Failed,))
            return FlakyReduction(status=first_observation[0], flaky=False)
        flaky_tests: list[str] = []
        failed_tests: list[str] = []
        timed_out = False
        for test_name in sorted(statuses, key=lambda value: value.encode("utf-8")):
            observations = tuple(statuses[test_name])
            if not observations or observations[0] is not CheckStatus.Failed:
                continue
            if any(status is CheckStatus.TimedOut for status in observations):
                timed_out = True
                failed_tests.append(test_name)
                continue
            if len(observations) != 3:
                failed_tests.append(test_name)
                continue
            passed = sum(status is CheckStatus.Passed for status in observations)
            if passed >= 2:
                flaky_tests.append(test_name)
            else:
                failed_tests.append(test_name)
        return FlakyReduction(
            status=(
                CheckStatus.TimedOut
                if timed_out
                else CheckStatus.Failed
                if failed_tests
                else CheckStatus.Passed
            ),
            flaky=bool(flaky_tests),
            event_count=1 if flaky_tests else 0,
            flaky_tests=tuple(flaky_tests),
            failed_tests=tuple(failed_tests),
        )

    statuses = list(statuses)
    if not eligible or not statuses or statuses[0] is not CheckStatus.Failed:
        return (
            FlakyReduction(status=statuses[0], flaky=False)
            if statuses
            else FlakyReduction(status=CheckStatus.Failed, flaky=False)
        )
    if any(status is CheckStatus.TimedOut for status in statuses):
        return FlakyReduction(status=CheckStatus.TimedOut, flaky=False)
    if len(statuses) != 3:
        return FlakyReduction(status=statuses[0], flaky=False)
    passed = sum(status is CheckStatus.Passed for status in statuses)
    if passed >= 2:
        return FlakyReduction(status=CheckStatus.Passed, flaky=True, event_count=1)
    return FlakyReduction(status=CheckStatus.Failed, flaky=False)


def assess_generated_test(
    source: str,
    *,
    generated: bool = True,
    minimum_nontrivial_assertions: int = 1,
) -> GeneratedTestAssessment:
    """Apply the LOW_QUALITY gate without model judgement.

    The first implementation targets Python test artifacts.  An assertion is
    non-trivial when it is not a literal boolean and does not compare two
    identical literals; this deliberately favors disclosure over optimistic
    acceptance when the syntax cannot be analyzed.
    """

    if minimum_nontrivial_assertions < 0:
        raise ValueError("minimum_nontrivial_assertions must be non-negative")
    if not generated:
        return GeneratedTestAssessment(False, False, 0, "TRANSLATED_TESTS")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("generated test source is not valid Python") from exc
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or isinstance(node.test, ast.Constant):
            continue
        if isinstance(node.test, ast.Compare):
            operands = [node.test.left, *node.test.comparators]
            first = operands[0]
            if len(operands) > 1 and isinstance(first, ast.Constant) and all(
                isinstance(operand, ast.Constant) and operand.value == first.value
                for operand in operands
            ):
                continue
        count += 1
    low_quality = count < minimum_nontrivial_assertions
    return GeneratedTestAssessment(
        generated=True,
        low_quality=low_quality,
        nontrivial_assertions=count,
        confidence_tier="GENERATED_TESTS",
        disclosure=(
            "generated tests prove translated code is self-consistent with the Agent's "
            "source understanding"
            if low_quality
            else None
        ),
    )
