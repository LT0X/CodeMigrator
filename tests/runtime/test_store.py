from __future__ import annotations

import asyncio
from datetime import UTC

import pytest

from codemigrator.core import RunId, SecretRegistry
from codemigrator.runtime.agent_runs import AgentRunId
from codemigrator.runtime.contracts import CandidateCheckpointFact, EventSpec, RunState
from codemigrator.runtime.memory import EvolutionSegmentDraft
from codemigrator.runtime.schema import RUNTIME_SCHEMA_SQL
from codemigrator.runtime.store import (
    InMemoryRuntimeStore,
    StoreCommitError,
    _decode_state,
    _dump_json,
)


def test_runtime_state_round_trips_through_json_for_durable_store():
    from .conftest import create_run, uid

    state = RunState(
        run_id=uid(),
        create_request=create_run(),
        frozen_plan_sha256="a" * 64,
        candidate_checkpoints=(
            CandidateCheckpointFact(
                agent_run_id=AgentRunId(uid()),
                slice_id=uid(),
                generation=1,
                expected_candidate_oid="b" * 40,
                candidate_oid="c" * 40,
                receipt_sha256="d" * 64,
            ),
        ),
    )
    assert _decode_state(_dump_json(state)) == state


def test_runtime_schema_contains_separate_run_and_append_only_event_tables():
    assert "CREATE TABLE IF NOT EXISTS runtime_runs" in RUNTIME_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS runtime_events" in RUNTIME_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS draft_owner_facts" in RUNTIME_SCHEMA_SQL
    assert "PRIMARY KEY (run_id, sequence)" in RUNTIME_SCHEMA_SQL
    assert "UNIQUE (run_id, slice_id)" in RUNTIME_SCHEMA_SQL
    assert "context evolution template is frozen per Run" in RUNTIME_SCHEMA_SQL


@pytest.mark.asyncio
async def test_in_memory_commit_can_atomically_append_evolution_with_events():
    from .conftest import uid

    store = InMemoryRuntimeStore()
    run_id = uid()
    slice_id = uid()
    await store.create(RunState(run_id=run_id), ())
    await store.commit(
        RunState(run_id=run_id, version=1),
        (EventSpec("slice.integrated", {"slice_id": str(slice_id)}),),
        evolution=EvolutionSegmentDraft(run_id, slice_id, "verified slice", "a" * 64),
    )
    assert len((await store.load(run_id)).events) == 1
    entries = await store.evolution_segments(run_id=run_id)
    assert entries[0].slice_id == slice_id
    with pytest.raises(StoreCommitError, match="already been appended"):
        await store.commit(
            RunState(run_id=run_id, version=2),
            (),
            evolution=EvolutionSegmentDraft(run_id, slice_id, "duplicate", "a" * 64),
        )


@pytest.mark.asyncio
async def test_in_memory_store_rejects_registered_secret_before_materializing_events():
    from .conftest import uid

    registry = SecretRegistry()
    registry.register("runtime-secret")
    store = InMemoryRuntimeStore(secret_registry=registry)

    run_id = uid()
    with pytest.raises(StoreCommitError, match="observation rejected"):
        await store.create(
            RunState(run_id=run_id),
            (EventSpec("unsafe.event", {"summary": "runtime-secret"}),),
        )

    assert await store.load(run_id) is None


@pytest.mark.asyncio
async def test_in_memory_store_rejects_sensitive_event_without_status_change():
    from .conftest import uid

    store = InMemoryRuntimeStore()
    run_id = uid()
    with pytest.raises(StoreCommitError, match="observation rejected"):
        await store.create(
            RunState(run_id=run_id),
            (EventSpec("unsafe.event", {"content": "source"}),),
        )
    assert await store.load(run_id) is None


@pytest.mark.asyncio
async def test_in_memory_run_event_read_wait_and_terminal_cursor_are_sequence_scoped():
    from .conftest import uid

    store = InMemoryRuntimeStore()
    run_id = RunId(uid())
    await store.create(
        RunState(run_id=run_id),
        (EventSpec("agent_run.terminal", {"status": "COMPLETED"}),),
    )
    assert await store.is_run_stream_terminal(run_id, 1) is False

    assert await store.read_run_events(run_id, 1) == ()
    waiter = asyncio.create_task(store.wait_for_run_events(run_id, 1))
    await asyncio.sleep(0)
    await store.commit(
        RunState(run_id=run_id, version=1),
        (EventSpec("run.status_changed", {"run_status": "FAILED"}),),
    )
    await asyncio.wait_for(waiter, timeout=1)

    events = await store.read_run_events(run_id, 0)
    assert [event.sequence for event in events] == [1, 2]
    assert [event.event_type for event in events] == [
        "agent_run.terminal",
        "run.status_changed",
    ]
    assert all(event.timestamp_utc.tzinfo == UTC for event in events)
    assert events == await store.read_run_events(run_id, 0)
    assert await store.is_run_stream_terminal(run_id, 1) is False
    assert await store.is_run_stream_terminal(run_id, 2) is True


@pytest.mark.asyncio
async def test_in_memory_run_wait_rechecks_commit_between_read_and_wait_setup():
    from .conftest import uid

    store = InMemoryRuntimeStore()
    run_id = RunId(uid())
    await store.create(RunState(run_id=run_id), ())
    assert await store.read_run_events(run_id, 0) == ()
    await store.commit(
        RunState(run_id=run_id, version=1),
        (EventSpec("slice.status_changed", {"status": "RUNNING"}),),
    )
    await asyncio.wait_for(store.wait_for_run_events(run_id, 0), timeout=1)


@pytest.mark.asyncio
async def test_draft_terminal_cursor_uses_only_committed_terminal_event_types():
    from uuid import uuid4

    from codemigrator.runtime.contracts import DraftSessionEventSpec

    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    await store.commit_draft_owner_fact(
        draft_id,
        "close-fact-without-close-event",
        "draft.closed",
        {},
        events=(DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())}),),
    )
    assert await store.is_draft_session_terminal(draft_id, 1) is False

    await store.commit_draft_owner_fact(
        draft_id,
        "attached",
        "draft.attached_to_run",
        {},
        events=(DraftSessionEventSpec("session.attached_to_run", {"run_id": str(uuid4())}),),
    )
    assert await store.is_draft_session_terminal(draft_id, 1) is False
    assert await store.is_draft_session_terminal(draft_id, 2) is True
