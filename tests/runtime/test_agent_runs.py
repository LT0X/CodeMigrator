from __future__ import annotations

from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest

from codemigrator.core import Phase, SessionKind
from codemigrator.runtime.agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from codemigrator.runtime.contracts import EventSpec, RunState, agent_run_lifecycle_spec
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.store import (
    InMemoryRuntimeStore,
    StoreCommitError,
    _decode_agent_run,
    _dump_agent_run,
)

from .conftest import uid


def run_record(*, owner_id: UUID | None = None, key: str = "plan:1") -> AgentRun:
    return AgentRun(
        agent_run_id=AgentRunId(uuid4()),
        owner_kind="run",
        owner_id=owner_id or uid(),
        logical_task_key=key,
        phase=Phase.Plan,
        session_kind=SessionKind.PlanAuxiliary,
        thread_id=str(uuid4()),
        model_binding_sha256="a" * 64,
        context_sha256="b" * 64,
        toolset_sha256="c" * 64,
        template_sha256="d" * 64,
    )


def test_record_rejects_payload_fields_and_invalid_digest():
    record = run_record()
    assert isinstance(record.agent_run_id, UUID)
    for forbidden in ("prompt", "transcript", "source", "tool_payload"):
        with pytest.raises(TypeError):
            AgentRun(**{**asdict(record), forbidden: "secret"})
    with pytest.raises(ValueError, match="digest"):
        replace(record, context_sha256="raw context")
    with pytest.raises(ValueError, match="task key"):
        replace(record, logical_task_key="source code: print(secret)")
    with pytest.raises(ValueError, match="thread"):
        replace(record, thread_id="raw transcript")


def test_terminal_record_requires_matching_session_exit():
    record = run_record()
    with pytest.raises(ValueError, match="exit"):
        replace(record, state=SessionState.Closed)
    with pytest.raises(ValueError, match="exit"):
        replace(record, state=SessionState.Running, exit=SessionExit.Completed)
    with pytest.raises(ValueError, match="exit"):
        replace(record, state=SessionState.Failed, exit=SessionExit.Completed)
    assert (
        replace(record, state=SessionState.Closed, exit=SessionExit.Completed).exit
        is SessionExit.Completed
    )


def test_record_metadata_round_trip_and_lifecycle_spec_omit_private_refs():
    record = replace(run_record(), checkpoint_sha256="e" * 64)
    assert _decode_agent_run(_dump_agent_run(record)) == record
    started = agent_run_lifecycle_spec(record)
    assert started.event_type == "agent_run.started"
    assert started.data == {
        "agent_run_id": str(record.agent_run_id),
        "phase": "PLAN",
        "session_kind": "PLAN_AUXILIARY",
    }
    terminal = replace(record, state=SessionState.Closed, exit=SessionExit.Completed)
    receipt = AgentRunReceipt(uuid4(), terminal.agent_run_id, "plan.accepted")
    finished = agent_run_lifecycle_spec(terminal, receipt)
    assert finished.data["exit"] == "COMPLETED"
    assert finished.data["receipt_category"] == "plan.accepted"
    assert "checkpoint_sha256" not in finished.data
    with pytest.raises(ValueError, match="category"):
        AgentRunReceipt(uuid4(), terminal.agent_run_id, "tool output: secret")


@pytest.mark.asyncio
async def test_logical_key_is_idempotent_within_owner_only():
    store = InMemoryRuntimeStore()
    first = run_record()
    await store.create(RunState(run_id=first.owner_id), ())
    assert await store.create_or_get_agent_run(first) == first
    assert (
        await store.create_or_get_agent_run(replace(first, agent_run_id=AgentRunId(uuid4())))
        == first
    )
    assert await store.load_agent_run(first.agent_run_id) == first
    other_owner = run_record(key=first.logical_task_key)
    await store.create(RunState(run_id=other_owner.owner_id), ())
    assert await store.create_or_get_agent_run(other_owner) == other_owner
    with pytest.raises(StoreCommitError, match="identity mismatch"):
        await store.create_or_get_agent_run(
            replace(first, agent_run_id=AgentRunId(uuid4()), context_sha256="e" * 64)
        )
    with pytest.raises(StoreCommitError, match="identity mismatch"):
        await store.create_or_get_agent_run(replace(first, thread_id=str(uuid4())))


@pytest.mark.asyncio
async def test_run_agent_run_requires_existing_owner_run():
    store = InMemoryRuntimeStore()
    run_owned = run_record()
    with pytest.raises(StoreCommitError, match="owner Run does not exist"):
        await store.create_or_get_agent_run(run_owned)
    assert await store.load_agent_run(run_owned.agent_run_id) is None
    draft_owned = replace(run_owned, owner_kind="draft")
    assert await store.create_or_get_agent_run(draft_owned) == draft_owned


