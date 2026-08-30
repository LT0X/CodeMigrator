from __future__ import annotations

from codemigrator.core import CheckAction, DiagnosticSeverity, FileLine, Sha256
from codemigrator.core.models.verification import DiagnosticMapping
from codemigrator.verification.checks import (
    CheckResultEvidence,
    VerificationLayer,
    instantiate_checks,
    make_skipped_empty_result,
)
from codemigrator.verification.fingerprint import (
    compare_stability,
    verification_fingerprint,
)

from .conftest import check, result, template


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


def test_skipped_empty_disposition_is_semantic_but_generated_marker_is_not() -> None:
    required = check(CheckAction.Test, "1" * 64)
    spec = instantiate_checks(
        VerificationLayer.FINAL,
        [required],
        {required.template_sha256: template(CheckAction.Test, "pytest")},
        test_files=[],
    )[0]
    skipped = make_skipped_empty_result(spec, receipt_id=None)
    ordinary = result(required, invocation_hash=str(spec.invocation_hash))
    assert verification_fingerprint("1" * 40, "2" * 64, [skipped]) != verification_fingerprint(
        "1" * 40, "2" * 64, [ordinary]
    )
    assert verification_fingerprint(
        "1" * 40,
        "2" * 64,
        [ordinary],
    ) == verification_fingerprint(
        "1" * 40,
        "2" * 64,
        [CheckResultEvidence(result=ordinary, generated=True)],
    )


def test_stability_comparison_rejects_duplicate_check_ids() -> None:
    required = check(CheckAction.Test, "1" * 64)
    one = result(required, invocation_hash="b" * 64)
    try:
        compare_stability(
            tested_commit_oid="1" * 40,
            final_checks=[one, one],
            final_frozen_hash="2" * 64,
            prospective_checks=[one],
            prospective_frozen_hash="2" * 64,
            prospective_tested_commit_oid="1" * 40,
        )
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate check IDs must block stability comparison")
