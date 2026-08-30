from codemigrator.core import CheckAction, CheckStatus
from codemigrator.verification.execution import (
    ExecutionFacts,
    assess_generated_test,
    flaky_reduce,
    normalize_execution,
    register_launch,
)


def test_execution_facts_follow_the_fixed_precedence() -> None:
    assert normalize_execution(ExecutionFacts(cancelled=True, exit_code=0)) is None
    assert normalize_execution(ExecutionFacts(output_limit_exceeded=True, exit_code=0)).status is (
        CheckStatus.OutputLimitExceeded
    )
    assert (
        normalize_execution(ExecutionFacts(timed_out=True, exit_code=1)).status
        is CheckStatus.TimedOut
    )
    assert normalize_execution(ExecutionFacts(infrastructure_error=True, exit_code=0)).status is (
        CheckStatus.InfrastructureError
    )
    assert (
        normalize_execution(ExecutionFacts(seccomp_denied=True, exit_code=0)).status
        is CheckStatus.Failed
    )
    assert normalize_execution(ExecutionFacts(exit_code=0)).status is CheckStatus.Passed
    assert normalize_execution(ExecutionFacts(exit_code=1)).status is CheckStatus.Failed


def test_launch_registers_canonical_empty_artifacts_before_execution() -> None:
    launch = register_launch("check-1")
    assert launch.stdout.size == launch.stderr.size == 0
    assert launch.stdout.sha256 == launch.stderr.sha256
    normalized = normalize_execution(
        ExecutionFacts(infrastructure_error=True, exit_code=0),
        launch=launch,
    )
    assert normalized is not None
    assert normalized.launch == launch


def test_flaky_is_only_test_integration_or_final_and_timeout_is_not_flaky() -> None:
    assert (
        flaky_reduce(
            CheckAction.Test,
            "INTEGRATION",
            [CheckStatus.Failed, CheckStatus.Passed, CheckStatus.Passed],
        ).status
        is CheckStatus.Passed
    )
    assert (
        flaky_reduce(
            CheckAction.Test,
            "FINAL",
            [CheckStatus.Failed, CheckStatus.Passed, CheckStatus.Passed],
        ).flaky
        is True
    )
    assert (
        flaky_reduce(
            CheckAction.Test,
            "FINAL",
            [CheckStatus.Failed, CheckStatus.Failed, CheckStatus.Passed],
        ).status
        is CheckStatus.Failed
    )


def test_generated_test_quality_is_deterministic_and_separate_from_execution() -> None:
    low = assess_generated_test("def test_empty():\n    assert True\n")
    good = assess_generated_test("def test_value(value):\n    assert value == 'expected'\n")
    translated = assess_generated_test("", generated=False)
    assert low.low_quality is True
    assert good.nontrivial_assertions == 1
    assert good.low_quality is False
    assert translated.confidence_tier == "TRANSLATED_TESTS"
    assert (
        flaky_reduce(
            CheckAction.Test,
            "FINAL",
            [CheckStatus.Failed, CheckStatus.TimedOut],
        ).flaky
        is False
    )
    assert (
        flaky_reduce(
            CheckAction.Compile,
            "LOCAL",
            [CheckStatus.Failed],
        ).status
        is CheckStatus.Failed
    )