@pytest.mark.asyncio
async def test_distinct_logical_tasks_cannot_share_agent_thread():
    store = InMemoryRuntimeStore()
    first = run_record()
    await store.create(RunState(run_id=first.owner_id), ())
    await store.create_or_get_agent_run(first)
    duplicate_thread = replace(first, agent_run_id=AgentRunId(uuid4()), logical_task_key="plan:2")
    with pytest.raises(StoreCommitError, match="thread"):
        await store.create_or_get_agent_run(duplicate_thread)
    assert await store.load_agent_run(duplicate_thread.agent_run_id) is None
    assert (
        await store.create_or_get_agent_run(replace(first, agent_run_id=AgentRunId(uuid4())))
        == first
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("phase", "PLAN"),
        ("session_kind", "PLAN_AUXILIARY"),
        ("slice_ref", {"source": "raw payload"}),
        ("retry_of", "raw transcript"),
        ("continuation_of", {"tool_payload": "secret"}),
        ("restarted_from", "raw prompt"),
        ("model_binding_sha256", {"prompt": "secret"}),
        ("checkpoint_sha256", {"source": "secret"}),
        ("state", "CREATED"),
        ("exit", "COMPLETED"),
    ],
)
def test_record_rejects_payload_shaped_values_for_typed_fields(field, value):
    with pytest.raises(ValueError):
        replace(run_record(), **{field: value})


@pytest.mark.asyncio
async def test_owner_receipt_replay_and_run_event_are_atomic():
    store = InMemoryRuntimeStore()
    record = run_record()
    await store.create(RunState(run_id=record.owner_id), ())
    await store.create_or_get_agent_run(record)
    terminal = replace(
        record, state=SessionState.Closed, exit=SessionExit.Completed, result_sha256="e" * 64
    )
    receipt = AgentRunReceipt(
        receipt_id=uuid4(), agent_run_id=record.agent_run_id, category="plan.accepted"
    )
    event = EventSpec("plan.accepted", {"agent_run_id": str(record.agent_run_id)})
    committed = await store.commit_agent_run_receipt(
        terminal, receipt, state=RunState(run_id=record.owner_id, version=1), events=(event,)
    )
    assert committed == receipt
    assert await store.load_agent_run(record.agent_run_id) == terminal
    assert await store.load_agent_run_receipt(record.agent_run_id) == receipt
    assert (await store.load(record.owner_id)).events[0].event_type == "plan.accepted"
    retried_receipt = replace(receipt, receipt_id=uuid4())
    assert (
        await store.commit_agent_run_receipt(
            terminal,
            retried_receipt,
            state=RunState(run_id=record.owner_id, version=1),
            events=(event,),
        )
        == receipt
    )
    assert len((await store.load(record.owner_id)).events) == 1


@pytest.mark.asyncio
async def test_owner_receipt_failed_commit_rolls_back_record_and_event():
    store = InMemoryRuntimeStore()
    record = run_record()
    await store.create(RunState(run_id=record.owner_id), ())
    await store.create_or_get_agent_run(record)
    terminal = replace(record, state=SessionState.Closed, exit=SessionExit.Completed)
    receipt = AgentRunReceipt(
        receipt_id=uuid4(), agent_run_id=record.agent_run_id, category="plan.accepted"
    )
    store.fail_next_commit()
    with pytest.raises(StoreCommitError, match="injected"):
        await store.commit_agent_run_receipt(
            terminal,
            receipt,
            state=RunState(run_id=record.owner_id, version=1),
            events=(EventSpec("plan.accepted"),),
        )
    assert await store.load_agent_run(record.agent_run_id) == record
    assert await store.load_agent_run_receipt(record.agent_run_id) is None
    assert (await store.load(record.owner_id)).events == ()


@pytest.mark.asyncio
async def test_owner_receipt_rejects_stale_run_state_without_terminalizing():
    store = InMemoryRuntimeStore()
    record = run_record()
    await store.create(RunState(run_id=record.owner_id, version=4), ())
    await store.create_or_get_agent_run(record)
    terminal = replace(record, state=SessionState.Closed, exit=SessionExit.Completed)
    receipt = AgentRunReceipt(uuid4(), record.agent_run_id, "plan.accepted")
    with pytest.raises(StoreCommitError, match="version"):
        await store.commit_agent_run_receipt(
            terminal,
            receipt,
            state=RunState(run_id=record.owner_id, version=4),
            events=(EventSpec("plan.accepted"),),
        )
    assert await store.load_agent_run(record.agent_run_id) == record
    assert (await store.load(record.owner_id)).events == ()
