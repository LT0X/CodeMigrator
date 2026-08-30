from __future__ import annotations

from codemigrator.core import CheckAction, DiagnosticSeverity, FileLine, Sha256
from codemigrator.core.models.verification import DiagnosticMapping
from codemigrator.verification.fingerprint import (
    compare_stability,
    verification_fingerprint,
)

from .conftest import check, result


def test_fingerprint_ignores_receipt_and_artifact_carriers() -> None:
    required = check(CheckAction.Compile, "1" * 64)
    first = result(required, invocation_hash="b" * 64, artifact_fill="a")
    second = result(required, invocation_hash="b" * 64, artifact_fill="d")
    assert verification_fingerprint("1" * 40, "2" * 64, [first]) == verification_fingerprint(
        "1" * 40, "2" * 64, [second]
    )


def test_same_oid_shared_test_semantic_drift_is_nondeterministic() -> None:
    required = check(CheckAction.Test, "1" * 64)
    first = result(required, invocation_hash="b" * 64)
    diagnostic = DiagnosticMapping(
        severity=DiagnosticSeverity.Error,
        target=FileLine(kind="FILE_LINE", file_path="src/app.py", line=3),
        code="E1",
        message_hash=Sha256("a" * 64),
    )
    second = result(required, invocation_hash="b" * 64, diagnostics=[diagnostic.model_dump()])
    comparison = compare_stability(
        tested_commit_oid="1" * 40,
        final_checks=[first],
        final_frozen_hash="2" * 64,
        prospective_checks=[second],
        prospective_frozen_hash="3" * 64,
        prospective_tested_commit_oid="1" * 40,
    )
    assert comparison.nondeterministic is True
    assert comparison.reason == "NONDETERMINISTIC_VERIFICATION"


def test_same_oid_carrier_changes_do_not_trigger_stability_drift() -> None:
    required = check(CheckAction.Test, "1" * 64)
    first = result(required, invocation_hash="b" * 64, artifact_fill="a")
    second = result(required, invocation_hash="b" * 64, artifact_fill="d")
    comparison = compare_stability(
        tested_commit_oid="1" * 40,
        final_checks=[first],
        final_frozen_hash="2" * 64,
        prospective_checks=[second],
        prospective_frozen_hash="2" * 64,
        prospective_tested_commit_oid="1" * 40,
    )
    assert comparison.nondeterministic is False
