from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from codemigrator.core import (
    CandidateGeneration,
    GitOid,
    Phase,
    RunId,
    RunStatus,
    SessionKind,
    SliceCandidate,
    SliceGenerationRef,
    SliceId,
    WriteScope,
    WriteScopeOut,
)
from codemigrator.runtime.actor import RunActor
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.contracts import (
    CandidateCheckpointClaim,
    EventSpec,
    ExecutionRoundDecision,
    RunState,
    agent_run_lifecycle_spec,
)
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.recovery import (
    AgentRunRecoveryCoordinator,
    candidate_checkpoint_digest,
    write_scope_digest,
)
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError
from codemigrator.workspace import (
    CheckpointManifest,
    CheckpointReceipt,
    WorkspaceManager,
)

BASE_OID = "a" * 40
CANDIDATE_OID = "b" * 40


def _receipt(run_id: RunId, slice_id: SliceId, generation: int = 1) -> CheckpointReceipt:
    manifest = CheckpointManifest(
        slice_candidate=SliceCandidate(
            run_id=run_id,
            slice_id=slice_id,
            generation=CandidateGeneration(generation),
            base_verified_oid=GitOid("c" * 40),
            candidate_commit_oid=GitOid(BASE_OID),
        ),
        file_count=1,
        total_bytes=2,
        file_set_digest="d" * 64,
        scope_check_passed=True,
    )
    return CheckpointReceipt(
        run_id=run_id,
        slice_id=slice_id,
        generation=generation,
        expected_candidate_oid=BASE_OID,
        new_candidate_oid=CANDIDATE_OID,
        manifest=manifest,
        idempotency_key="e" * 64,
    )


def _agent_run(
    run_id: RunId,
    slice_id: SliceId,
    *,
    receipt: CheckpointReceipt | None = None,
    state: SessionState = SessionState.Closed,
) -> AgentRun:
    scope = WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[]))
    run = AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="run",
        owner_id=run_id,
        logical_task_key="execute:slice-1:g1",
        phase=Phase.Execute,
        session_kind=SessionKind.Implementation,
        thread_id=str(uuid4()),
        model_binding_sha256="1" * 64,
        context_sha256="2" * 64,
        toolset_sha256="3" * 64,
        template_sha256="4" * 64,
        write_scope_sha256=write_scope_digest(scope),
        slice_ref=SliceGenerationRef(
            slice_id=slice_id,
            generation=1,
            baseline_candidate_oid=BASE_OID,
        ),
        state=state,
        exit=SessionExit.Completed if state is SessionState.Closed else None,
    )
    if receipt is not None:
        run = replace(run, candidate_checkpoint_sha256=candidate_checkpoint_digest(receipt))
    return replace(run, result_sha256="5" * 64) if state is SessionState.Closed else run


class _CheckpointVerifier:
    def __init__(self, receipt: CheckpointReceipt | None) -> None:
        self.receipt = receipt

    def is_committed_receipt(self, receipt: CheckpointReceipt) -> bool:
        return receipt == self.receipt


class _CandidateSource:
    def __init__(self, snapshots: dict[str, dict[str, bytes]]) -> None:
        self.snapshots = snapshots

    def files_at(self, candidate_oid: str) -> dict[str, bytes]:
        return self.snapshots[candidate_oid]


class _OneRoundScheduler:
    def __init__(self, decision: ExecutionRoundDecision) -> None:
        self.decision = decision

    async def advance_one_round(self, run_id, logical_key, *, on_agent_run_started=None):
        del on_agent_run_started
        return self.decision


async def _store_with_terminal_agent(
    run_id: RunId,
    record: AgentRun,
) -> InMemoryRuntimeStore:
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(
            run_id=run_id,
            status=RunStatus.Executing,
            version=1,
            frozen_plan_sha256="f" * 64,
        ),
        (EventSpec("run.created", {"status": RunStatus.Executing.value}),),
    )
    created = await store.create_or_get_agent_run(
        replace(
            record,
            state=SessionState.Created,
            exit=None,
            result_sha256=None,
            candidate_checkpoint_sha256=None,
        )
    )
    started = replace(created, state=SessionState.Running)
    await store.commit(
        replace((await store.snapshot(run_id)).state, version=2),
        (agent_run_lifecycle_spec(started),),
    )
    terminal_receipt = AgentRunReceipt(uuid4(), record.agent_run_id, "session.terminal")
    await store.commit_agent_run_receipt(
        record,
        terminal_receipt,
        state=replace((await store.snapshot(run_id)).state, version=3),
        events=(agent_run_lifecycle_spec(record, terminal_receipt),),
    )
    assert created.agent_run_id == record.agent_run_id
    return store


