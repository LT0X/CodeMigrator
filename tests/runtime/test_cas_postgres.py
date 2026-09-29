from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from codemigrator.runtime.cas import CasLedger, FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.store import PostgreSQLRuntimeStore, StoreCommitError

from .test_agent_runs_postgres import isolated_store
from .test_langgraph_checkpointer import checkpoint, config


@pytest.mark.asyncio
async def test_postgres_cas_multi_owner_refs_and_last_release(tmp_path: Path):
    async with isolated_store() as store:
        cas = FileHostCAS(tmp_path)
        ledger = CasLedger(cas, store)
        one, two = uuid4(), uuid4()
        ref = await ledger.put(b"shared private body", "draft", one, "draft:1")
        assert await ledger.put(b"shared private body", "draft", one, "draft:1") == ref
        assert await ledger.put(b"shared private body", "run", two, "run:1") == ref
        assert not await ledger.release("draft", one, "draft:1")
        assert cas.read(ref) == b"shared private body"
        assert await ledger.release("run", two, "run:1")
        assert not cas.path_for(ref.digest).exists()


@pytest.mark.asyncio
async def test_postgres_checkpoint_restart_indexes_and_orphan_recovery(tmp_path: Path):
    async with isolated_store() as store:
        cas = FileHostCAS(tmp_path)
        owner = uuid4()
        thread = str(uuid4())
        checkpoint_id = "00000000-0000-0000-0000-000000000001"
        saver = CasCheckpointSaver(cas, store, graph_family="run", owner_kind="run", owner_id=owner)
        saved = await saver.aput(
            config(thread),
            checkpoint(checkpoint_id, "private body"),
            {"source": "input", "step": 0},
            {"value": 1},
        )
        await saver.aput_writes(saved, [("result", {"tool": "private"})], "task-1")
        reopened = CasCheckpointSaver(
            cas,
            PostgreSQLRuntimeStore(store.pool),
            graph_family="run",
            owner_kind="run",
            owner_id=owner,
        )
        restored = await reopened.aget_tuple(config(thread))
        assert restored.checkpoint["channel_values"]["value"] == "private body"
        assert restored.pending_writes == [("task-1", "result", {"tool": "private"})]
        indexes = await store.list_checkpoint_indexes(thread)
        assert indexes[0].object.digest != "private body"
        with pytest.raises(StoreCommitError, match="reference key"):
            await saver.aput(
                config(thread),
                checkpoint(checkpoint_id, "different body"),
                {"source": "input", "step": 0},
                {"value": 1},
            )
        assert (await reopened.aget_tuple(saved)).checkpoint["channel_values"][
            "value"
        ] == "private body"
        assert await CasLedger(cas, store).collect_orphans(min_age_seconds=0) == 1
        await reopened.adelete_thread(thread)
        assert await reopened.aget_tuple(config(thread)) is None
        assert list(cas.iter_objects()) == []
