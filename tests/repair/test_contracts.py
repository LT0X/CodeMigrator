import uuid

import pytest

from codemigrator.core import (
    AttributionReliability,
    GlobalRepairSession,
    RepairDecisionId,
    RepairEvidence,
    RunId,
    SliceId,
    WriteScope,
    WriteScopeOut,
)
from codemigrator.runtime.repair import (
    ActiveWriter,
    RepairBrief,
    RepairConstraints,
    RepairFailureFacts,
    RepairHistoryEntry,
    RepairNavigationIndex,
    RepairReadScope,
    RepairSessionIdentity,
    SituationalSnapshot,
    build_repair_session_dispatch,
)


def uid() -> uuid.UUID:
    return uuid.uuid4()


def scope(*paths: str, roots: tuple[str, ...] = ()) -> WriteScope:
    return WriteScope(  # type: ignore[call-arg]
        out=WriteScopeOut(write_paths=list(paths), create_roots=list(roots))
    )


def evidence(*slice_ids: str) -> RepairEvidence:
    return RepairEvidence(
        candidate_slice_set=[SliceId(uuid.UUID(value)) for value in slice_ids],
        reliability=AttributionReliability.Uncertain,
        strong_coupling=True,
        cross_generation_recurrence=False,
        conservation_signal_summary={"status": "ambiguous"},
    )


def brief() -> RepairBrief:
    return RepairBrief(
        attribution=evidence(str(SLICE_A), str(SLICE_B)),
        failure_facts=RepairFailureFacts(
            failed_test_refs=("test://suite/test_case",),
            diagnostic_summary={"code": "E001", "summary": "contract mismatch"},
            cas_refs=("cas://" + "a" * 64,),
        ),
        scope_index=RepairNavigationIndex(
            paths=("src/a.py", "src/b.py"), positions=("src/a.py:10",)
        ),
        repair_history=(RepairHistoryEntry("decision-1", "rejected"),),
        constraints=RepairConstraints(
            write_scope=scope("src/a.py", "src/b.py"),
            verification_requirements=("local", "prospective", "final"),
            impact_preview_required=False,
        ),
    )


RUN_ID = uid()
DECISION_ID = uid()
SLICE_A = uid()
SLICE_B = uid()


def test_repair_session_identity_has_no_candidate_generation_and_is_immutable():
    identity = RepairSessionIdentity(RunId(RUN_ID), RepairDecisionId(DECISION_ID))
    assert identity.session_kind.value == "REPAIR_SESSION"
    assert identity.generation is None
    with pytest.raises(AttributeError):
        identity.run_id = RunId(uid())  # type: ignore[misc]


def test_repair_brief_contains_all_five_required_sections_and_is_immutable():
    value = brief()
    assert value.attribution.candidate_slice_set == [SliceId(SLICE_A), SliceId(SLICE_B)]
    assert value.failure_facts.cas_refs == ("cas://" + "a" * 64,)
    assert value.scope_index.paths == ("src/a.py", "src/b.py")
    assert value.repair_history[0].decision_id == "decision-1"
    assert value.constraints.impact_preview_required is False
    with pytest.raises(TypeError):
        value.failure_facts.diagnostic_summary["new"] = "not mutable"  # type: ignore[index]


def test_repair_history_rejects_duplicate_decisions():
    value = brief()
    with pytest.raises(ValueError, match="duplicate"):
        RepairBrief(
            attribution=value.attribution,
            failure_facts=value.failure_facts,
            scope_index=value.scope_index,
            repair_history=(
                RepairHistoryEntry("decision-1", "rejected"),
                RepairHistoryEntry("decision-1", "rejected"),
            ),
            constraints=value.constraints,
        )


def test_repair_dispatch_binds_joint_scope_to_brief_and_read_scope():
    value = brief()
    session = build_repair_session_dispatch(
        session=GlobalRepairSession(
            repair_decision_id=RepairDecisionId(DECISION_ID),
            run_id=RunId(RUN_ID),
            joint_write_scope=value.constraints.write_scope,
        ),
        read_scope=RepairReadScope(
            source_snapshot_oid="snapshot-1",
            contract_refs=("contract-1",),
            domain_workspace_refs=("workspace-1",),
            verified_head_oid="a" * 40,
        ),
        brief=value,
    )
    assert session.identity.session_kind.value == "REPAIR_SESSION"
    assert session.impact_preview_required is False


def test_situational_snapshot_is_machine_derived_and_has_no_context_serializer():
    snapshot = SituationalSnapshot(
        slice_states={str(SLICE_A): "INTEGRATED"},
        verified_oid="a" * 40,
        active_dispatches=("dispatch-1",),
        budget_ratio=0.75,
        prior_repair_history=("decision-1",),
    )
    assert snapshot.slice_states[str(SLICE_A)] == "INTEGRATED"
    assert snapshot.budget_ratio == 0.75
    assert not hasattr(snapshot, "to_context")


def test_active_writer_rejects_non_active_status_in_constructor():
    with pytest.raises(ValueError, match="active writer"):
        ActiveWriter("slice-a", "INTEGRATED", scope("src/a.py"))
