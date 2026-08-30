from __future__ import annotations

from codemigrator.runtime.report import build_report


def test_report_template_is_deterministic_and_model_free():
    first = build_report(
        run_id="run-1",
        verified_commit="a" * 40,
        passed_checks=("compile", "test"),
        failed_checks=(),
    )
    second = build_report(
        run_id="run-1",
        verified_commit="a" * 40,
        passed_checks=("test", "compile"),
        failed_checks=(),
    )
    assert first == second
    assert "run-1" in first
    assert "a" * 40 in first
