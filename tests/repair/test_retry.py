import pytest

from codemigrator.runtime.repair import RepairAttemptGate, RepairLineage


def test_retry_gate_requires_new_evidence_and_allows_three_total_attempts():
    gate = RepairAttemptGate()
    assert gate.try_start("run-1", "decision-1", "evidence-1").accepted is True
    duplicate = gate.try_start("run-1", "decision-1", "evidence-1")
    assert duplicate.accepted is False
    assert duplicate.reason == "DUPLICATE_EVIDENCE"
    assert gate.try_start("run-1", "decision-1", "evidence-2").attempt == 2
    assert gate.try_start("run-1", "decision-1", "evidence-3").attempt == 3
    exhausted = gate.try_start("run-1", "decision-1", "evidence-4")
    assert exhausted.accepted is False
    assert exhausted.reason == "REPAIR_RETRY_EXHAUSTED"
    assert gate.attempts("run-1", "decision-1") == 3


def test_retry_gate_isolated_by_repair_session_identity_and_limit_is_configurable():
    gate = RepairAttemptGate(limit=1)
    assert gate.try_start("run-1", "decision-a", "evidence-1").accepted is True
    assert gate.try_start("run-1", "decision-b", "evidence-1").accepted is True
    with pytest.raises(ValueError):
        RepairAttemptGate(limit=0)


def test_lineage_marks_original_slice_superseded_without_changing_generation():
    lineage = RepairLineage.supersede(
        run_id="run-1", original_slice_id="slice-a", repair_decision_id="decision-1", generation=2
    )
    assert lineage.relation == "superseded-by-repair"
    assert lineage.original_generation == 2
    assert lineage.repair_decision_id == "decision-1"