@pytest.mark.asyncio
async def test_session_completed_without_m08_receipt_cannot_advance_actor_round() -> None:
    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    record = _agent_run(run_id, slice_id)
    store = await _store_with_terminal_agent(run_id, record)
    actor = RunActor(
        run_id,
        store,
        execution_scheduler=_OneRoundScheduler(
            ExecutionRoundDecision(
                complete=True,
                dispatch_count=0,
                completed_write_agent_run_ids=(record.agent_run_id,),
                candidate_claims=(CandidateCheckpointClaim(record.agent_run_id, None),),
            )
        ),
        candidate_checkpoint_verifier=_CheckpointVerifier(None),
    )
    await actor.start()
    before = await store.snapshot(run_id)

    with pytest.raises(StoreCommitError, match="candidate checkpoint"):
        await actor.advance_execution_round(run_id, f"run.execute.round:{run_id}:0")

    after = await store.snapshot(run_id)
    assert after.state == before.state
    assert after.events == before.events
    await actor.stop()


@pytest.mark.asyncio
async def test_actor_accepts_only_matching_persisted_candidate_checkpoint() -> None:
    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    receipt = _receipt(run_id, slice_id)
    record = _agent_run(run_id, slice_id, receipt=receipt)
    store = await _store_with_terminal_agent(run_id, record)
    actor = RunActor(
        run_id,
        store,
        execution_scheduler=_OneRoundScheduler(
            ExecutionRoundDecision(
                complete=True,
                dispatch_count=0,
                completed_write_agent_run_ids=(record.agent_run_id,),
                candidate_claims=(CandidateCheckpointClaim(record.agent_run_id, receipt),),
            )
        ),
        candidate_checkpoint_verifier=_CheckpointVerifier(receipt),
    )
    await actor.start()

    result = await actor.advance_execution_round(run_id, f"run.execute.round:{run_id}:0")

    snapshot = await store.snapshot(run_id)
    assert result.complete is True
    assert snapshot.state.candidate_checkpoints[0].candidate_oid == CANDIDATE_OID
    assert snapshot.state.candidate_checkpoints[0].agent_run_id == record.agent_run_id
    assert [event.event_type for event in snapshot.events[-2:]] == [
        "slice.candidate.accepted",
        "run.execute.round",
    ]
    assert "candidate_oid" not in snapshot.events[-2].data
    assert "candidate_checkpoint_sha256" not in snapshot.events[-2].data
    await actor.stop()


@pytest.mark.asyncio
async def test_actor_rejects_candidate_receipt_that_is_not_persisted_by_m08() -> None:
    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    receipt = _receipt(run_id, slice_id)
    record = _agent_run(run_id, slice_id, receipt=receipt)
    store = await _store_with_terminal_agent(run_id, record)
    actor = RunActor(
        run_id,
        store,
        execution_scheduler=_OneRoundScheduler(
            ExecutionRoundDecision(
                complete=True,
                dispatch_count=0,
                completed_write_agent_run_ids=(record.agent_run_id,),
                candidate_claims=(CandidateCheckpointClaim(record.agent_run_id, receipt),),
            )
        ),
        candidate_checkpoint_verifier=_CheckpointVerifier(None),
    )
    await actor.start()
    before = await store.snapshot(run_id)

    with pytest.raises(StoreCommitError, match="candidate checkpoint receipt"):
        await actor.advance_execution_round(run_id, f"run.execute.round:{run_id}:0")

    after = await store.snapshot(run_id)
    assert after.state == before.state
    assert after.events == before.events
    await actor.stop()


@pytest.mark.asyncio
async def test_failed_actor_commit_leaves_candidate_for_idempotent_replay() -> None:
    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    receipt = _receipt(run_id, slice_id)
    record = _agent_run(run_id, slice_id, receipt=receipt)
    store = await _store_with_terminal_agent(run_id, record)
    decision = ExecutionRoundDecision(
        complete=True,
        dispatch_count=0,
        completed_write_agent_run_ids=(record.agent_run_id,),
        candidate_claims=(CandidateCheckpointClaim(record.agent_run_id, receipt),),
    )
    actor = RunActor(
        run_id,
        store,
        execution_scheduler=_OneRoundScheduler(decision),
        candidate_checkpoint_verifier=_CheckpointVerifier(receipt),
    )
    await actor.start()
    before = await store.snapshot(run_id)
    store.fail_next_commit()

    with pytest.raises(StoreCommitError, match="receipt could not be committed"):
        await actor.advance_execution_round(run_id, f"run.execute.round:{run_id}:0")

    after_failure = await store.snapshot(run_id)
    assert after_failure.state == before.state
    assert after_failure.events == before.events
    result = await actor.advance_execution_round(run_id, f"run.execute.round:{run_id}:0")
    after_replay = await store.snapshot(run_id)
    assert result.complete is True
    assert after_replay.state.candidate_checkpoints[0].candidate_oid == CANDIDATE_OID
    await actor.stop()


