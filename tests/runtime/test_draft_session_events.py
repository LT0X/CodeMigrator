"""Draft owner events are one durable, redacted projection of committed facts."""

from __future__ import annotations

import asyncio
from datetime import UTC
from uuid import uuid4

import pytest

from codemigrator.api.deps import EventRecord
from codemigrator.api.dto import SessionEvent
from codemigrator.core import SecretRegistry
from codemigrator.runtime.contracts import DraftSessionEventSpec
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


@pytest.mark.asyncio
async def test_draft_fact_and_event_replay_is_ordered_idempotent_and_terminal() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    question = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})
    receipt = await store.commit_draft_owner_fact(
        draft_id, "question:1", "draft.ask_user.question", {"prompt": "private"}, events=(question,)
    )
    first = await store.read_draft_session_events(draft_id, 0)
    assert [event.sequence for event in first] == [1]
    assert first[0].timestamp_utc.tzinfo == UTC
    assert first[0].data == question.data
    assert (
        await store.commit_draft_owner_fact(
            draft_id,
            "question:1",
            "draft.ask_user.question",
            {"prompt": "private"},
            events=(question,),
        )
        == receipt
    )
    assert await store.read_draft_session_events(draft_id, 1) == ()
    with pytest.raises(StoreCommitError, match="replay mismatch"):
        await store.commit_draft_owner_fact(
            draft_id,
            "question:1",
            "draft.ask_user.question",
            {"prompt": "private"},
            events=(
                DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())}),
            ),
        )
    with pytest.raises(StoreCommitError, match="replay mismatch"):
        await store.commit_draft_owner_fact(
            draft_id,
            "question:1",
            "draft.ask_user.question",
            {"prompt": "changed"},
            events=(question,),
        )
    await store.commit_draft_owner_fact(
        draft_id,
        "closed",
        "draft.closed",
        {"thread_id": "private"},
        events=(DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),),
    )
    assert [event.sequence for event in await store.read_draft_session_events(draft_id, 0)] == [
        1,
        2,
    ]
    assert await store.is_draft_session_terminal(draft_id, 1) is False
    assert await store.is_draft_session_terminal(draft_id, 2) is True


@pytest.mark.asyncio
async def test_draft_event_validation_failure_rolls_back_fact() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    with pytest.raises(StoreCommitError):
        await store.commit_draft_owner_fact(
            draft_id,
            "question:1",
            "draft.ask_user.question",
            {"prompt": "private"},
            events=(DraftSessionEventSpec("assistant.delta", {"text": "private"}),),
        )
    assert await store.list_draft_owner_facts(draft_id) == ()
    assert await store.read_draft_session_events(draft_id, 0) == ()


@pytest.mark.asyncio
async def test_draft_agent_lifecycle_accepts_only_existing_public_fields() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    agent_run_id = str(uuid4())
    await store.commit_draft_owner_fact(
        draft_id,
        "agent:start",
        "draft.agent.started",
        {},
        events=(
            DraftSessionEventSpec(
                "agent_run.started",
                {
                    "agent_run_id": agent_run_id,
                    "phase": "PLAN",
                    "session_kind": "EXPLORE_COORDINATOR",
                    "receipt_key": "internal",
                    "thread_id": "internal",
                    "message": "raw body",
                },
            ),
        ),
    )
    event = (await store.read_draft_session_events(draft_id, 0))[0]
    assert event.data == {
        "agent_run_id": agent_run_id,
        "phase": "PLAN",
        "session_kind": "EXPLORE_COORDINATOR",
    }
    with pytest.raises(StoreCommitError, match="invalid"):
        await store.commit_draft_owner_fact(
            draft_id,
            "agent:bad",
            "draft.agent.started",
            {},
            events=(
                DraftSessionEventSpec(
                    "agent_run.started",
                    {
                        "agent_run_id": agent_run_id,
                        "phase": "WRONG",
                        "session_kind": "EXPLORE_COORDINATOR",
                    },
                ),
            ),
        )
    assert await store.load_draft_owner_fact(draft_id, "agent:bad") is None


@pytest.mark.asyncio
async def test_draft_terminal_category_rejects_private_error_text() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    with pytest.raises(StoreCommitError, match="invalid"):
        await store.commit_draft_owner_fact(
            draft_id,
            "terminal",
            "draft.agent.terminal",
            {},
            events=(
                DraftSessionEventSpec(
                    "agent_run.terminal",
                    {
                        "agent_run_id": str(uuid4()),
                        "phase": "PLAN",
                        "session_kind": "EXPLORE_COORDINATOR",
                        "exit": "FAILED",
                        "receipt_category": "Private error body",
                    },
                ),
            ),
        )
    assert await store.list_draft_owner_facts(draft_id) == ()


@pytest.mark.asyncio
async def test_draft_event_reads_cannot_mutate_committed_payload() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    spec = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})
    await store.commit_draft_owner_fact(
        draft_id, "question", "draft.ask_user.question", {}, events=(spec,)
    )
    first = (await store.read_draft_session_events(draft_id, 0))[0]
    first.data["question_id"] = "tampered"
    assert (await store.read_draft_session_events(draft_id, 0))[0].data == spec.data
    await store.commit_draft_owner_fact(
        draft_id, "question", "draft.ask_user.question", {}, events=(spec,)
    )


@pytest.mark.asyncio
async def test_draft_event_wait_wakes_after_commit() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    waiter = asyncio.create_task(store.wait_for_draft_session_events(draft_id, 0))
    await asyncio.sleep(0)
    await store.commit_draft_owner_fact(
        draft_id,
        "closed",
        "draft.closed",
        {},
        events=(DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),),
    )
    await asyncio.wait_for(waiter, 1)


def test_session_event_projection_discards_internal_details_and_redacts_secret() -> None:
    registry = SecretRegistry()
    registry.register("sensitive-token")
    record = EventRecord(
        run_id=uuid4(),
        sequence=1,
        event_type="agent_run.started",
        data={
            "agent_run_id": str(uuid4()),
            "phase": "PLAN",
            "session_kind": "EXPLORE_COORDINATOR",
            "receipt_key": "private",
            "checkpoint_sha256": "private",
            "message": "sensitive-token",
        },
        timestamp_utc=__import__("datetime").datetime.now(UTC),
    )
    projected = SessionEvent.from_record(record, secret_registry=registry)
    assert set(projected.data) == {"agent_run_id", "phase", "session_kind"}
    assert "sensitive-token" not in repr(projected)
