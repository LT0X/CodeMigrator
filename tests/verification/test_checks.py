from __future__ import annotations

import uuid

import pytest

from codemigrator.core import CheckAction, CheckId, CheckStatus, StableErrorCode
from codemigrator.verification.checks import (
    VerificationLayer,
    frozen_required_checks_sha256,
    instantiate_checks,
    make_skipped_empty_result,
    validate_check_results,
)

from .conftest import check, result, template


def test_layers_select_the_documented_check_actions() -> None:
    compile_check = check(CheckAction.Compile, "1" * 64)
    lint_check = check(CheckAction.Lint, "2" * 64)
    type_check = check(CheckAction.TypeCheck, "3" * 64)
    test_check = check(CheckAction.Test, "4" * 64)
    scaffold_check = check(CheckAction.Scaffold, "5" * 64)
    templates = {
        item.template_sha256: template(item.action, item.action.value.lower())
        for item in (compile_check, lint_check, type_check, test_check, scaffold_check)
    }

    local = instantiate_checks(
        VerificationLayer.LOCAL,
        [test_check, type_check, compile_check, scaffold_check],
        templates,
    )
    integration = instantiate_checks(
        VerificationLayer.INTEGRATION,
        [test_check, type_check, compile_check, lint_check, scaffold_check],
        templates,
        test_files=["tests/test_app.py", "tests/test_unready.py"],
        test_coverage={"tests/test_app.py": {"slice-a"}, "tests/test_unready.py": {"slice-b"}},
        integrated_slices={"slice-a"},
    )
    final = instantiate_checks(
        VerificationLayer.FINAL,
        [test_check, type_check, compile_check],
        templates,
        test_files=["tests/test_app.py"],
    )

    assert {item.required_check.action for item in local} == {
        CheckAction.Compile,
        CheckAction.TypeCheck,
    }
    assert {item.required_check.action for item in integration} == {
        CheckAction.Compile,
        CheckAction.Lint,
        CheckAction.TypeCheck,
        CheckAction.Test,
    }
    assert {item.required_check.action for item in final} == {CheckAction.Test}
    assert next(
        item for item in integration if item.required_check.action is CheckAction.Test
    ).test_files == ("tests/test_app.py",)


def test_required_check_hash_is_byte_sorted_and_invocation_hash_excludes_paths() -> None:
    first = check(CheckAction.Compile, "1" * 64)
    second = check(CheckAction.Test, "2" * 64)
    assert frozen_required_checks_sha256([first, second]) == frozen_required_checks_sha256(
        [second, first]
    )

    templates = {first.template_sha256: template(CheckAction.Compile, "python")}
    one = instantiate_checks(
        VerificationLayer.LOCAL,
        [first],
        templates,
        candidate_path="/tmp/one",
    )[0]
    two = instantiate_checks(
        VerificationLayer.LOCAL,
        [first],
        templates,
        candidate_path="/tmp/two",
    )[0]
    assert one.invocation_hash == two.invocation_hash


def test_exact_set_errors_block_the_derived_guard() -> None:
    expected = [check(CheckAction.Compile, "1" * 64), check(CheckAction.Test, "2" * 64)]
    specs = instantiate_checks(
        VerificationLayer.INTEGRATION,
        expected,
        {
            item.template_sha256: template(item.action, item.action.value.lower())
            for item in expected
        },
        test_files=[],
    )
    compile_spec = next(spec for spec in specs if spec.check_id == expected[0].id)
    duplicate = result(expected[0], invocation_hash=str(compile_spec.invocation_hash))
    report = validate_check_results(specs, [duplicate, duplicate])
    assert set(report.errors) == {
        StableErrorCode.CHECK_MISSING,
        StableErrorCode.CHECK_DUPLICATE,
    }
    assert report.guard.all_required_checks_passed is False


def test_empty_test_is_a_passed_typed_receipt_without_execution() -> None:
    required = check(CheckAction.Test, "1" * 64)
    spec = instantiate_checks(
        VerificationLayer.FINAL,
        [required],
        {required.template_sha256: template(CheckAction.Test, "pytest")},
        test_files=[],
    )[0]
    receipt = make_skipped_empty_result(spec, receipt_id=None)
    assert receipt.status is CheckStatus.Passed
    assert receipt.disposition == "SkippedEmpty"
    assert receipt.result.status is CheckStatus.Passed


