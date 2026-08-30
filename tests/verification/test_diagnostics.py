from __future__ import annotations

from codemigrator.core import CheckAction, DiagnosticSeverity, FileLine, SliceId
from codemigrator.core.models.verification import TestIdentity as DiagnosticTestIdentity
from codemigrator.verification.diagnostics import (
    AttributionContext,
    DiagnosticParserRegistry,
    attribute_diagnostics,
    normalize_diagnostic_message,
)


def test_builtin_parsers_normalize_file_line_test_identity_and_unknown() -> None:
    registry = DiagnosticParserRegistry.with_builtins()
    mypy = registry.parse(
        CheckAction.TypeCheck,
        "mypy",
        "src/app.py:7: error: Incompatible return value [return-value]",
    )
    assert isinstance(mypy[0].target, FileLine)
    assert mypy[0].severity is DiagnosticSeverity.Error
    assert mypy[0].code == "return-value"

    pytest_result = registry.parse(
        CheckAction.Test,
        "pytest",
        "FAILED tests/test_app.py::test_create - AssertionError: wrong value",
    )
    assert isinstance(pytest_result[0].target, DiagnosticTestIdentity)
    assert pytest_result[0].target.test_name == "tests/test_app.py::test_create"

    unknown = registry.parse(CheckAction.Test, "new-runner", "failed in a future format")
    assert unknown[0].target.kind == "UNKNOWN"
    assert unknown[0].severity is DiagnosticSeverity.Error


def test_static_unique_attribution_is_reliable_and_warning_does_not_route() -> None:
    registry = DiagnosticParserRegistry.with_builtins()
    diagnostics = registry.parse(CheckAction.Lint, "ruff", "src/app.py:3:1: F401 unused import")
    slice_id = SliceId("00000000-0000-0000-0000-000000000001")
    result = attribute_diagnostics(
        diagnostics,
        AttributionContext(write_scopes={slice_id: {"src/app.py"}}),
    )
    assert result.candidate_slice_set == (slice_id,)
    assert result.reliability.value == "Reliable"


def test_test_failure_uses_symbol_evidence_before_file_fallback() -> None:
    registry = DiagnosticParserRegistry.with_builtins()
    diagnostics = registry.parse(
        CheckAction.Test, "pytest", "FAILED tests/test_api.py::test_get - failed"
    )
    implementation = SliceId("00000000-0000-0000-0000-000000000001")
    test_slice = SliceId("00000000-0000-0000-0000-000000000002")
    result = attribute_diagnostics(
        diagnostics,
        AttributionContext(
            write_scopes={implementation: {"src/api.py"}, test_slice: {"tests/test_api.py"}},
            test_to_symbols={"tests/test_api.py::test_get": ("Api.get",)},
            symbol_to_slices={"Api.get": (implementation,)},
        ),
    )
    assert result.candidate_slice_set == (implementation,)


def test_multiple_static_hits_and_strong_coupling_are_not_direct_routes() -> None:
    registry = DiagnosticParserRegistry.with_builtins()
    diagnostics = registry.parse(CheckAction.Compile, "go", "shared.go:9: undefined: Value")
    first = SliceId("00000000-0000-0000-0000-000000000001")
    second = SliceId("00000000-0000-0000-0000-000000000002")
    result = attribute_diagnostics(
        diagnostics,
        AttributionContext(
            write_scopes={first: {"shared.go"}, second: {"shared.go"}},
            interface_definition_slices=(first,),
            call_site_slices=(second,),
        ),
    )
    assert set(result.candidate_slice_set) == {first, second}
    assert result.strong_coupling is True
    assert result.reliability.value == "Uncertain"


def test_file_fallback_uses_test_file_and_dependency_facts_without_guessing() -> None:
    registry = DiagnosticParserRegistry.with_builtins()
    diagnostics = registry.parse(
        CheckAction.Test,
        "pytest",
        "FAILED tests/test_api.py::test_get - AssertionError",
    )
    test_slice = SliceId("00000000-0000-0000-0000-000000000002")
    result = attribute_diagnostics(
        diagnostics,
        AttributionContext(
            write_scopes={test_slice: {"tests/test_api.py"}},
            test_file_to_slices={"tests/test_api.py": (test_slice,)},
        ),
    )
    assert result.candidate_slice_set == (test_slice,)
    assert normalize_diagnostic_message("2026-08-30 /tmp/project/src/app.py:7") == (
        "<timestamp> <path>:7"
    )
