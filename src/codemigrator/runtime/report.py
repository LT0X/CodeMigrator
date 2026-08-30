"""Deterministic report body assembly from already verified facts."""

from __future__ import annotations


def build_report(
    *,
    run_id: str,
    verified_commit: str,
    passed_checks: tuple[str, ...],
    failed_checks: tuple[str, ...],
) -> str:
    """Build a stable, model-free report from sorted semantic facts."""

    passed = sorted(set(passed_checks))
    failed = sorted(set(failed_checks))
    lines = [
        "# CodeMigrator Migration Report",
        "",
        f"Run: {run_id}",
        f"Verified commit: {verified_commit}",
        "",
        "## Verification",
        "",
        f"Passed checks: {', '.join(passed) if passed else 'none'}",
        f"Failed checks: {', '.join(failed) if failed else 'none'}",
    ]
    return "\n".join(lines) + "\n"


__all__ = ["build_report"]
