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

    later = auditor.record_round(
        AuditRound(diff=AuditDiff(findings=(AuditFinding("late finding"),)))
    )

    assert later is AuditState.Clean
    assert len(auditor.rounds) == 1


def test_escalated_audit_is_terminal_and_does_not_accept_a_fourth_round() -> None:
    auditor = CompletenessAuditor(seed=2)
    failing = AuditRound(diff=AuditDiff(findings=(AuditFinding("x"),)))

    assert auditor.record_round(failing) is AuditState.ReviseRules
    assert auditor.record_round(failing) is AuditState.ReviseRules
    assert auditor.record_round(failing) is AuditState.Escalate

    assert auditor.record_round(AuditRound(diff=AuditDiff(findings=()))) is AuditState.Escalate
    assert len(auditor.rounds) == 3