@pytest.mark.asyncio
async def test_write_recovery_rebuilds_latest_candidate_with_new_thread_and_lineage(
    tmp_path,
) -> None:
    run_id, slice_id = RunId(uuid4()), SliceId(uuid4())
    receipt = _receipt(run_id, slice_id)
    previous = replace(
        _agent_run(run_id, slice_id, receipt=receipt, state=SessionState.Running),
        checkpoint_sha256="7" * 64,
    )
    manager = WorkspaceManager(tmp_path / "workspace")
    old_handle = manager.provision(run_id, slice_id, 1, "c" * 40, {"src/a.py": b"old"})
    manager.start_iteration(old_handle)
    scope = WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[]))
    source = {CANDIDATE_OID: {"src/a.py": b"candidate"}}
    store = InMemoryRuntimeStore()
    await store.create(
        RunState(run_id=run_id, status=RunStatus.Executing, version=1),
        (EventSpec("run.created", {"status": RunStatus.Executing.value}),),
    )
    coordinator = AgentRunRecoveryCoordinator(
        candidate_checkpoints=_CheckpointVerifier(receipt),
        candidate_source=_CandidateSource(source),
        workspace_manager=manager,
        agent_runs=store,
    )

    recovered = await coordinator.restart_write_session(
        previous,
        receipt,
        old_handle,
        write_scope=scope,
        context_sha256="6" * 64,
        agent_run_id=AgentRunId(uuid4()),
        thread_id=str(uuid4()),
    )

    assert recovered.workspace.generation == old_handle.generation
    assert recovered.workspace.candidate_oid == CANDIDATE_OID
    assert manager.root(recovered.workspace).read_bytes("src/a.py") == b"candidate"
    assert recovered.write_scope == scope
    assert recovered.agent_run.agent_run_id != previous.agent_run_id
    assert recovered.agent_run.thread_id != previous.thread_id
    assert recovered.agent_run.restarted_from == previous.agent_run_id
    assert recovered.agent_run.checkpoint_sha256 is None
    assert recovered.agent_run.slice_ref.baseline_candidate_oid == CANDIDATE_OID
    assert recovered.agent_run.write_scope_sha256 == previous.write_scope_sha256

    repeated = await coordinator.restart_write_session(
        previous,
        receipt,
        old_handle,
        write_scope=scope,
        context_sha256="6" * 64,
        agent_run_id=AgentRunId(uuid4()),
        thread_id=str(uuid4()),
    )
    assert repeated.agent_run.agent_run_id == recovered.agent_run.agent_run_id
    assert repeated.agent_run.thread_id == recovered.agent_run.thread_id


@pytest.mark.asyncio
async def test_readonly_recovery_reuses_original_thread_after_identity_and_cas_checks() -> None:
    record = AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="draft",
        owner_id=uuid4(),
        logical_task_key="draft:explore",
        phase=Phase.Plan,
        session_kind=SessionKind.ExploreCoordinator,
        thread_id=str(uuid4()),
        model_binding_sha256="1" * 64,
        context_sha256="2" * 64,
        toolset_sha256="3" * 64,
        template_sha256="4" * 64,
        checkpoint_sha256="5" * 64,
        state=SessionState.Running,
    )
    reader = _ValidCheckpointReader("5" * 64)
    coordinator = AgentRunRecoveryCoordinator(checkpoint_reader=reader)

    resumed = await coordinator.resume_readonly(record, record)

    assert resumed is record
    assert reader.calls == [(record.thread_id, record.checkpoint_sha256)]
    with pytest.raises(ValueError, match="recovery validation"):
        await coordinator.resume_readonly(record, replace(record, context_sha256="6" * 64))


class _ValidCheckpointReader:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = []

    async def verify_checkpoint(self, thread_id: str, digest: str) -> bool:
        self.calls.append((thread_id, digest))
        return digest == self.digest
