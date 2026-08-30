from __future__ import annotations

import pytest

from codemigrator.core import SecretRegistry
from codemigrator.runtime.contracts import EventSpec, RunState
from codemigrator.runtime.memory import EvolutionSegmentDraft
from codemigrator.runtime.schema import RUNTIME_SCHEMA_SQL
from codemigrator.runtime.store import (
    InMemoryRuntimeStore,
    StoreCommitError,
    _decode_state,
    _dump_json,
)


def test_runtime_state_round_trips_through_json_for_durable_store():
    from .conftest import uid

    state = RunState(run_id=uid())
    assert _decode_state(_dump_json(state)) == state


def test_runtime_schema_contains_separate_run_and_append_only_event_tables():
    assert "CREATE TABLE IF NOT EXISTS runtime_runs" in RUNTIME_SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS runtime_events" in RUNTIME_SCHEMA_SQL
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
