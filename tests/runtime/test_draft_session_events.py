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


@pytest.mark.asyncio
async def test_api_command_commits_and_replays_draft_owner_receipt_atomically() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    question_id = str(uuid4())
    event = DraftSessionEventSpec("session.question.asked", {"question_id": question_id})
    body = b'{"kind":"DRAFT"}'

    async def command(transaction):
        return await store.commit_draft_owner_fact(
            draft_id,
            "draft.created",
            "draft.created",
            {"revision": 0},
            events=(event,),
            transaction=transaction,
        )

    def project(receipt):
        return {"session_id": str(receipt.draft_id), "revision": 0}

    def owner(receipt):
        return ("draft", receipt.draft_id, receipt.receipt_key)

    first = await store.execute_api_command(
        principal_id="local",
        route="/api/v1/sessions",
        key="create-draft-1",
        canonical_body=body,
        status_code=201,
        command=command,
        project_response=project,
        owner_receipt=owner,
    )
    replay = await store.execute_api_command(
        principal_id="local",
        route="/api/v1/sessions",
        key="create-draft-1",
        canonical_body=body,
        status_code=201,
        command=lambda _transaction: pytest.fail("replayed owner command ran twice"),
        project_response=project,
        owner_receipt=owner,
    )
    conflict = await store.execute_api_command(
        principal_id="local",
        route="/api/v1/sessions",
        key="create-draft-1",
        canonical_body=b'{"kind":"DIFFERENT"}',
        status_code=201,
        command=lambda _transaction: pytest.fail("conflicting owner command ran"),
        project_response=project,
        owner_receipt=owner,
    )

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["response"] == {"session_id": str(draft_id), "revision": 0}
    assert conflict == {"conflict": True, "replayed": False}
    assert await store.load_draft_owner_fact(draft_id, "draft.created") is not None
    assert [item.sequence for item in await store.read_draft_session_events(draft_id, 0)] == [1]


@pytest.mark.asyncio
async def test_api_command_projection_failure_rolls_back_draft_fact_and_event() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    event = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})
    body = b'{"kind":"DRAFT"}'

    async def command(transaction):
        return await store.commit_draft_owner_fact(
            draft_id,
            "draft.created",
            "draft.created",
            {"revision": 0},
            events=(event,),
            transaction=transaction,
        )

    with pytest.raises(RuntimeError, match="projection failed"):
        await store.execute_api_command(
            principal_id="local",
            route="/api/v1/sessions",
            key="create-draft-rollback",
            canonical_body=body,
            status_code=201,
            command=command,
            project_response=lambda _receipt: (_ for _ in ()).throw(
                RuntimeError("projection failed")
            ),
            owner_receipt=lambda receipt: (
                "draft",
                receipt.draft_id,
                receipt.receipt_key,
            ),
        )

    assert await store.load_draft_owner_fact(draft_id, "draft.created") is None
    assert await store.list_draft_owner_facts(draft_id) == ()
    assert await store.read_draft_session_events(draft_id, 0) == ()

    committed = await store.execute_api_command(
        principal_id="local",
        route="/api/v1/sessions",
        key="create-draft-rollback",
        canonical_body=body,
        status_code=201,
        command=command,
        project_response=lambda receipt: {
            "session_id": str(receipt.draft_id),
            "revision": 0,
        },
        owner_receipt=lambda receipt: (
            "draft",
            receipt.draft_id,
            receipt.receipt_key,
        ),
    )
    assert committed["replayed"] is False
    assert len(await store.read_draft_session_events(draft_id, 0)) == 1


@pytest.mark.asyncio
async def test_cancelled_draft_command_hides_staged_data_and_restores_terminal_state() -> None:
    store = InMemoryRuntimeStore()
    draft_id = uuid4()
    staged = asyncio.Event()
    attempts = 0

    async def command(transaction):
        nonlocal attempts
        attempts += 1
        receipt = await store.commit_draft_owner_fact(
            draft_id,
            "draft.closed",
            "draft.closed",
            {},
            events=(DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),),
            transaction=transaction,
        )
        if attempts == 1:
            staged.set()
            await asyncio.Event().wait()
        return receipt

    async def execute_command():
        return await store.execute_api_command(
            principal_id="local",
            route="/api/v1/sessions",
            key="cancel-draft-command",
            canonical_body=b'{"kind":"DRAFT"}',
            status_code=201,
            command=command,
            project_response=lambda receipt: {"session_id": str(receipt.draft_id)},
            owner_receipt=lambda receipt: (
                "draft",
                receipt.draft_id,
                receipt.receipt_key,
            ),
        )

    owner_task = asyncio.create_task(execute_command())
    readers = []
    try:
        await asyncio.wait_for(staged.wait(), 1)
        readers = [
            asyncio.create_task(store.load_draft_owner_fact(draft_id, "draft.closed")),
            asyncio.create_task(store.read_draft_session_events(draft_id, 0)),
            asyncio.create_task(store.is_draft_session_terminal(draft_id, 1)),
            asyncio.create_task(store.wait_for_draft_session_events(draft_id, 0)),
        ]
        await asyncio.sleep(0)
        assert all(not reader.done() for reader in readers)

        owner_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner_task

        assert await asyncio.wait_for(readers[0], 1) is None
        assert await asyncio.wait_for(readers[1], 1) == ()
        assert await asyncio.wait_for(readers[2], 1) is False
        await asyncio.sleep(0)
        assert not readers[3].done()
    finally:
        if not owner_task.done():
            owner_task.cancel()
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(owner_task, *readers, return_exceptions=True)

    committed = await execute_command()
    assert committed["replayed"] is False
    assert await store.load_draft_owner_fact(draft_id, "draft.closed") is not None
    assert await store.is_draft_session_terminal(draft_id, 1) is True
    assert [event.sequence for event in await store.read_draft_session_events(draft_id, 0)] == [1]


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
