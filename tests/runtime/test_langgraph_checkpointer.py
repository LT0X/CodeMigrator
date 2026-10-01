from __future__ import annotations

from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from codemigrator.runtime.cas import CasIntegrityError, FileHostCAS
from codemigrator.runtime.checkpointer import CasCheckpointSaver
from codemigrator.runtime.store import InMemoryRuntimeStore, StoreCommitError


def config(thread_id: str, checkpoint_id: str | None = None, namespace: str = "") -> dict:
    values = {"thread_id": thread_id, "checkpoint_ns": namespace}
    if checkpoint_id is not None:
        values["checkpoint_id"] = checkpoint_id
    return {"configurable": values}


def checkpoint(checkpoint_id: str, value: object) -> dict:
    return {
        "v": 4,
        "ts": "2026-09-29T00:00:00+00:00",
        "id": checkpoint_id,
        "channel_values": {"value": value},
        "channel_versions": {"value": 1},
        "versions_seen": {},
    }


def saver(tmp_path: Path, store: InMemoryRuntimeStore, owner_id=None) -> CasCheckpointSaver:
    return CasCheckpointSaver(
        FileHostCAS(tmp_path),
        store,
        graph_family="run",
        owner_kind="run",
        owner_id=owner_id or uuid4(),
    )


@pytest.mark.asyncio
async def test_checkpoint_and_pending_writes_survive_saver_restart(tmp_path: Path):
    store = InMemoryRuntimeStore()
    owner_id = uuid4()
    first = saver(tmp_path, store, owner_id)
    thread = str(uuid4())
    one, two = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    saved_one = await first.aput(
        config(thread),
        checkpoint(one, {"secret": "body"}),
        {"source": "input", "step": 0},
        {"value": 1},
    )
    await first.aput_writes(saved_one, [("result", {"raw": "tool body"})], "task-1")
    saved_two = await first.aput(
        saved_one, checkpoint(two, "final"), {"source": "loop", "step": 1}, {"value": 2}
    )
    reopened = saver(tmp_path, store, owner_id)
    latest = await reopened.aget_tuple(config(thread))
    indexes = await store.list_checkpoint_indexes(thread)
    first_digest = next(item.object.digest for item in indexes if item.checkpoint_id == one)
    latest_digest = next(item.object.digest for item in indexes if item.checkpoint_id == two)
    assert await reopened.verify_checkpoint(thread, first_digest)
    assert await reopened.verify_checkpoint(thread, latest_digest)
    assert not await reopened.verify_checkpoint(thread, "f" * 64)
    assert latest.checkpoint["id"] == two
    assert latest.parent_config["configurable"]["checkpoint_id"] == one
    previous = await reopened.aget_tuple(saved_one)
    assert previous.checkpoint["channel_values"]["value"] == {"secret": "body"}
    assert previous.pending_writes == [("task-1", "result", {"raw": "tool body"})]
    assert [item.checkpoint["id"] async for item in reopened.alist(config(thread))] == [two, one]
    assert saved_two["configurable"]["checkpoint_id"] == two


@pytest.mark.asyncio
async def test_owner_scoped_saver_cannot_delete_another_owners_thread(tmp_path: Path):
    store = InMemoryRuntimeStore()
    cas = FileHostCAS(tmp_path)
    owner_a, owner_b = uuid4(), uuid4()
    saver_a = CasCheckpointSaver(
        cas, store, graph_family="agent", owner_kind="run", owner_id=owner_a
    )
    saver_b = CasCheckpointSaver(
        cas, store, graph_family="agent", owner_kind="run", owner_id=owner_b
    )
    thread = str(uuid4())
    await saver_a.aput(
        config(thread), checkpoint(str(uuid4()), "owner-a"), {"source": "input"}, {}
    )

    with pytest.raises(ValueError, match="checkpoint owner identity mismatch"):
        await saver_b.adelete_thread(thread)

    restored = await saver_a.aget_tuple(config(thread))
    assert restored is not None
    assert restored.checkpoint["channel_values"]["value"] == "owner-a"


@pytest.mark.asyncio
async def test_namespace_isolation_filter_before_and_delete(tmp_path: Path):
    store = InMemoryRuntimeStore()
    instance = saver(tmp_path, store)
    thread = str(uuid4())
    one, two = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    await instance.aput(
        config(thread, namespace="child"),
        checkpoint(one, "child"),
        {"source": "input", "step": 0},
        {},
    )
    await instance.aput(config(thread), checkpoint(two, "root"), {"source": "loop", "step": 1}, {})
    assert (await instance.aget_tuple(config(thread))).checkpoint["id"] == two
    assert (await instance.aget_tuple(config(thread, namespace="child"))).checkpoint["id"] == one
    assert [
        item.checkpoint["id"]
        async for item in instance.alist(config(thread), filter={"source": "loop"})
    ] == [two]
    assert [
        item.checkpoint["id"]
        async for item in instance.alist(config(thread), before=config(thread, two))
    ] == []
    await instance.adelete_thread(thread)
    assert await instance.aget_tuple(config(thread)) is None
    assert await instance.aget_tuple(config(thread, namespace="child")) is None


@pytest.mark.asyncio
async def test_corrupt_checkpoint_is_rejected_before_deserialization(tmp_path: Path):
    store = InMemoryRuntimeStore()
    instance = saver(tmp_path, store)
    thread = str(uuid4())
    saved = await instance.aput(
        config(thread), checkpoint(str(uuid4()), "safe"), {"source": "input", "step": 0}, {}
    )
    digest = (await store.list_checkpoint_indexes(thread))[0].object.digest
    instance.cas.path_for(digest).write_bytes(b"corrupt")
    with pytest.raises(CasIntegrityError):
        await instance.aget_tuple(saved)
    with pytest.raises(CasIntegrityError):
        await instance.verify_checkpoint(thread, digest)


class CounterState(TypedDict):
    count: int


@pytest.mark.asyncio
async def test_real_langgraph_resumes_from_reopened_cas_saver(tmp_path: Path):
    store = InMemoryRuntimeStore()
    owner = uuid4()
    thread = str(uuid4())
    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    first_graph = builder.compile(checkpointer=saver(tmp_path, store, owner))
    assert (await first_graph.ainvoke({"count": 0}, config(thread)))["count"] == 1
    restarted_graph = builder.compile(checkpointer=saver(tmp_path, store, owner))
    assert (await restarted_graph.aget_state(config(thread))).values["count"] == 1
    assert (await restarted_graph.ainvoke({"count": 4}, config(thread)))["count"] == 5


@pytest.mark.asyncio
async def test_pending_write_freezes_thread_owner_before_checkpoint(tmp_path: Path):
    store = InMemoryRuntimeStore()
    thread = str(uuid4())
    checkpoint_id = str(uuid4())
    first = saver(tmp_path, store, uuid4())
    await first.aput_writes(config(thread, checkpoint_id), [("result", "private")], "task-1")
    second = saver(tmp_path, store, uuid4())
    with pytest.raises(StoreCommitError, match="another owner"):
        await second.aput(config(thread), checkpoint(checkpoint_id, "intruder"), {}, {})
