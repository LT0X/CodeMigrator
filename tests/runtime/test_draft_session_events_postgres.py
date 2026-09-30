"""Draft event ledger transactional behavior on an isolated PostgreSQL schema."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from codemigrator.runtime.contracts import DraftSessionEventSpec
from codemigrator.runtime.store import StoreCommitError

from .test_agent_runs_postgres import isolated_store


@pytest.mark.asyncio
async def test_postgres_concurrent_replay_has_one_fact_and_one_event() -> None:
    async with isolated_store() as store:
        draft_id = uuid4()
        event = DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())})

        async def commit():
            return await store.commit_draft_owner_fact(
                draft_id,
                "question:1",
                "draft.ask_user.question",
                {"private": "body"},
                events=(event,),
            )

        receipts = await asyncio.gather(*(commit() for _ in range(8)))
        assert all(receipt == receipts[0] for receipt in receipts)
        assert len(await store.list_draft_owner_facts(draft_id)) == 1
        first = await store.read_draft_session_events(draft_id, 0)
        assert len(first) == 1 and first[0].sequence == 1
        with pytest.raises(StoreCommitError, match="replay mismatch"):
            await store.commit_draft_owner_fact(
                draft_id,
                "question:1",
                "draft.ask_user.question",
                {"private": "body"},
                events=(
                    DraftSessionEventSpec("session.question.asked", {"question_id": str(uuid4())}),
                ),
            )
        with pytest.raises(StoreCommitError, match="replay mismatch"):
            await store.commit_draft_owner_fact(
                draft_id,
                "question:1",
                "draft.ask_user.question",
                {"private": "changed"},
                events=(event,),
            )
        assert await store.read_draft_session_events(draft_id, 0) == first
        restarted = type(store)(store.pool)
        assert await restarted.read_draft_session_events(draft_id, 0) == first
        assert await restarted.read_draft_session_events(draft_id, 1) == ()


@pytest.mark.asyncio
async def test_postgres_event_insert_failure_rolls_back_fact() -> None:
    async with isolated_store() as store:
        draft_id = uuid4()
        async with store.pool.acquire() as connection:
            await connection.execute("""CREATE FUNCTION reject_draft_event() RETURNS trigger AS $$
                BEGIN RAISE EXCEPTION 'reject test event'; END; $$ LANGUAGE plpgsql""")
            await connection.execute("""CREATE TRIGGER reject_draft_event
                BEFORE INSERT ON draft_session_events
                FOR EACH ROW EXECUTE FUNCTION reject_draft_event()""")
        with pytest.raises(Exception, match="reject test event"):
            await store.commit_draft_owner_fact(
                draft_id,
                "closed",
                "draft.closed",
                {},
                events=(DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),),
            )
        assert await store.list_draft_owner_facts(draft_id) == ()
        assert await store.read_draft_session_events(draft_id, 0) == ()


@pytest.mark.asyncio
async def test_postgres_wait_wakes_and_terminal_replays_after_restart() -> None:
    async with isolated_store() as store:
        draft_id = uuid4()
        waiter = asyncio.create_task(store.wait_for_draft_session_events(draft_id, 0))
        await asyncio.sleep(0.05)
        await store.commit_draft_owner_fact(
            draft_id,
            "draft.closed",
            "draft.closed",
            {},
            events=(DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),),
        )
        await asyncio.wait_for(waiter, 2)
        restarted = type(store)(store.pool)
        assert await restarted.is_draft_session_terminal(draft_id, 0) is False
        assert await restarted.is_draft_session_terminal(draft_id, 1) is True
