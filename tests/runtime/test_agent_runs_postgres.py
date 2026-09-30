"""AgentRun PostgreSQL contract tests against an isolated temporary schema."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import asyncpg
import pytest

from codemigrator.runtime.agent_runs import AgentRunId, AgentRunReceipt
from codemigrator.runtime.cas import CasObject
from codemigrator.runtime.contracts import EventSpec, RunState
from codemigrator.runtime.loop_contracts import SessionExit, SessionState
from codemigrator.runtime.store import PostgreSQLRuntimeStore, StoreCommitError

from .test_agent_runs import run_record


@asynccontextmanager
async def isolated_store():
    dsn = os.environ.get("CODEMIGRATOR_TEST_PG_DSN")
    if not dsn:
        pytest.skip("CODEMIGRATOR_TEST_PG_DSN is not configured")
    schema = f"agent_runs_test_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(dsn, server_settings={"search_path": schema})
        store = PostgreSQLRuntimeStore(pool)
        await store.initialize()
        yield store
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_run_owner_gate_has_zero_agent_run_side_effects():
    async with isolated_store() as store:
        record = run_record()
        with pytest.raises(StoreCommitError, match="owner Run does not exist"):
            await store.create_or_get_agent_run(record)
        assert await store.load_agent_run(record.agent_run_id) is None
        draft = replace(record, owner_kind="draft")
        assert await store.create_or_get_agent_run(draft) == draft


@pytest.mark.asyncio
async def test_postgres_concurrent_replay_and_unique_thread():
    async with isolated_store() as store:
        first = run_record()
        await store.create(RunState(run_id=first.owner_id), ())
        replay = replace(first, agent_run_id=AgentRunId(uuid4()), thread_id=str(uuid4()))
        records = await asyncio.gather(
            store.create_or_get_agent_run(first),
            store.create_or_get_agent_run(replay),
        )
        assert records[0] == records[1]
        different_task = replace(first, agent_run_id=AgentRunId(uuid4()), logical_task_key="plan:2")
        with pytest.raises(StoreCommitError, match="thread"):
            await store.create_or_get_agent_run(different_task)
        assert await store.load_agent_run(different_task.agent_run_id) is None


@pytest.mark.asyncio
async def test_postgres_receipt_failure_rolls_back_run_agent_and_event():
    async with isolated_store() as store:
        record = run_record()
        await store.create(RunState(run_id=record.owner_id), ())
        await store.create_or_get_agent_run(record)
        terminal = replace(
            record,
            state=SessionState.Closed,
            exit=SessionExit.Completed,
            result_sha256="e" * 64,
        )
        receipt = AgentRunReceipt(uuid4(), record.agent_run_id, "plan.accepted")
        cas_references = ((f"agent-result:{record.agent_run_id}", CasObject("e" * 64, 12)),)
        with pytest.raises(StoreCommitError, match="observation rejected"):
            await store.commit_agent_run_receipt(
                terminal,
                receipt,
                state=RunState(run_id=record.owner_id, version=1),
                events=(EventSpec("plan.accepted", {"content": "raw source"}),),
                cas_references=cas_references,
            )
        assert (await store.load(record.owner_id)).state.version == 0
        assert (await store.load(record.owner_id)).events == ()
        assert await store.load_agent_run(record.agent_run_id) == record
        assert await store.load_agent_run_receipt(record.agent_run_id) is None
        assert await store.get_cas_reference("run", record.owner_id, cas_references[0][0]) is None
        assert (
            await store.commit_agent_run_receipt(
                terminal,
                receipt,
                state=RunState(run_id=record.owner_id, version=1),
                events=(EventSpec("plan.accepted", {"category": "plan"}),),
                cas_references=cas_references,
            )
            == receipt
        )
        assert await store.load_agent_run_receipt(record.agent_run_id) == receipt
        assert (
            await store.get_cas_reference("run", record.owner_id, cas_references[0][0])
            == cas_references[0][1]
        )


@pytest.mark.asyncio
async def test_postgres_draft_owner_fact_receipt_is_idempotent():
    async with isolated_store() as store:
        draft_id = uuid4()
        key = "draft.answer:question-1"
        fact = {"question_id": "question-1", "selected_option": "preserve"}
        receipt = await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)
        assert (
            await store.commit_draft_owner_fact(draft_id, key, "draft.ask_user.answer", fact)
            == receipt
        )
        assert await store.load_draft_owner_fact(draft_id, key) == (receipt, fact)
        assert await store.list_draft_owner_facts(draft_id) == ((receipt, fact),)
        with pytest.raises(StoreCommitError, match="replay mismatch"):
            await store.commit_draft_owner_fact(
                draft_id,
                key,
                "draft.ask_user.answer",
                {"question_id": "question-1", "selected_option": "merge"},
            )
