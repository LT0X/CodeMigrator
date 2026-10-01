"""Async LangGraph checkpointer with opaque CAS bodies and indexed metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from codemigrator.core.models.plan import PlanProposal

from .cas import CasObject, CheckpointIndex, FileHostCAS, PendingWriteIndex


class CheckpointIndexStore(Protocol):
    async def publish_checkpoint_index(self, index: CheckpointIndex) -> None: ...
    async def list_checkpoint_indexes(
        self, thread_id: str | None = None, namespace: str | None = None
    ) -> tuple[CheckpointIndex, ...]: ...
    async def publish_pending_write_index(self, index: PendingWriteIndex) -> None: ...
    async def list_pending_write_indexes(
        self, thread_id: str, namespace: str, checkpoint_id: str
    ) -> tuple[PendingWriteIndex, ...]: ...
    async def delete_checkpoint_thread(
        self,
        thread_id: str,
        *,
        graph_family: str,
        owner_kind: str,
        owner_id: UUID,
    ) -> tuple[CasObject, ...]: ...
    async def referenced_digests(self) -> frozenset[str]: ...


def _config_parts(config: RunnableConfig) -> tuple[str, str, str | None]:
    configurable = config.get("configurable") or {}
    thread_id = configurable.get("thread_id")
    if not isinstance(thread_id, str):
        raise ValueError("checkpoint config requires thread_id")
    try:
        UUID(thread_id)
    except ValueError as exc:
        raise ValueError("checkpoint thread_id must be UUID") from exc
    namespace = configurable.get("checkpoint_ns", "")
    checkpoint_id = configurable.get("checkpoint_id")
    if not isinstance(namespace, str) or (
        checkpoint_id is not None and not isinstance(checkpoint_id, str)
    ):
        raise ValueError("checkpoint namespace and ID must be text")
    return thread_id, namespace, checkpoint_id


def _saved_config(thread_id: str, namespace: str, checkpoint_id: str) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": namespace,
            "checkpoint_id": checkpoint_id,
        }
    }


class CasCheckpointSaver(BaseCheckpointSaver[str]):
    """Persist complete versioned checkpoint and write bodies in FileHostCAS."""

    def __init__(
        self,
        cas: FileHostCAS,
        store: CheckpointIndexStore,
        *,
        graph_family: str,
        owner_kind: str,
        owner_id: UUID,
    ) -> None:
        # An empty module allowlist rejects untrusted Python object imports on read.
        super().__init__(
            serde=JsonPlusSerializer(
                pickle_fallback=False,
                # PLAN's structured AgentState result is a trusted core contract.
                # Everything else remains blocked unless explicitly added here.
                allowed_msgpack_modules=[PlanProposal],
            )
        )
        if graph_family not in {"run", "draft", "agent"}:
            raise ValueError("unknown graph family")
        if owner_kind not in {"run", "draft"} or not isinstance(owner_id, UUID):
            raise ValueError("invalid checkpoint owner")
        self.cas = cas
        self.store = store
        self.graph_family = graph_family
        self.owner_kind = owner_kind
        self.owner_id = owner_id

    def for_owner(self, *, owner_kind: str, owner_id: UUID) -> CasCheckpointSaver:
        """Create an isolated saver bound to one graph owner's CAS references."""

        if self.graph_family == "run" and owner_kind != "run":
            raise ValueError("Run graph checkpoints require a Run owner")
        if self.graph_family == "draft" and owner_kind != "draft":
            raise ValueError("Draft graph checkpoints require a Draft owner")
        return CasCheckpointSaver(
            self.cas,
            self.store,
            graph_family=self.graph_family,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )

    def _encode(self, value: Any) -> bytes:
        type_name, payload = self.serde.dumps_typed(value)
        header = json.dumps({"version": 1, "type": type_name}, separators=(",", ":"))
        return header.encode("ascii") + b"\n" + payload

    def _decode(self, body: bytes) -> Any:
        header_bytes, separator, payload = body.partition(b"\n")
        if not separator or len(header_bytes) > 128:
            raise ValueError("invalid checkpoint body envelope")
        header = json.loads(header_bytes)
        if header.get("version") != 1 or not isinstance(header.get("type"), str):
            raise ValueError("unsupported checkpoint body version")
        return self.serde.loads_typed((header["type"], payload))

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id, namespace, parent_id = _config_parts(config)
        checkpoint_id = checkpoint["id"]
        if not isinstance(checkpoint_id, str):
            raise ValueError("checkpoint ID must be text")
        body = self._encode((checkpoint, get_checkpoint_metadata(config, metadata)))
        async with self.cas.lock(hashlib.sha256(body).hexdigest()):
            object_ref = await asyncio.to_thread(self.cas.put, body)
            await self.store.publish_checkpoint_index(
                CheckpointIndex(
                    self.graph_family,
                    self.owner_kind,
                    self.owner_id,
                    thread_id,
                    namespace,
                    checkpoint_id,
                    parent_id,
                    object_ref,
                )
            )
        return _saved_config(thread_id, namespace, checkpoint_id)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id, namespace, checkpoint_id = _config_parts(config)
        if checkpoint_id is None:
            raise ValueError("pending writes require checkpoint ID")
        for position, (channel, value) in enumerate(writes):
            write_index = WRITES_IDX_MAP.get(channel, position)
            body = self._encode((task_path, channel, value))
            async with self.cas.lock(hashlib.sha256(body).hexdigest()):
                object_ref = await asyncio.to_thread(self.cas.put, body)
                await self.store.publish_pending_write_index(
                    PendingWriteIndex(
                        self.graph_family,
                        self.owner_kind,
                        self.owner_id,
                        thread_id,
                        namespace,
                        checkpoint_id,
                        task_id,
                        write_index,
                        object_ref,
                    )
                )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id, namespace, checkpoint_id = _config_parts(config)
        indexes = await self.store.list_checkpoint_indexes(thread_id, namespace)
        index = (
            next((item for item in indexes if item.checkpoint_id == checkpoint_id), None)
            if checkpoint_id is not None
            else next(iter(indexes), None)
        )
        if index is None:
            return None
        if (index.owner_kind, index.owner_id, index.graph_family) != (
            self.owner_kind,
            self.owner_id,
            self.graph_family,
        ):
            raise ValueError("checkpoint owner identity mismatch")
        checkpoint, metadata = self._decode(await asyncio.to_thread(self.cas.read, index.object))
        writes = await self.store.list_pending_write_indexes(
            thread_id, namespace, index.checkpoint_id
        )
        pending = []
        for write in writes:
            _, channel, value = self._decode(await asyncio.to_thread(self.cas.read, write.object))
            pending.append((write.task_id, channel, value))
        return CheckpointTuple(
            config=_saved_config(thread_id, namespace, index.checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=(
                _saved_config(thread_id, namespace, index.parent_checkpoint_id)
                if index.parent_checkpoint_id is not None
                else None
            ),
            pending_writes=pending,
        )

    async def verify_checkpoint(self, thread_id: str, digest: str) -> bool:
        """Validate the referenced checkpoint body and every pending-write CAS body."""

        indexes = await self.store.list_checkpoint_indexes(thread_id, None)
        for index in indexes:
            if index.object.digest != digest:
                continue
            checkpoint = await self.aget_tuple(
                _saved_config(thread_id, index.namespace, index.checkpoint_id)
            )
            return checkpoint is not None
        return False

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if limit is not None and limit <= 0:
            return
        if config is None:
            thread_id = namespace = selected_id = None
        else:
            thread_id, namespace, selected_id = _config_parts(config)
        before_id = _config_parts(before)[2] if before is not None else None
        indexes = await self.store.list_checkpoint_indexes(thread_id, namespace)
        emitted = 0
        for index in indexes:
            if (index.owner_kind, index.owner_id, index.graph_family) != (
                self.owner_kind,
                self.owner_id,
                self.graph_family,
            ):
                continue
            if selected_id is not None and index.checkpoint_id != selected_id:
                continue
            if before_id is not None and index.checkpoint_id >= before_id:
                continue
            item = await self.aget_tuple(
                _saved_config(index.thread_id, index.namespace, index.checkpoint_id)
            )
            if (
                item is None
                or filter
                and any(item.metadata.get(key) != value for key, value in filter.items())
            ):
                continue
            yield item
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    async def adelete_thread(self, thread_id: str) -> None:
        _config_parts(_saved_config(thread_id, "", ""))
        candidates = await self.store.delete_checkpoint_thread(
            thread_id,
            graph_family=self.graph_family,
            owner_kind=self.owner_kind,
            owner_id=self.owner_id,
        )
        for item in candidates:
            async with self.cas.lock(item.digest):
                if item.digest not in await self.store.referenced_digests():
                    await asyncio.to_thread(self.cas.delete, item.digest)


__all__ = ["CasCheckpointSaver"]
