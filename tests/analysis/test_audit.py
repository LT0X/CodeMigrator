from codemigrator.analysis import (
    AuditDiff,
    AuditFinding,
    AuditRound,
    AuditState,
    CompletenessAuditor,
)


def test_audit_samples_are_disjoint_and_rounds_stop_after_three_failures() -> None:
    auditor = CompletenessAuditor(seed=7)
    samples = auditor.sample(
        files=["src/a.py", "src/b.py", "src/c.py", "tests/t.py"],
        random_count=2,
        adversarial=["tests/t.py", "src/c.py"],
    )

    assert set(samples.random_files).isdisjoint(samples.adversarial_files)
    assert (
        auditor.record_round(AuditRound(diff=AuditDiff(findings=(AuditFinding("x"),))))
        is AuditState.ReviseRules
    )
    assert (
        auditor.record_round(AuditRound(diff=AuditDiff(findings=(AuditFinding("x"),))))
        is AuditState.ReviseRules
    )
    assert (
        auditor.record_round(AuditRound(diff=AuditDiff(findings=(AuditFinding("x"),))))
        is AuditState.Escalate
    )


def test_clean_audit_round_closes_the_audit() -> None:
    auditor = CompletenessAuditor(seed=1)
    result = auditor.record_round(AuditRound(diff=AuditDiff(findings=())))

    assert result is AuditState.Clean
