import uuid
from dataclasses import replace

import pytest

from codemigrator.core import (
    ArtifactRef,
    AttributionReliability,
    RepairDecision,
    RepairDecisionId,
    RepairEvidence,
    RepoRelativePath,
    RunId,
    Sha256,
    SliceId,
    WriteScope,
    WriteScopeOut,
)
from codemigrator.runtime.integration import IntegrationCoordinator
from codemigrator.runtime.repair import (
    ActiveWriter,
    GlobalRepairOrchestrator,
    RepairDispatchRequest,
    RepairExecutionResult,
    RepairFailureFacts,
    RepairNavigationIndex,
    RepairSlice,
)


def scope(*paths: str) -> WriteScope:
    return WriteScope(out=WriteScopeOut(write_paths=list(paths), create_roots=[]))


class Runner:
    def __init__(self) -> None:
        self.dispatches = []

    async def run(self, dispatch):  # type: ignore[no-untyped-def]
        self.dispatches.append(dispatch)
        return RepairExecutionResult(
            candidate_commit_oid="candidate-1",
            checkpoint_passed=True,
            local_verification_passed=True,
        )


class Events:
    def __init__(self) -> None:
        self.values = []

    async def append(self, event):  # type: ignore[no-untyped-def]
        self.values.append(event)


def request() -> RepairDispatchRequest:
    run_id = RunId(uuid.uuid4())
    decision_id = RepairDecisionId(uuid.uuid4())
    slice_id = SliceId(uuid.uuid4())
    return RepairDispatchRequest(
        decision=RepairDecision(
            decision_id=decision_id,
            run_id=run_id,
            repair_set=[slice_id],
            domain_split={slice_id: [RepoRelativePath("src/a.py")]},
            brief_refs=[
                ArtifactRef(sha256=Sha256("a" * 64), size=1, media_type="application/json")
            ],
        ),
        repair_slices=(RepairSlice(slice_id, "INTEGRATED", scope("src/a.py")),),
        active_writers=(),
        evidence=RepairEvidence(
            candidate_slice_set=[slice_id],
            reliability=AttributionReliability.Uncertain,
            strong_coupling=True,
            cross_generation_recurrence=False,
            conservation_signal_summary={"reason": "ambiguous"},
        ),
        failure_facts=RepairFailureFacts(
            failed_test_refs=("test://suite/case",),
            diagnostic_summary={"code": "E001", "summary": "mismatch"},
            cas_refs=("cas://" + "a" * 64,),
        ),
        scope_index=RepairNavigationIndex(paths=("src/a.py",), positions=("src/a.py:10",)),
        repair_history=(),
        source_snapshot_oid="snapshot-1",
        contract_refs=("contract-1",),
        domain_workspace_refs=("workspace-1",),
        verified_head_oid="verified-1",
        verification_requirements=("local", "prospective", "final"),
        evidence_key="fingerprint-1",
        original_slice_id=slice_id,
        original_generation=2,
        cas_ref_validator=lambda _ref, current_run: current_run == str(run_id),
    )


@pytest.mark.asyncio
async def test_adopted_repair_runs_checkpoint_verify_and_enters_fifo():
    runner = Runner()
    events = Events()
    coordinator = IntegrationCoordinator()
    orchestrator = GlobalRepairOrchestrator(
        coordinator, runner, event_sink=events
    )
    result = await orchestrator.dispatch(request())
    assert result.accepted is True
    assert result.item is not None
    assert result.item.repair is True
    assert result.item.generation is None
    assert result.item.repair_decision_id is not None
    assert [event.event_type for event in events.values] == [
        "repair.session.started",
        "repair.session.integration_queued",
    ]
    assert len(runner.dispatches) == 1


@pytest.mark.asyncio
async def test_unsafe_repair_waits_without_calling_runner():
    runner = Runner()
    request_value = request()
    request_value = replace(
        request_value,
        active_writers=(ActiveWriter("other", "RUNNING", scope("src/a.py")),),
    )
    result = await GlobalRepairOrchestrator(IntegrationCoordinator(), runner).dispatch(
        request_value
    )
    assert result.accepted is False
    assert result.reason == "IN_FLIGHT_SCOPE_CONFLICT"
    assert runner.dispatches == []


def test_repair_request_requires_cas_ownership_validation_when_refs_are_present():
    with pytest.raises(ValueError, match="ownership validator"):
        replace(request(), cas_ref_validator=None)