def test_skipped_empty_is_only_valid_for_a_known_empty_test_spec() -> None:
    compile_required = check(CheckAction.Compile, "1" * 64)
    compile_spec = instantiate_checks(
        VerificationLayer.LOCAL,
        [compile_required],
        {compile_required.template_sha256: template(CheckAction.Compile, "go")},
    )[0]
    with pytest.raises(ValueError, match="Test"):
        make_skipped_empty_result(compile_spec, receipt_id=None)

    test_required = check(CheckAction.Test, "2" * 64)
    test_spec = instantiate_checks(
        VerificationLayer.FINAL,
        [test_required],
        {test_required.template_sha256: template(CheckAction.Test, "pytest")},
        test_files=["tests/test_app.py"],
    )[0]
    with pytest.raises(ValueError, match="empty"):
        make_skipped_empty_result(test_spec, receipt_id=None)


def test_exact_set_requires_typed_skip_for_a_known_empty_test_spec() -> None:
    required = check(CheckAction.Test, "1" * 64)
    spec = instantiate_checks(
        VerificationLayer.FINAL,
        [required],
        {required.template_sha256: template(CheckAction.Test, "pytest")},
        test_files=[],
    )[0]
    skipped = make_skipped_empty_result(spec, receipt_id=None)
    assert validate_check_results([spec], [skipped]).guard.all_required_checks_passed is True
    assert (
        validate_check_results([spec], [skipped.result]).guard.all_required_checks_passed is False
    )


def test_error_unknown_blocks_the_derived_guard_even_when_checks_pass() -> None:
    required = check(CheckAction.Compile, "1" * 64)
    spec = instantiate_checks(
        VerificationLayer.LOCAL,
        [required],
        {required.template_sha256: template(CheckAction.Compile, "go")},
    )[0]
    diagnostic = {
        "severity": "Error",
        "target": {"kind": "UNKNOWN"},
        "code": "UNKNOWN_DIAGNOSTIC",
        "message_hash": "a" * 64,
    }
    passed = result(required, invocation_hash=str(spec.invocation_hash), diagnostics=[diagnostic])
    report = validate_check_results([spec], [passed])
    assert report.guard.error_unknown_count == 1
    assert report.guard.all_required_checks_passed is False


def test_integration_test_without_coverage_mapping_is_blocked() -> None:
    required = check(CheckAction.Test, "1" * 64)
    with pytest.raises(ValueError, match="coverage"):
        instantiate_checks(
            VerificationLayer.INTEGRATION,
            [required],
            {required.template_sha256: template(CheckAction.Test, "pytest")},
            test_files=["tests/test_app.py"],
            test_coverage={},
            integrated_slices={"slice-a"},
        )


def test_integration_empty_test_set_does_not_require_coverage_data() -> None:
    required = check(CheckAction.Test, "1" * 64)
    spec = instantiate_checks(
        VerificationLayer.INTEGRATION,
        [required],
        {required.template_sha256: template(CheckAction.Test, "pytest")},
        test_files=[],
        integrated_slices={"slice-a"},
    )[0]
    assert spec.test_files == ()


def test_unexpected_and_invocation_mismatch_are_independent_exact_set_errors() -> None:
    expected = [check(CheckAction.Compile, "1" * 64), check(CheckAction.Test, "2" * 64)]
    specs = instantiate_checks(
        VerificationLayer.INTEGRATION,
        expected,
        {
            item.template_sha256: template(item.action, item.action.value.lower())
            for item in expected
        },
        test_files=[],
    )
    first = next(spec for spec in specs if spec.check_id == expected[0].id)
    wrong = result(expected[0], invocation_hash="f" * 64)
    extra = result(expected[1], invocation_hash=str(next(
        spec for spec in specs if spec.check_id == expected[1].id
    ).invocation_hash))
    extra = extra.model_copy(update={"check_id": CheckId(uuid.uuid4())})
    report = validate_check_results(specs, [wrong, extra])
    assert StableErrorCode.CHECK_MISSING in report.errors
    assert StableErrorCode.CHECK_UNEXPECTED in report.errors
    assert StableErrorCode.INVOCATION_HASH_MISMATCH in report.errors
    assert first.check_id == expected[0].id


def test_descriptor_action_mismatch_is_rejected_before_execution() -> None:
    required = check(CheckAction.Compile, "1" * 64)
    with pytest.raises(ValueError, match="action mismatch"):
        instantiate_checks(
            VerificationLayer.LOCAL,
            [required],
            {required.template_sha256: template(CheckAction.TypeCheck, "mypy")},
        )
