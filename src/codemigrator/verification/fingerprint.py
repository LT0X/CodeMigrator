"""Semantic verification fingerprints and same-commit drift detection."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from codemigrator.core import (
    CheckResult,
    FailureReason,
    FileLine,
    GitOid,
    RunId,
    Sha256,
    TestIdentity,
    Unknown,
    canonical_json_bytes,
)
from codemigrator.core.models.verification import VerificationOutcome, VerificationSubject

from .checks import CheckResultEvidence, ResultEvidence, SkippedEmptyResult


@dataclass(frozen=True)
class StabilityComparison:
    nondeterministic: bool
    reason: str | None
    compared_check_ids: tuple[str, ...] = ()


def _target_semantics(target: object) -> dict[str, object]:
    if isinstance(target, FileLine):
        return {"kind": target.kind, "file_path": target.file_path, "line": target.line}
    if isinstance(target, TestIdentity):
        return {"kind": target.kind, "test_name": target.test_name}
    if isinstance(target, Unknown):
        return {"kind": target.kind}
    return {"kind": "UNKNOWN"}


def _unwrap_result(result: ResultEvidence) -> CheckResult:
    if isinstance(result, (SkippedEmptyResult, CheckResultEvidence)):
        return result.result
    return result


def _result_disposition(result: ResultEvidence) -> str | None:
    if isinstance(result, SkippedEmptyResult):
        return result.disposition
    if isinstance(result, CheckResultEvidence):
        return result.disposition
    return None


def semantic_check_result(result: ResultEvidence) -> dict[str, object]:
    """Return the fingerprint payload for one result, excluding carriers."""

    unwrapped = _unwrap_result(result)
    diagnostics = [
        {
            "severity": diagnostic.severity.value,
            "target": _target_semantics(diagnostic.target),
            "code": diagnostic.code,
            "message_hash": str(diagnostic.message_hash),
        }
        for diagnostic in unwrapped.diagnostics
    ]
    diagnostics.sort(key=lambda item: canonical_json_bytes(item))
    semantic: dict[str, object] = {
        "check_id": str(unwrapped.check_id),
        "invocation_hash": str(unwrapped.invocation_hash),
        "status": unwrapped.status.value,
        "diagnostics": diagnostics,
    }
    disposition = _result_disposition(result)
    if disposition is not None:
        semantic["disposition"] = disposition
    return semantic


def verification_fingerprint(
    tested_commit_oid: str,
    frozen_required_checks_sha256: str,
    results: Iterable[ResultEvidence],
) -> Sha256:
    """Hash only the tested commit, frozen check set and semantic results."""

    materialized = list(results)
    check_ids = [str(_unwrap_result(result).check_id) for result in materialized]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError("duplicate CheckId blocks verification fingerprinting")
    semantic_results = sorted(
        (semantic_check_result(result) for result in materialized),
        key=lambda item: str(item["check_id"]).encode("utf-8"),
    )
    payload = {
        "tested_commit_oid": tested_commit_oid,
        "frozen_required_checks_sha256": frozen_required_checks_sha256,
        "semantic_results": semantic_results,
    }
    return Sha256(hashlib.sha256(canonical_json_bytes(payload)).hexdigest())


def build_verification_outcome(
    *,
    run_id: RunId,
    subject: VerificationSubject,
    tested_commit_oid: GitOid,
    frozen_required_checks_sha256: Sha256,
    results: Sequence[ResultEvidence],
) -> VerificationOutcome:
    """Assemble the core outcome without adding a second public contract."""

    unwrapped = [_unwrap_result(result) for result in results]
    return VerificationOutcome(
        run_id=run_id,
        subject=subject,
        tested_commit_oid=tested_commit_oid,
        frozen_required_checks_sha256=frozen_required_checks_sha256,
        check_results=unwrapped,
        verification_fingerprint=verification_fingerprint(
            str(tested_commit_oid), str(frozen_required_checks_sha256), results
        ),
    )


@dataclass(frozen=True)
class VerificationOutcomeEvidence:
    """Core outcome plus non-fingerprint provenance annotations."""

    outcome: VerificationOutcome
    result_evidence: tuple[CheckResultEvidence, ...]


def build_verification_evidence(
    *,
    run_id: RunId,
    subject: VerificationSubject,
    tested_commit_oid: GitOid,
    frozen_required_checks_sha256: Sha256,
    results: Sequence[ResultEvidence],
) -> VerificationOutcomeEvidence:
    """Assemble generated/skipped evidence beside the closed core outcome."""

    evidence = tuple(
        result
        if isinstance(result, CheckResultEvidence)
        else result.as_evidence()
        if isinstance(result, SkippedEmptyResult)
        else CheckResultEvidence(result=result)
        for result in results
    )
    return VerificationOutcomeEvidence(
        outcome=build_verification_outcome(
            run_id=run_id,
            subject=subject,
            tested_commit_oid=tested_commit_oid,
            frozen_required_checks_sha256=frozen_required_checks_sha256,
            results=evidence,
        ),
        result_evidence=evidence,
    )


def _validate_unique_check_ids(results: Sequence[ResultEvidence], label: str) -> None:
    check_ids = [str(_unwrap_result(result).check_id) for result in results]
    if len(check_ids) != len(set(check_ids)):
        raise ValueError(f"duplicate CheckId blocks {label} stability comparison")


def compare_stability(
    *,
    tested_commit_oid: str,
    final_checks: Sequence[ResultEvidence],
    final_frozen_hash: str,
    prospective_checks: Sequence[ResultEvidence],
    prospective_frozen_hash: str,
    prospective_tested_commit_oid: str,
) -> StabilityComparison:
    """Compare Final and Prospective semantics only when the commit is shared."""

    _validate_unique_check_ids(final_checks, "Final")
    _validate_unique_check_ids(prospective_checks, "Prospective")
    if tested_commit_oid != prospective_tested_commit_oid:
        return StabilityComparison(False, None)
    final_by_id = {
        str(_unwrap_result(result).check_id): result for result in final_checks
    }
    prospective_by_id = {
        str(_unwrap_result(result).check_id): result for result in prospective_checks
    }
    shared = tuple(
        sorted(final_by_id.keys() & prospective_by_id.keys(), key=lambda value: value.encode())
    )
    if not shared:
        return StabilityComparison(False, None)
    if final_frozen_hash == prospective_frozen_hash:
        final_fp = verification_fingerprint(tested_commit_oid, final_frozen_hash, final_checks)
        prospective_fp = verification_fingerprint(
            prospective_tested_commit_oid, prospective_frozen_hash, prospective_checks
        )
        drifted = final_fp != prospective_fp
    else:
        drifted = any(
            semantic_check_result(final_by_id[check_id])
            != semantic_check_result(prospective_by_id[check_id])
            for check_id in shared
        )
    return StabilityComparison(
        nondeterministic=drifted,
        reason=FailureReason.NondeterministicVerification.value if drifted else None,
        compared_check_ids=shared,
    )
