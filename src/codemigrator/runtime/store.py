"""Runtime persistence ports and a deterministic transactional test adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from codemigrator.core import (
    ActiveDispatch,
    CreateRun,
    FailureReason,
    Phase,
    RunId,
    RunStatus,
    SecretRegistry,
    SessionKind,
    SliceGenerationRef,
    SliceId,
    canonical_json_bytes,
)

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .budget import BudgetUsage
from .cas import CasObject, CheckpointIndex, PendingWriteIndex
from .contracts import (
    CandidateCheckpointFact,
    DraftOwnerReceipt,
    DraftSessionEvent,
    DraftSessionEventSpec,
    EventSpec,
    RunState,
    RuntimeEvent,
    RuntimeSnapshot,
)
from .loop_contracts import SessionExit, SessionState
from .memory import EvolutionSegment, EvolutionSegmentDraft
from .schema import RUNTIME_SCHEMA_SQL

_RUN_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.Completed.value,
        RunStatus.PartiallyCompleted.value,
        RunStatus.Failed.value,
        RunStatus.Cancelled.value,
    }
)
_DRAFT_TERMINAL_EVENTS = frozenset({"session.closed", "session.attached_to_run"})


class RuntimeStore(Protocol):
    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        """Load one Run and its append-only events."""

    async def create(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        """Insert a new Run atomically with its first events."""

    async def commit(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        """Commit state and events in one transaction."""

    async def read_run_events(self, run_id: RunId, after_sequence: int) -> tuple[RuntimeEvent, ...]:
        """Read committed Run events strictly after a sequence cursor."""

    async def wait_for_run_events(self, run_id: RunId, after_sequence: int) -> None:
        """Wait for a wake-up and recheck the durable Run event ledger."""

    async def is_run_stream_terminal(self, run_id: RunId, after_sequence: int) -> bool:
        """Report whether a terminal Run event at or before the cursor is committed."""

    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun:
        """Create one session per owner logical task, or return its frozen identity."""

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Load private session metadata."""

    async def list_agent_runs_by_owner(
        self, owner_kind: str, owner_id: UUID
    ) -> tuple[AgentRun, ...]:
        """Load private session metadata for one owner during retention cleanup."""

    async def commit_draft_owner_fact(
        self,
        draft_id: UUID,
        receipt_key: str,
        category: str,
        fact: Mapping[str, object],
        *,
        events: Sequence[DraftSessionEventSpec] = (),
    ) -> DraftOwnerReceipt:
        """Persist an immutable Draft fact and its idempotency receipt."""

    async def load_draft_owner_fact(
        self, draft_id: UUID, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None: ...

    async def list_draft_owner_facts(
        self, draft_id: UUID
    ) -> tuple[tuple[DraftOwnerReceipt, dict[str, object]], ...]: ...

    async def read_draft_session_events(
        self, draft_id: UUID, after_sequence: int
    ) -> tuple[DraftSessionEvent, ...]: ...

    async def wait_for_draft_session_events(self, draft_id: UUID, after_sequence: int) -> None: ...

    async def is_draft_session_terminal(self, draft_id: UUID, after_sequence: int) -> bool: ...

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None:
        """Load the committed owner receipt, if any."""

    async def get_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None:
        """Load a durable owner reference to an opaque CAS object."""

    async def commit_agent_run_receipt(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        *,
        state: RunState | None = None,
        events: Sequence[EventSpec] = (),
        cas_references: Sequence[tuple[str, CasObject]] = (),
    ) -> AgentRunReceipt:
        """Commit terminal metadata and owner facts/events in one transaction."""


class StoreCommitError(RuntimeError):
    """Raised when the persistence transaction cannot be committed."""


def _validate_new_agent_run(record: AgentRun) -> None:
    if record.state is not SessionState.Created or record.exit is not None:
        raise StoreCommitError("new AgentRun must be created without an exit")
    if any(
        getattr(record, name) is not None
        for name in (
            "checkpoint_sha256",
            "candidate_checkpoint_sha256",
            "result_sha256",
            "usage_sha256",
        )
    ):
        raise StoreCommitError("new AgentRun cannot have terminal references")


def _validate_agent_receipt(
    current: AgentRun | None,
    record: AgentRun,
    receipt: AgentRunReceipt,
    state: RunState | None,
    events: Sequence[EventSpec],
    cas_references: Sequence[tuple[str, CasObject]] = (),
) -> None:
    if current is None:
        raise StoreCommitError("AgentRun does not exist")
    if not current.same_frozen_identity(record):
        raise StoreCommitError("AgentRun frozen identity mismatch")
    if current.is_terminal or not record.is_terminal:
        raise StoreCommitError("AgentRun receipt requires a new terminal state")
    if receipt.agent_run_id != record.agent_run_id:
        raise StoreCommitError("AgentRun receipt identity mismatch")
    if record.owner_kind == "run":
        if state is None or state.run_id != record.owner_id:
            raise StoreCommitError("Run owner receipt requires matching Run state")
    elif state is not None or events:
        raise StoreCommitError("Draft owner cannot write Run facts or events")
    if record.owner_kind != "run" and cas_references:
        raise StoreCommitError("Run receipt CAS refs require a Run owner")
    if any(
        not reference_key
        or len(reference_key) > 256
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-"
            for character in reference_key
        )
        for reference_key, _ in cas_references
    ):
        raise StoreCommitError("CAS reference key is invalid")


def _validate_evolution_draft(draft: EvolutionSegmentDraft, expected_run_id: object) -> None:
    if draft.run_id != expected_run_id:
        raise StoreCommitError("context evolution run identity does not match state")
    if not isinstance(draft.summary_text, str) or not draft.summary_text.strip():
        raise StoreCommitError("context evolution summary must be non-empty")
    if len(draft.template_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in draft.template_sha256
    ):
        raise StoreCommitError("context evolution template digest is invalid")


def _next_in_memory_evolution(
    draft: EvolutionSegmentDraft | None,
    run_id: object,
    entries: Mapping[object, list[EvolutionSegment]],
) -> EvolutionSegment | None:
    if draft is None:
        return None
    _validate_evolution_draft(draft, run_id)
    previous = entries.get(run_id, ())
    if previous and previous[0].template_sha256 != draft.template_sha256:
        raise StoreCommitError("context evolution template is frozen per Run")
    if any(entry.slice_id == draft.slice_id for entry in previous):
        raise StoreCommitError("context evolution slice has already been appended")
    return EvolutionSegment(
        run_id=run_id,
        entry_index=len(previous),
        slice_id=draft.slice_id,
        summary_text=draft.summary_text,
        template_sha256=draft.template_sha256,
    )


class InMemoryRuntimeStore:
    """A transactionally behaving store double for actor and contract tests."""

    def __init__(self, *, secret_registry: SecretRegistry | None = None) -> None:
        self._snapshots: dict[RunId, RuntimeSnapshot] = {}
        self._evolution: dict[object, list[EvolutionSegment]] = {}
        self.commit_count = 0
        self._fail_next = False
        self.secret_registry = secret_registry or SecretRegistry()
        self._run_conditions: dict[RunId, asyncio.Condition] = {}
        self._agent_runs: dict[AgentRunId, AgentRun] = {}
        self._agent_run_keys: dict[tuple[str, UUID, str], AgentRunId] = {}
        self._agent_threads: dict[UUID, AgentRunId] = {}
        self._agent_receipts: dict[AgentRunId, AgentRunReceipt] = {}
        self._agent_lock = asyncio.Lock()
        self._draft_facts: dict[tuple[UUID, str], tuple[DraftOwnerReceipt, dict[str, object]]] = {}
        self._draft_events: dict[UUID, list[DraftSessionEvent]] = {}
        self._draft_event_specs: dict[
            tuple[UUID, str], tuple[tuple[str, dict[str, object]], ...]
        ] = {}
        self._draft_conditions: dict[UUID, asyncio.Condition] = {}
        self._draft_terminals: dict[UUID, int] = {}
        self._cas_refs: dict[tuple[str, UUID, str], CasObject] = {}
        self._checkpoints: dict[tuple[str, str, str], CheckpointIndex] = {}
        self._pending_writes: dict[tuple[str, str, str, str, int], PendingWriteIndex] = {}

    async def add_cas_reference(
        self, object_ref: CasObject, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> None:
        async with self._agent_lock:
            if self._fail_next:
                self._fail_next = False
                raise StoreCommitError("injected commit failure")
            key = (owner_kind, owner_id, reference_key)
            previous = self._cas_refs.get(key)
            if previous is not None and previous != object_ref:
                raise StoreCommitError("CAS reference key already points to another object")
            if any(
                value.digest == object_ref.digest and value.size != object_ref.size
                for value in self._cas_refs.values()
            ):
                raise StoreCommitError("CAS object size mismatch")
            self._cas_refs[key] = object_ref

    async def get_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None:
        return self._cas_refs.get((owner_kind, owner_id, reference_key))

    async def commit_draft_owner_fact(
        self,
        draft_id: UUID,
        receipt_key: str,
        category: str,
        fact: Mapping[str, object],
        *,
        events: Sequence[DraftSessionEventSpec] = (),
    ) -> DraftOwnerReceipt:
        receipt, payload = _make_draft_receipt(draft_id, receipt_key, category, fact)
        event_data = _prepare_draft_events(events, self.secret_registry)
        async with self._agent_lock:
            key = (draft_id, receipt_key)
            previous = self._draft_facts.get(key)
            if previous is not None:
                if (
                    previous[0] != receipt
                    or previous[1] != payload
                    or self._draft_event_specs[key] != event_data
                ):
                    raise StoreCommitError("Draft owner fact replay mismatch")
                return previous[0]
            if self._fail_next:
                self._fail_next = False
                raise StoreCommitError("injected commit failure")
            self._draft_facts[key] = (receipt, payload)
            self._draft_event_specs[key] = event_data
            ledger = self._draft_events.setdefault(draft_id, [])
            first_sequence = len(ledger) + 1
            appended = tuple(
                DraftSessionEvent(
                    draft_id, first_sequence + index, event_type, data, datetime.now(UTC)
                )
                for index, (event_type, data) in enumerate(event_data)
            )
            ledger.extend(appended)
            for event in appended:
                if event.event_type in _DRAFT_TERMINAL_EVENTS:
                    terminal = self._draft_terminals.get(draft_id)
                    if terminal is None or event.sequence < terminal:
                        self._draft_terminals[draft_id] = event.sequence
            condition = self._draft_conditions.setdefault(draft_id, asyncio.Condition())
            async with condition:
                if event_data:
                    condition.notify_all()
            return receipt

    async def read_draft_session_events(
        self, draft_id: UUID, after_sequence: int
    ) -> tuple[DraftSessionEvent, ...]:
        return tuple(
            DraftSessionEvent(
                event.draft_id,
                event.sequence,
                event.event_type,
                dict(event.data),
                event.timestamp_utc,
            )
            for event in self._draft_events.get(draft_id, ())
            if event.sequence > after_sequence
        )

    async def wait_for_draft_session_events(self, draft_id: UUID, after_sequence: int) -> None:
        condition = self._draft_conditions.setdefault(draft_id, asyncio.Condition())
        async with condition:
            await condition.wait_for(
                lambda: (
                    len(self._draft_events.get(draft_id, ())) > after_sequence
                    or (
                        self._draft_terminals.get(draft_id) is not None
                        and self._draft_terminals[draft_id] <= after_sequence
                    )
                )
            )

    async def is_draft_session_terminal(self, draft_id: UUID, after_sequence: int) -> bool:
        terminal_sequence = self._draft_terminals.get(draft_id)
        return terminal_sequence is not None and terminal_sequence <= after_sequence

    async def read_run_events(self, run_id: RunId, after_sequence: int) -> tuple[RuntimeEvent, ...]:
        snapshot = self._snapshots.get(run_id)
        if snapshot is None:
            return ()
        return tuple(
            RuntimeEvent(
                sequence=event.sequence,
                event_type=event.event_type,
                data=dict(event.data),
                timestamp_utc=event.timestamp_utc,
            )
            for event in snapshot.events
            if event.sequence > after_sequence
        )

    async def wait_for_run_events(self, run_id: RunId, after_sequence: int) -> None:
        condition = self._run_conditions.setdefault(run_id, asyncio.Condition())
        async with condition:
            await condition.wait_for(
                lambda: (
                    self._has_run_events_after(run_id, after_sequence)
                    or self._run_terminal_reached(run_id, after_sequence)
                )
            )

    async def is_run_stream_terminal(self, run_id: RunId, after_sequence: int) -> bool:
        return self._run_terminal_reached(run_id, after_sequence)

    def _has_run_events_after(self, run_id: RunId, after_sequence: int) -> bool:
        snapshot = self._snapshots.get(run_id)
        return snapshot is not None and any(
            event.sequence > after_sequence for event in snapshot.events
        )

    def _run_terminal_reached(self, run_id: RunId, after_sequence: int) -> bool:
        snapshot = self._snapshots.get(run_id)
        return snapshot is not None and any(
            event.sequence <= after_sequence
            and event.event_type == "run.status_changed"
            and _event_status(event.data) in _RUN_TERMINAL_STATUSES
            for event in snapshot.events
        )

    async def load_draft_owner_fact(
        self, draft_id: UUID, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None:
        value = self._draft_facts.get((draft_id, receipt_key))
        if value is None:
            return None
        receipt, fact = value
        _verify_draft_fact_digest(receipt, fact)
        return receipt, _decode_draft_fact(canonical_json_bytes(fact))

    async def list_draft_owner_facts(
        self, draft_id: UUID
    ) -> tuple[tuple[DraftOwnerReceipt, dict[str, object]], ...]:
        values = tuple(
            value
            for (owner_id, _), value in sorted(
                self._draft_facts.items(), key=lambda item: item[0][1]
            )
            if owner_id == draft_id
        )
        for receipt, fact in values:
            _verify_draft_fact_digest(receipt, fact)
        return tuple(
            (receipt, _decode_draft_fact(canonical_json_bytes(fact))) for receipt, fact in values
        )

    async def release_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None:
        async with self._agent_lock:
            object_ref = self._cas_refs.pop((owner_kind, owner_id, reference_key), None)
            if object_ref is None or any(
                value.digest == object_ref.digest for value in self._cas_refs.values()
            ):
                return None
            return object_ref

    async def referenced_digests(self) -> frozenset[str]:
        return frozenset(value.digest for value in self._cas_refs.values())

    async def publish_checkpoint_index(self, index: CheckpointIndex) -> None:
        key = (index.thread_id, index.namespace, index.checkpoint_id)
        async with self._agent_lock:
            if self._fail_next:
                self._fail_next = False
                raise StoreCommitError("injected commit failure")
            existing = self._checkpoints.get(key)
            if existing is not None:
                if existing != index:
                    raise StoreCommitError("checkpoint identity collision")
                return
            if any(
                item.thread_id == index.thread_id
                and (item.owner_kind, item.owner_id, item.graph_family)
                != (index.owner_kind, index.owner_id, index.graph_family)
                for item in self._checkpoints.values()
            ) or any(
                item.thread_id == index.thread_id
                and (item.owner_kind, item.owner_id, item.graph_family)
                != (index.owner_kind, index.owner_id, index.graph_family)
                for item in self._pending_writes.values()
            ):
                raise StoreCommitError("checkpoint thread belongs to another owner")
            self._checkpoints[key] = index
            self._cas_refs[(index.owner_kind, index.owner_id, index.reference_key)] = index.object

    async def list_checkpoint_indexes(
        self, thread_id: str | None = None, namespace: str | None = None
    ) -> tuple[CheckpointIndex, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._checkpoints.values()
                    if (thread_id is None or item.thread_id == thread_id)
                    and (namespace is None or item.namespace == namespace)
                ),
                key=lambda item: item.checkpoint_id,
                reverse=True,
            )
        )

    async def publish_pending_write_index(self, index: PendingWriteIndex) -> None:
        write_key = (
            index.thread_id,
            index.namespace,
            index.checkpoint_id,
            index.task_id,
            index.write_index,
        )
        async with self._agent_lock:
            owner_mismatch = any(
                item.thread_id == index.thread_id
                and (item.owner_kind, item.owner_id) != (index.owner_kind, index.owner_id)
                for item in self._checkpoints.values()
            ) or any(
                item.thread_id == index.thread_id
                and (item.owner_kind, item.owner_id) != (index.owner_kind, index.owner_id)
                for item in self._pending_writes.values()
            )
            if owner_mismatch:
                raise StoreCommitError("pending write thread belongs to another owner")
            existing = self._pending_writes.get(write_key)
            if existing is not None and index.write_index >= 0:
                return
            if self._fail_next:
                self._fail_next = False
                raise StoreCommitError("injected commit failure")
            if existing is not None:
                self._cas_refs.pop((existing.owner_kind, existing.owner_id, existing.reference_key))
            self._pending_writes[write_key] = index
            self._cas_refs[(index.owner_kind, index.owner_id, index.reference_key)] = index.object

    async def list_pending_write_indexes(
        self, thread_id: str, namespace: str, checkpoint_id: str
    ) -> tuple[PendingWriteIndex, ...]:
        return tuple(
            item
            for key, item in self._pending_writes.items()
            if key[:3] == (thread_id, namespace, checkpoint_id)
        )

    async def delete_checkpoint_thread(self, thread_id: str) -> tuple[CasObject, ...]:
        async with self._agent_lock:
            indexes: list[CheckpointIndex | PendingWriteIndex] = []
            indexes.extend(
                item for item in self._checkpoints.values() if item.thread_id == thread_id
            )
            indexes.extend(
                item for item in self._pending_writes.values() if item.thread_id == thread_id
            )
            for item in indexes:
                self._cas_refs.pop((item.owner_kind, item.owner_id, item.reference_key), None)
            self._checkpoints = {
                key: item for key, item in self._checkpoints.items() if item.thread_id != thread_id
            }
            self._pending_writes = {
                key: item
                for key, item in self._pending_writes.items()
                if item.thread_id != thread_id
            }
            live = {value.digest for value in self._cas_refs.values()}
            return tuple(
                {
                    item.object.digest: item.object
                    for item in indexes
                    if item.object.digest not in live
                }.values()
            )

    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun:
        _validate_new_agent_run(record)
        async with self._agent_lock:
            if record.owner_kind == "run" and RunId(record.owner_id) not in self._snapshots:
                raise StoreCommitError("AgentRun owner Run does not exist")
            existing_id = self._agent_run_keys.get(record.owner_key)
            if existing_id is not None:
                existing = self._agent_runs[existing_id]
                if not existing.same_creation_identity(record):
                    raise StoreCommitError("AgentRun logical task identity mismatch")
                return existing
            if record.agent_run_id in self._agent_runs:
                raise StoreCommitError("AgentRun ID already exists")
            if UUID(record.thread_id) in self._agent_threads:
                raise StoreCommitError("AgentRun thread already exists")
            self._agent_runs[record.agent_run_id] = record
            self._agent_run_keys[record.owner_key] = record.agent_run_id
            self._agent_threads[UUID(record.thread_id)] = record.agent_run_id
            return record

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        return self._agent_runs.get(agent_run_id)

    async def list_agent_runs_by_owner(
        self, owner_kind: str, owner_id: UUID
    ) -> tuple[AgentRun, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._agent_runs.values()
                    if record.owner_kind == owner_kind and record.owner_id == owner_id
                ),
                key=lambda record: record.logical_task_key,
            )
        )

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None:
        return self._agent_receipts.get(agent_run_id)

    async def commit_agent_run_receipt(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        *,
        state: RunState | None = None,
        events: Sequence[EventSpec] = (),
        cas_references: Sequence[tuple[str, CasObject]] = (),
    ) -> AgentRunReceipt:
        async with self._agent_lock:
            existing_receipt = self._agent_receipts.get(record.agent_run_id)
            if existing_receipt is not None:
                if (
                    existing_receipt.category != receipt.category
                    or existing_receipt.agent_run_id != receipt.agent_run_id
                    or self._agent_runs[record.agent_run_id] != record
                    or any(
                        self._cas_refs.get((record.owner_kind, record.owner_id, key)) != obj
                        for key, obj in cas_references
                    )
                ):
                    raise StoreCommitError("AgentRun receipt replay mismatch")
                return existing_receipt
            current = self._agent_runs.get(record.agent_run_id)
            _validate_agent_receipt(current, record, receipt, state, events, cas_references)
            for reference_key, object_ref in cas_references:
                existing_ref = self._cas_refs.get(
                    (record.owner_kind, record.owner_id, reference_key)
                )
                if existing_ref is not None and existing_ref != object_ref:
                    raise StoreCommitError("CAS reference key already points to another object")
                if any(
                    value.digest == object_ref.digest and value.size != object_ref.size
                    for value in self._cas_refs.values()
                ):
                    raise StoreCommitError("CAS object size mismatch")
            if self._fail_next:
                self._fail_next = False
                raise StoreCommitError("injected commit failure")
            if state is not None:
                previous = self._snapshots.get(state.run_id)
                if previous is None:
                    raise StoreCommitError("runtime run does not exist")
                if state.version != previous.state.version + 1:
                    raise StoreCommitError("Run owner state version is stale")
                try:
                    materialized = tuple(
                        RuntimeEvent(
                            sequence=len(previous.events) + index + 1,
                            event_type=event.event_type,
                            data=_redact_event_data(event.data, self.secret_registry),
                        )
                        for index, event in enumerate(events)
                    )
                except ValueError as exc:
                    raise StoreCommitError("observation rejected") from exc
                snapshot = RuntimeSnapshot(state=state, events=(*previous.events, *materialized))
            else:
                snapshot = None
            if snapshot is not None:
                run_id = RunId(record.owner_id)
                condition = self._run_conditions.setdefault(run_id, asyncio.Condition())
                async with condition:
                    self._snapshots[run_id] = snapshot
                    if events:
                        condition.notify_all()
            self._agent_runs[record.agent_run_id] = record
            self._agent_receipts[record.agent_run_id] = receipt
            for reference_key, object_ref in cas_references:
                self._cas_refs[(record.owner_kind, record.owner_id, reference_key)] = object_ref
            self.commit_count += 1
            return receipt

    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        return self._snapshots.get(run_id)

    async def create(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        if state.run_id in self._snapshots:
            raise StoreCommitError("run already exists")
        return await self._write(state, events, evolution=evolution)

    async def commit(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        if state.run_id not in self._snapshots:
            raise StoreCommitError("run does not exist")
        return await self._write(state, events, evolution=evolution)

    async def snapshot(self, run_id: RunId) -> RuntimeSnapshot:
        snapshot = await self.load(run_id)
        if snapshot is None:
            raise KeyError(run_id)
        return snapshot

    def fail_next_commit(self) -> None:
        self._fail_next = True

    async def _write(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        if self._fail_next:
            self._fail_next = False
            raise StoreCommitError("injected commit failure")
        condition = self._run_conditions.setdefault(state.run_id, asyncio.Condition())
        async with condition:
            previous = self._snapshots.get(state.run_id)
            first_sequence = len(previous.events) + 1 if previous is not None else 1
            try:
                materialized = tuple(
                    RuntimeEvent(
                        sequence=first_sequence + index,
                        event_type=event.event_type,
                        data=_redact_event_data(event.data, self.secret_registry),
                    )
                    for index, event in enumerate(events)
                )
            except ValueError as exc:
                raise StoreCommitError("observation rejected") from exc
            evolution_entry = _next_in_memory_evolution(evolution, state.run_id, self._evolution)
            snapshot = RuntimeSnapshot(
                state=state,
                events=(*previous.events, *materialized) if previous else materialized,
            )
            self._snapshots[state.run_id] = snapshot
            if evolution_entry is not None:
                self._evolution.setdefault(state.run_id, []).append(evolution_entry)
            self.commit_count += 1
            if materialized:
                condition.notify_all()
            return snapshot

    async def evolution_segments(self, *, run_id: object) -> tuple[EvolutionSegment, ...]:
        return tuple(self._evolution.get(run_id, ()))


class PostgreSQLRuntimeStore:
    """Durable runtime store using one transaction for state and run events.

    The pool/connection object is deliberately accepted at the adapter boundary;
    runtime logic never imports or manages a database connection directly.
    """

    def __init__(self, pool: Any, *, secret_registry: SecretRegistry | None = None) -> None:
        self.pool = pool
        self.secret_registry = secret_registry or SecretRegistry()

    async def commit_draft_owner_fact(
        self,
        draft_id: UUID,
        receipt_key: str,
        category: str,
        fact: Mapping[str, object],
        *,
        events: Sequence[DraftSessionEventSpec] = (),
    ) -> DraftOwnerReceipt:
        receipt, payload = _make_draft_receipt(draft_id, receipt_key, category, fact)
        event_data = _prepare_draft_events(events, self.secret_registry)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))", str(draft_id)
                )
                inserted = await connection.fetchval(
                    """INSERT INTO draft_owner_facts(
                        draft_id, receipt_key, category, fact_sha256, fact
                    ) VALUES ($1,$2,$3,$4,$5::jsonb)
                    ON CONFLICT (draft_id, receipt_key) DO NOTHING
                    RETURNING receipt_key""",
                    draft_id,
                    receipt_key,
                    category,
                    receipt.fact_sha256,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                )
                row = await connection.fetchrow(
                    """SELECT category, fact_sha256, fact FROM draft_owner_facts
                    WHERE draft_id=$1 AND receipt_key=$2""",
                    draft_id,
                    receipt_key,
                )
                if row is None:
                    raise StoreCommitError("Draft owner fact commit disappeared")
                stored = _decode_draft_fact(_row_value(row, "fact"))
                stored_receipt = DraftOwnerReceipt(
                    draft_id,
                    receipt_key,
                    str(_row_value(row, "category")),
                    str(_row_value(row, "fact_sha256")).strip(),
                )
                if stored_receipt != receipt or stored != payload:
                    raise StoreCommitError("Draft owner fact replay mismatch")
                if inserted is not None:
                    first_sequence = await connection.fetchval(
                        """SELECT COALESCE(MAX(sequence), 0) + 1
                        FROM draft_session_events WHERE draft_id=$1""",
                        draft_id,
                    )
                    for index, (event_type, data) in enumerate(event_data):
                        await connection.execute(
                            """INSERT INTO draft_session_events
                            (draft_id, receipt_key, sequence, event_type, data)
                            VALUES ($1,$2,$3,$4,$5::jsonb)""",
                            draft_id,
                            receipt_key,
                            first_sequence + index,
                            event_type,
                            json.dumps(data, sort_keys=True, separators=(",", ":")),
                        )
                    if event_data:
                        await connection.execute(
                            "SELECT pg_notify('draft_session_events', $1)", str(draft_id)
                        )
                else:
                    replay_rows = await connection.fetch(
                        """SELECT event_type, data FROM draft_session_events
                        WHERE draft_id=$1 AND receipt_key=$2 ORDER BY sequence""",
                        draft_id,
                        receipt_key,
                    )
                    replay_events = tuple(
                        (
                            str(_row_value(row, "event_type")),
                            _decode_draft_fact(_row_value(row, "data")),
                        )
                        for row in replay_rows
                    )
                    if replay_events != event_data:
                        raise StoreCommitError("Draft owner fact replay mismatch")
                return stored_receipt

    async def read_draft_session_events(
        self, draft_id: UUID, after_sequence: int
    ) -> tuple[DraftSessionEvent, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT sequence, event_type, data, timestamp_utc FROM draft_session_events
                WHERE draft_id=$1 AND sequence>$2 ORDER BY sequence""",
                draft_id,
                after_sequence,
            )
        return tuple(
            DraftSessionEvent(
                draft_id,
                int(_row_value(row, "sequence")),
                str(_row_value(row, "event_type")),
                _decode_draft_fact(_row_value(row, "data")),
                _row_value(row, "timestamp_utc").astimezone(UTC),
            )
            for row in rows
        )

    async def wait_for_draft_session_events(self, draft_id: UUID, after_sequence: int) -> None:
        loop = asyncio.get_running_loop()
        wake = loop.create_future()

        def listener(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
            if payload == str(draft_id) and not wake.done():
                wake.set_result(None)

        async with self.pool.acquire() as connection:
            await connection.add_listener("draft_session_events", listener)
            try:
                row = await connection.fetchrow(
                    """SELECT COALESCE(MAX(sequence), 0) AS latest_sequence,
                    COALESCE(MIN(sequence) FILTER (WHERE event_type = ANY($2::text[])), 0)
                        AS terminal_sequence
                    FROM draft_session_events WHERE draft_id=$1""",
                    draft_id,
                    list(_DRAFT_TERMINAL_EVENTS),
                )
                latest = int(_row_value(row, "latest_sequence"))
                terminal = int(_row_value(row, "terminal_sequence"))
                if latest > after_sequence or (terminal > 0 and terminal <= after_sequence):
                    return
                await wake
            finally:
                await connection.remove_listener("draft_session_events", listener)

    async def is_draft_session_terminal(self, draft_id: UUID, after_sequence: int) -> bool:
        async with self.pool.acquire() as connection:
            terminal = await connection.fetchval(
                """SELECT COALESCE(MIN(sequence), 0)
                FROM draft_session_events
                WHERE draft_id=$1 AND sequence <= $2 AND event_type = ANY($3::text[])""",
                draft_id,
                after_sequence,
                list(_DRAFT_TERMINAL_EVENTS),
            )
            return int(terminal) > 0

    async def load_draft_owner_fact(
        self, draft_id: UUID, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT category, fact_sha256, fact FROM draft_owner_facts
                WHERE draft_id=$1 AND receipt_key=$2""",
                draft_id,
                receipt_key,
            )
        if row is None:
            return None
        receipt = DraftOwnerReceipt(
            draft_id,
            receipt_key,
            str(_row_value(row, "category")),
            str(_row_value(row, "fact_sha256")).strip(),
        )
        fact = _decode_draft_fact(_row_value(row, "fact"))
        _verify_draft_fact_digest(receipt, fact)
        return receipt, fact

    async def list_draft_owner_facts(
        self, draft_id: UUID
    ) -> tuple[tuple[DraftOwnerReceipt, dict[str, object]], ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT receipt_key, category, fact_sha256, fact
                FROM draft_owner_facts WHERE draft_id=$1 ORDER BY receipt_key""",
                draft_id,
            )
        values = tuple(
            (
                DraftOwnerReceipt(
                    draft_id,
                    str(_row_value(row, "receipt_key")),
                    str(_row_value(row, "category")),
                    str(_row_value(row, "fact_sha256")).strip(),
                ),
                _decode_draft_fact(_row_value(row, "fact")),
            )
            for row in rows
        )
        for receipt, fact in values:
            _verify_draft_fact_digest(receipt, fact)
        return values

    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun:
        _validate_new_agent_run(record)
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                if record.owner_kind == "run":
                    owner_row = await connection.fetchrow(
                        "SELECT run_id FROM runtime_runs WHERE run_id = $1",
                        record.owner_id,
                    )
                    if owner_row is None:
                        raise StoreCommitError("AgentRun owner Run does not exist")
                try:
                    await connection.execute(
                        """INSERT INTO agent_runs(
                            agent_run_id, owner_kind, owner_id, logical_task_key,
                            thread_id, phase, session_kind, model_binding_sha256,
                            context_sha256, toolset_sha256, template_sha256,
                            state, exit, metadata
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14::jsonb)
                        ON CONFLICT (owner_kind, owner_id, logical_task_key) DO NOTHING""",
                        record.agent_run_id,
                        record.owner_kind,
                        record.owner_id,
                        record.logical_task_key,
                        UUID(record.thread_id),
                        record.phase.value,
                        record.session_kind.value,
                        record.model_binding_sha256,
                        record.context_sha256,
                        record.toolset_sha256,
                        record.template_sha256,
                        record.state.value,
                        None,
                        _dump_agent_run(record),
                    )
                except Exception as exc:
                    if getattr(exc, "constraint_name", None) == "agent_runs_thread_id_unique":
                        raise StoreCommitError("AgentRun thread already exists") from exc
                    raise
                row = await connection.fetchrow(
                    """SELECT metadata FROM agent_runs
                    WHERE owner_kind = $1 AND owner_id = $2 AND logical_task_key = $3""",
                    *record.owner_key,
                )
                if row is None:
                    raise StoreCommitError("AgentRun creation disappeared")
                existing = _decode_agent_run(_row_value(row, "metadata"))
                if not existing.same_creation_identity(record):
                    raise StoreCommitError("AgentRun logical task identity mismatch")
                return existing

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT metadata FROM agent_runs WHERE agent_run_id = $1",
                agent_run_id,
            )
        return _decode_agent_run(_row_value(row, "metadata")) if row is not None else None

    async def list_agent_runs_by_owner(
        self, owner_kind: str, owner_id: UUID
    ) -> tuple[AgentRun, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT metadata FROM agent_runs
                WHERE owner_kind=$1 AND owner_id=$2 ORDER BY logical_task_key""",
                owner_kind,
                owner_id,
            )
        return tuple(_decode_agent_run(_row_value(row, "metadata")) for row in rows)

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT receipt_id, category FROM agent_run_receipts WHERE agent_run_id = $1",
                agent_run_id,
            )
        if row is None:
            return None
        return AgentRunReceipt(
            _row_value(row, "receipt_id"), agent_run_id, str(_row_value(row, "category"))
        )

    async def commit_agent_run_receipt(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        *,
        state: RunState | None = None,
        events: Sequence[EventSpec] = (),
        cas_references: Sequence[tuple[str, CasObject]] = (),
    ) -> AgentRunReceipt:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT metadata FROM agent_runs WHERE agent_run_id = $1 FOR UPDATE",
                    record.agent_run_id,
                )
                current = (
                    _decode_agent_run(_row_value(row, "metadata")) if row is not None else None
                )
                receipt_row = await connection.fetchrow(
                    "SELECT receipt_id, category FROM agent_run_receipts WHERE agent_run_id = $1",
                    record.agent_run_id,
                )
                if receipt_row is not None:
                    existing_receipt = AgentRunReceipt(
                        _row_value(receipt_row, "receipt_id"),
                        record.agent_run_id,
                        str(_row_value(receipt_row, "category")),
                    )
                    if (
                        existing_receipt.category != receipt.category
                        or existing_receipt.agent_run_id != receipt.agent_run_id
                        or current != record
                    ):
                        raise StoreCommitError("AgentRun receipt replay mismatch")
                    for reference_key, object_ref in cas_references:
                        ref = await connection.fetchrow(
                            """SELECT r.digest, o.size_bytes FROM cas_object_refs r
                            JOIN cas_objects o ON o.digest=r.digest
                            WHERE r.owner_kind=$1 AND r.owner_id=$2 AND r.reference_key=$3""",
                            record.owner_kind,
                            record.owner_id,
                            reference_key,
                        )
                        if (
                            ref is None
                            or str(_row_value(ref, "digest")) != object_ref.digest
                            or int(_row_value(ref, "size_bytes")) != object_ref.size
                        ):
                            raise StoreCommitError("AgentRun receipt CAS ref replay mismatch")
                    return existing_receipt
                _validate_agent_receipt(current, record, receipt, state, events, cas_references)
                if state is not None:
                    locked = await connection.fetchrow(
                        "SELECT state FROM runtime_runs WHERE run_id = $1 FOR UPDATE",
                        state.run_id,
                    )
                    if locked is None:
                        raise StoreCommitError("runtime run does not exist")
                    previous_state = _decode_state(_row_value(locked, "state"))
                    if state.version != previous_state.version + 1:
                        raise StoreCommitError("Run owner state version is stale")
                    sequence_row = await connection.fetchrow(
                        """SELECT COALESCE(MAX(sequence), 0) AS last_sequence
                        FROM runtime_events WHERE run_id = $1""",
                        state.run_id,
                    )
                    await connection.execute(
                        "UPDATE runtime_runs SET state = $2::jsonb WHERE run_id = $1",
                        state.run_id,
                        _dump_json(state),
                    )
                    await _insert_events(
                        connection,
                        state.run_id,
                        events,
                        first_sequence=int(_row_value(sequence_row, "last_sequence")) + 1,
                        secret_registry=self.secret_registry,
                    )
                await connection.execute(
                    """UPDATE agent_runs SET state = $2, exit = $3, metadata = $4::jsonb,
                    updated_at = CURRENT_TIMESTAMP WHERE agent_run_id = $1""",
                    record.agent_run_id,
                    record.state.value,
                    record.exit.value if record.exit is not None else None,
                    _dump_agent_run(record),
                )
                for reference_key, object_ref in cas_references:
                    await connection.execute(
                        """INSERT INTO cas_objects(digest,size_bytes) VALUES($1,$2)
                        ON CONFLICT (digest) DO NOTHING""",
                        object_ref.digest,
                        object_ref.size,
                    )
                    object_row = await connection.fetchrow(
                        "SELECT size_bytes FROM cas_objects WHERE digest=$1 FOR UPDATE",
                        object_ref.digest,
                    )
                    if int(_row_value(object_row, "size_bytes")) != object_ref.size:
                        raise StoreCommitError("CAS object size mismatch")
                    await connection.execute(
                        """INSERT INTO cas_object_refs(owner_kind,owner_id,reference_key,digest)
                        VALUES($1,$2,$3,$4) ON CONFLICT(owner_kind,owner_id,reference_key)
                        DO NOTHING""",
                        record.owner_kind,
                        record.owner_id,
                        reference_key,
                        object_ref.digest,
                    )
                    ref_row = await connection.fetchrow(
                        """SELECT digest FROM cas_object_refs WHERE owner_kind=$1
                        AND owner_id=$2 AND reference_key=$3""",
                        record.owner_kind,
                        record.owner_id,
                        reference_key,
                    )
                    if str(_row_value(ref_row, "digest")) != object_ref.digest:
                        raise StoreCommitError("CAS reference key already has another digest")
                await connection.execute(
                    """INSERT INTO agent_run_receipts(receipt_id, agent_run_id, category)
                    VALUES ($1,$2,$3)""",
                    receipt.receipt_id,
                    receipt.agent_run_id,
                    receipt.category,
                )
                return receipt

    async def add_cas_reference(
        self, object_ref: CasObject, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await _add_cas_reference_with_connection(
                    connection, object_ref, owner_kind, owner_id, reference_key
                )

    async def get_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT r.digest, o.size_bytes FROM cas_object_refs r
                JOIN cas_objects o ON o.digest = r.digest
                WHERE r.owner_kind = $1 AND r.owner_id = $2 AND r.reference_key = $3""",
                owner_kind,
                owner_id,
                reference_key,
            )
        return _cas_object_from_row(row) if row is not None else None

    async def release_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                return await _release_cas_reference_with_connection(
                    connection, owner_kind, owner_id, reference_key
                )

    async def referenced_digests(self) -> frozenset[str]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch("SELECT DISTINCT digest FROM cas_object_refs")
        return frozenset(str(_row_value(row, "digest")).strip() for row in rows)

    async def publish_checkpoint_index(self, index: CheckpointIndex) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO graph_threads(thread_id, graph_family, owner_kind, owner_id)
                    VALUES ($1,$2,$3,$4) ON CONFLICT (thread_id) DO NOTHING""",
                    UUID(index.thread_id),
                    index.graph_family,
                    index.owner_kind,
                    index.owner_id,
                )
                thread = await connection.fetchrow(
                    """SELECT graph_family, owner_kind, owner_id FROM graph_threads
                    WHERE thread_id = $1""",
                    UUID(index.thread_id),
                )
                if (
                    str(_row_value(thread, "graph_family")),
                    str(_row_value(thread, "owner_kind")),
                    _row_value(thread, "owner_id"),
                ) != (index.graph_family, index.owner_kind, index.owner_id):
                    raise StoreCommitError("checkpoint thread belongs to another owner")
                await _add_cas_reference_with_connection(
                    connection,
                    index.object,
                    index.owner_kind,
                    index.owner_id,
                    index.reference_key,
                )
                await connection.execute(
                    """INSERT INTO graph_checkpoints(
                        thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                        digest, size_bytes
                    ) VALUES ($1,$2,$3,$4,$5,$6)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO NOTHING""",
                    UUID(index.thread_id),
                    index.namespace,
                    index.checkpoint_id,
                    index.parent_checkpoint_id,
                    index.object.digest,
                    index.object.size,
                )
                row = await connection.fetchrow(
                    """SELECT parent_checkpoint_id, digest, size_bytes
                    FROM graph_checkpoints WHERE thread_id=$1 AND checkpoint_ns=$2
                    AND checkpoint_id=$3""",
                    UUID(index.thread_id),
                    index.namespace,
                    index.checkpoint_id,
                )
                if (
                    _row_value(row, "parent_checkpoint_id"),
                    str(_row_value(row, "digest")).strip(),
                    int(_row_value(row, "size_bytes")),
                ) != (index.parent_checkpoint_id, index.object.digest, index.object.size):
                    raise StoreCommitError("checkpoint identity collision")

    async def list_checkpoint_indexes(
        self, thread_id: str | None = None, namespace: str | None = None
    ) -> tuple[CheckpointIndex, ...]:
        async with self.pool.acquire() as connection:
            return await _list_checkpoint_indexes_with_connection(connection, thread_id, namespace)

    async def publish_pending_write_index(self, index: PendingWriteIndex) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO graph_threads(thread_id, graph_family, owner_kind, owner_id)
                    VALUES ($1,$2,$3,$4) ON CONFLICT (thread_id) DO NOTHING""",
                    UUID(index.thread_id),
                    index.graph_family,
                    index.owner_kind,
                    index.owner_id,
                )
                thread = await connection.fetchrow(
                    """SELECT graph_family, owner_kind, owner_id FROM graph_threads
                    WHERE thread_id=$1""",
                    UUID(index.thread_id),
                )
                if (
                    _row_value(thread, "graph_family"),
                    _row_value(thread, "owner_kind"),
                    _row_value(thread, "owner_id"),
                ) != (index.graph_family, index.owner_kind, index.owner_id):
                    raise StoreCommitError("pending write owner mismatch")
                existing = await connection.fetchrow(
                    """SELECT digest FROM graph_pending_writes WHERE thread_id=$1
                    AND checkpoint_ns=$2 AND checkpoint_id=$3 AND task_id=$4
                    AND write_index=$5 FOR UPDATE""",
                    UUID(index.thread_id),
                    index.namespace,
                    index.checkpoint_id,
                    index.task_id,
                    index.write_index,
                )
                if existing is not None and index.write_index >= 0:
                    return
                if existing is not None:
                    await connection.execute(
                        """DELETE FROM cas_object_refs WHERE owner_kind=$1 AND owner_id=$2
                        AND reference_key=$3""",
                        index.owner_kind,
                        index.owner_id,
                        index.reference_key,
                    )
                await _add_cas_reference_with_connection(
                    connection,
                    index.object,
                    index.owner_kind,
                    index.owner_id,
                    index.reference_key,
                )
                await connection.execute(
                    """INSERT INTO graph_pending_writes(
                        thread_id, checkpoint_ns, checkpoint_id, task_id, write_index,
                        digest, size_bytes
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id, task_id, write_index)
                    DO UPDATE SET digest=EXCLUDED.digest, size_bytes=EXCLUDED.size_bytes""",
                    UUID(index.thread_id),
                    index.namespace,
                    index.checkpoint_id,
                    index.task_id,
                    index.write_index,
                    index.object.digest,
                    index.object.size,
                )

    async def list_pending_write_indexes(
        self, thread_id: str, namespace: str, checkpoint_id: str
    ) -> tuple[PendingWriteIndex, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT t.graph_family, t.owner_kind, t.owner_id, w.thread_id, w.checkpoint_ns,
                       w.checkpoint_id, w.task_id, w.write_index, w.digest, w.size_bytes
                FROM graph_pending_writes w JOIN graph_threads t ON t.thread_id=w.thread_id
                WHERE w.thread_id=$1 AND w.checkpoint_ns=$2 AND w.checkpoint_id=$3
                ORDER BY w.created_at, w.task_id, w.write_index""",
                UUID(thread_id),
                namespace,
                checkpoint_id,
            )
        return tuple(
            PendingWriteIndex(
                str(_row_value(row, "graph_family")),
                str(_row_value(row, "owner_kind")),
                _row_value(row, "owner_id"),
                str(_row_value(row, "thread_id")),
                str(_row_value(row, "checkpoint_ns")),
                str(_row_value(row, "checkpoint_id")),
                str(_row_value(row, "task_id")),
                int(_row_value(row, "write_index")),
                _cas_object_from_row(row),
            )
            for row in rows
        )

    async def delete_checkpoint_thread(self, thread_id: str) -> tuple[CasObject, ...]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                thread = await connection.fetchrow(
                    "SELECT owner_kind, owner_id FROM graph_threads WHERE thread_id=$1 FOR UPDATE",
                    UUID(thread_id),
                )
                if thread is None:
                    return ()
                indexes = await _list_checkpoint_indexes_with_connection(
                    connection, thread_id, None
                )
                writes = await self.list_pending_write_indexes_for_thread(connection, thread_id)
                items: tuple[CheckpointIndex | PendingWriteIndex, ...] = (*indexes, *writes)
                await connection.execute(
                    "DELETE FROM graph_pending_writes WHERE thread_id=$1", UUID(thread_id)
                )
                await connection.execute(
                    "DELETE FROM graph_checkpoints WHERE thread_id=$1", UUID(thread_id)
                )
                await connection.execute(
                    "DELETE FROM graph_threads WHERE thread_id=$1", UUID(thread_id)
                )
                candidates = {item.object.digest: item.object for item in items}
                for item in items:
                    await connection.execute(
                        """DELETE FROM cas_object_refs WHERE owner_kind=$1 AND owner_id=$2
                        AND reference_key=$3""",
                        item.owner_kind,
                        item.owner_id,
                        item.reference_key,
                    )
                unreferenced = []
                for candidate in candidates.values():
                    remaining = await connection.fetchrow(
                        "SELECT 1 FROM cas_object_refs WHERE digest=$1 LIMIT 1", candidate.digest
                    )
                    if remaining is None:
                        await connection.execute(
                            "DELETE FROM cas_objects WHERE digest=$1", candidate.digest
                        )
                        unreferenced.append(candidate)
                return tuple(unreferenced)

    async def list_pending_write_indexes_for_thread(
        self, connection: Any, thread_id: str
    ) -> tuple[PendingWriteIndex, ...]:
        rows = await connection.fetch(
            """SELECT t.graph_family, t.owner_kind, t.owner_id, w.thread_id, w.checkpoint_ns,
                   w.checkpoint_id, w.task_id, w.write_index, w.digest, w.size_bytes
            FROM graph_pending_writes w JOIN graph_threads t ON t.thread_id=w.thread_id
            WHERE w.thread_id=$1""",
            UUID(thread_id),
        )
        return tuple(
            PendingWriteIndex(
                str(_row_value(row, "graph_family")),
                str(_row_value(row, "owner_kind")),
                _row_value(row, "owner_id"),
                str(_row_value(row, "thread_id")),
                str(_row_value(row, "checkpoint_ns")),
                str(_row_value(row, "checkpoint_id")),
                str(_row_value(row, "task_id")),
                int(_row_value(row, "write_index")),
                _cas_object_from_row(row),
            )
            for row in rows
        )

    async def initialize(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(RUNTIME_SCHEMA_SQL)

    async def read_run_events(self, run_id: RunId, after_sequence: int) -> tuple[RuntimeEvent, ...]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """SELECT sequence, event_type, data, timestamp_utc FROM runtime_events
                WHERE run_id=$1 AND sequence>$2 ORDER BY sequence""",
                run_id,
                after_sequence,
            )
        return tuple(
            RuntimeEvent(
                sequence=int(_row_value(row, "sequence")),
                event_type=str(_row_value(row, "event_type")),
                data=_decode_event_data(_row_value(row, "data")),
                timestamp_utc=_row_value(row, "timestamp_utc"),
            )
            for row in rows
        )

    async def wait_for_run_events(self, run_id: RunId, after_sequence: int) -> None:
        loop = asyncio.get_running_loop()
        wake = loop.create_future()

        def listener(_connection: Any, _pid: int, _channel: str, payload: str) -> None:
            if payload == str(run_id) and not wake.done():
                wake.set_result(None)

        async with self.pool.acquire() as connection:
            await connection.add_listener("runtime_events", listener)
            try:
                row = await connection.fetchrow(
                    """SELECT COALESCE(MAX(sequence), 0) AS latest_sequence,
                    COALESCE(MIN(sequence) FILTER (
                        WHERE event_type='run.status_changed'
                        AND COALESCE(data->>'run_status', data->>'status') = ANY($2::text[])
                    ), 0) AS terminal_sequence
                    FROM runtime_events WHERE run_id=$1""",
                    run_id,
                    list(_RUN_TERMINAL_STATUSES),
                )
                latest = int(_row_value(row, "latest_sequence"))
                terminal = int(_row_value(row, "terminal_sequence"))
                if latest > after_sequence or (terminal > 0 and terminal <= after_sequence):
                    return
                await wake
            finally:
                await connection.remove_listener("runtime_events", listener)

    async def is_run_stream_terminal(self, run_id: RunId, after_sequence: int) -> bool:
        async with self.pool.acquire() as connection:
            terminal = await connection.fetchval(
                """SELECT COALESCE(MIN(sequence), 0) FROM runtime_events
                WHERE run_id=$1 AND sequence<=$2 AND event_type='run.status_changed'
                AND COALESCE(data->>'run_status', data->>'status') = ANY($3::text[])""",
                run_id,
                after_sequence,
                list(_RUN_TERMINAL_STATUSES),
            )
        return int(terminal) > 0

    async def load(self, run_id: RunId) -> RuntimeSnapshot | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT state FROM runtime_runs WHERE run_id = $1",
                run_id,
            )
            if row is None:
                return None
            event_rows = await connection.fetch(
                """
                SELECT sequence, event_type, data, timestamp_utc
                FROM runtime_events
                WHERE run_id = $1
                ORDER BY sequence
                """,
                run_id,
            )
        return RuntimeSnapshot(
            state=_decode_state(_row_value(row, "state")),
            events=tuple(
                RuntimeEvent(
                    sequence=int(_row_value(event, "sequence")),
                    event_type=str(_row_value(event, "event_type")),
                    data=_decode_event_data(_row_value(event, "data")),
                    timestamp_utc=_row_value(event, "timestamp_utc"),
                )
                for event in event_rows
            ),
        )

    async def create(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await connection.execute(
                        "INSERT INTO runtime_runs(run_id, state) VALUES ($1, $2::jsonb)",
                        state.run_id,
                        _dump_json(state),
                    )
                except Exception as exc:
                    raise StoreCommitError("unable to create runtime run") from exc
                await _insert_events(
                    connection,
                    state.run_id,
                    events,
                    first_sequence=1,
                    secret_registry=self.secret_registry,
                )
                if evolution is not None:
                    await _append_evolution_with_connection(connection, evolution, state.run_id)
        snapshot = await self.load(state.run_id)
        if snapshot is None:
            raise StoreCommitError("created runtime run disappeared")
        return snapshot

    async def commit(
        self,
        state: RunState,
        events: Sequence[EventSpec],
        *,
        evolution: EvolutionSegmentDraft | None = None,
    ) -> RuntimeSnapshot:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    "SELECT run_id FROM runtime_runs WHERE run_id = $1 FOR UPDATE",
                    state.run_id,
                )
                if locked is None:
                    raise StoreCommitError("runtime run does not exist")
                row = await connection.fetchrow(
                    """
                    SELECT COALESCE(MAX(sequence), 0) AS last_sequence
                    FROM runtime_events
                    WHERE run_id = $1
                    """,
                    state.run_id,
                )
                if row is None:
                    raise StoreCommitError("runtime run does not exist")
                last_sequence = int(_row_value(row, "last_sequence"))
                updated = await connection.execute(
                    "UPDATE runtime_runs SET state = $2::jsonb WHERE run_id = $1",
                    state.run_id,
                    _dump_json(state),
                )
                if updated.split()[-1] != "1":
                    raise StoreCommitError("runtime run does not exist")
                await _insert_events(
                    connection,
                    state.run_id,
                    events,
                    first_sequence=last_sequence + 1,
                    secret_registry=self.secret_registry,
                )
                if evolution is not None:
                    await _append_evolution_with_connection(connection, evolution, state.run_id)
        snapshot = await self.load(state.run_id)
        if snapshot is None:
            raise StoreCommitError("committed runtime run disappeared")
        return snapshot

    async def append_evolution_segment(
        self,
        *,
        run_id: object,
        slice_id: object,
        summary_text: str,
        template_sha256: str,
    ) -> EvolutionSegment:
        """Append one immutable evolution entry in the runtime transaction boundary."""

        if not summary_text.strip():
            raise ValueError("evolution summary must be non-empty")
        if len(template_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in template_sha256
        ):
            raise ValueError("template digest must be SHA-256")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                locked = await connection.fetchrow(
                    "SELECT run_id FROM runtime_runs WHERE run_id = $1 FOR UPDATE",
                    run_id,
                )
                if locked is None:
                    raise StoreCommitError("runtime run does not exist")
                entry = await _append_evolution_with_connection(
                    connection,
                    EvolutionSegmentDraft(run_id, slice_id, summary_text, template_sha256),
                    run_id,
                )
        return entry

    async def evolution_segments(self, *, run_id: object) -> tuple[EvolutionSegment, ...]:
        """Read an ordered immutable evolution projection for one Run."""

        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT entry_index, slice_id, summary_text, template_sha256
                FROM context_evolution_segments
                WHERE run_id = $1
                ORDER BY entry_index
                """,
                run_id,
            )
        return tuple(
            EvolutionSegment(
                run_id,
                int(_row_value(row, "entry_index")),
                _row_value(row, "slice_id"),
                str(_row_value(row, "summary_text")),
                str(_row_value(row, "template_sha256")),
            )
            for row in rows
        )


async def _append_evolution_with_connection(
    connection: Any,
    draft: EvolutionSegmentDraft,
    run_id: object,
) -> EvolutionSegment:
    _validate_evolution_draft(draft, run_id)
    row = await connection.fetchrow(
        """
        SELECT COALESCE(MAX(entry_index) + 1, 0) AS next_index,
               MIN(template_sha256) AS frozen_template
        FROM context_evolution_segments
        WHERE run_id = $1
        """,
        run_id,
    )
    if row is None:
        raise StoreCommitError("unable to read context evolution sequence")
    frozen_template = _row_value(row, "frozen_template")
    if frozen_template is not None and str(frozen_template) != draft.template_sha256:
        raise StoreCommitError("context evolution template is frozen per Run")
    duplicate = await connection.fetchrow(
        """
        SELECT 1
        FROM context_evolution_segments
        WHERE run_id = $1 AND slice_id = $2
        LIMIT 1
        """,
        run_id,
        draft.slice_id,
    )
    if duplicate is not None:
        raise StoreCommitError("context evolution slice has already been appended")
    entry_index = int(_row_value(row, "next_index"))
    try:
        await connection.execute(
            """
            INSERT INTO context_evolution_segments(
                run_id, entry_index, slice_id, summary_text, template_sha256
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            run_id,
            entry_index,
            draft.slice_id,
            draft.summary_text,
            draft.template_sha256,
        )
    except Exception as exc:
        raise StoreCommitError("unable to append context evolution") from exc
    return EvolutionSegment(
        run_id, entry_index, draft.slice_id, draft.summary_text, draft.template_sha256
    )


def _cas_object_from_row(row: Any) -> CasObject:
    return CasObject(
        str(_row_value(row, "digest")).strip(),
        int(_row_value(row, "size_bytes")),
    )


async def _list_checkpoint_indexes_with_connection(
    connection: Any, thread_id: str | None, namespace: str | None
) -> tuple[CheckpointIndex, ...]:
    rows = await connection.fetch(
        """SELECT t.graph_family, t.owner_kind, t.owner_id,
               c.thread_id, c.checkpoint_ns, c.checkpoint_id,
               c.parent_checkpoint_id, c.digest, c.size_bytes
        FROM graph_checkpoints c JOIN graph_threads t ON t.thread_id=c.thread_id
        WHERE ($1::uuid IS NULL OR c.thread_id=$1)
          AND ($2::text IS NULL OR c.checkpoint_ns=$2)
        ORDER BY c.checkpoint_id DESC""",
        UUID(thread_id) if thread_id is not None else None,
        namespace,
    )
    return tuple(
        CheckpointIndex(
            str(_row_value(row, "graph_family")),
            str(_row_value(row, "owner_kind")),
            _row_value(row, "owner_id"),
            str(_row_value(row, "thread_id")),
            str(_row_value(row, "checkpoint_ns")),
            str(_row_value(row, "checkpoint_id")),
            _row_value(row, "parent_checkpoint_id"),
            _cas_object_from_row(row),
        )
        for row in rows
    )


async def _add_cas_reference_with_connection(
    connection: Any,
    object_ref: CasObject,
    owner_kind: str,
    owner_id: UUID,
    reference_key: str,
) -> None:
    await connection.execute(
        """INSERT INTO cas_objects(digest, size_bytes) VALUES ($1,$2)
        ON CONFLICT (digest) DO NOTHING""",
        object_ref.digest,
        object_ref.size,
    )
    object_row = await connection.fetchrow(
        "SELECT size_bytes FROM cas_objects WHERE digest=$1 FOR UPDATE", object_ref.digest
    )
    if object_row is None or int(_row_value(object_row, "size_bytes")) != object_ref.size:
        raise StoreCommitError("CAS object size mismatch")
    await connection.execute(
        """INSERT INTO cas_object_refs(owner_kind, owner_id, reference_key, digest)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT (owner_kind, owner_id, reference_key) DO NOTHING""",
        owner_kind,
        owner_id,
        reference_key,
        object_ref.digest,
    )
    ref_row = await connection.fetchrow(
        """SELECT digest FROM cas_object_refs WHERE owner_kind=$1 AND owner_id=$2
        AND reference_key=$3""",
        owner_kind,
        owner_id,
        reference_key,
    )
    if ref_row is None or str(_row_value(ref_row, "digest")).strip() != object_ref.digest:
        raise StoreCommitError("CAS reference key already points to another object")


async def _release_cas_reference_with_connection(
    connection: Any,
    owner_kind: str,
    owner_id: UUID,
    reference_key: str,
) -> CasObject | None:
    row = await connection.fetchrow(
        """SELECT r.digest, o.size_bytes FROM cas_object_refs r
        JOIN cas_objects o ON o.digest=r.digest
        WHERE r.owner_kind=$1 AND r.owner_id=$2 AND r.reference_key=$3
        FOR UPDATE OF r""",
        owner_kind,
        owner_id,
        reference_key,
    )
    if row is None:
        return None
    object_ref = _cas_object_from_row(row)
    await connection.execute(
        """DELETE FROM cas_object_refs WHERE owner_kind=$1 AND owner_id=$2
        AND reference_key=$3""",
        owner_kind,
        owner_id,
        reference_key,
    )
    remaining = await connection.fetchrow(
        "SELECT 1 FROM cas_object_refs WHERE digest=$1 LIMIT 1", object_ref.digest
    )
    if remaining is not None:
        return None
    await connection.execute("DELETE FROM cas_objects WHERE digest=$1", object_ref.digest)
    return object_ref


async def _insert_events(
    connection: Any,
    run_id: RunId,
    events: Sequence[EventSpec],
    *,
    first_sequence: int,
    secret_registry: SecretRegistry,
) -> None:
    for index, event in enumerate(events, start=first_sequence):
        try:
            data = _redact_event_data(event.data, secret_registry)
        except ValueError as exc:
            raise StoreCommitError("observation rejected") from exc
        await connection.execute(
            """
            INSERT INTO runtime_events(run_id, sequence, event_type, data, timestamp_utc)
            VALUES ($1, $2, $3, $4::jsonb, clock_timestamp())
            """,
            run_id,
            index,
            event.event_type,
            json.dumps(data, sort_keys=True, separators=(",", ":")),
        )
    if events:
        await connection.execute("SELECT pg_notify('runtime_events', $1)", str(run_id))


def _decode_event_data(value: Any) -> dict[str, object]:
    try:
        payload = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else dict(value)
    except (TypeError, ValueError) as exc:
        raise StoreCommitError("stored Run event data is invalid") from exc
    if not isinstance(payload, dict):
        raise StoreCommitError("stored Run event data is not an object")
    return cast(dict[str, object], payload)


def _event_status(data: Mapping[str, object]) -> object:
    status = data.get("run_status", data.get("status"))
    return getattr(status, "value", status)


def _redact_event_data(value: dict[str, object], registry: SecretRegistry) -> dict[str, object]:
    result = registry.redact(value)
    if not result.accepted or not isinstance(result.value, dict):
        raise ValueError("observation rejected")
    return cast(dict[str, object], result.value)


_DRAFT_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "session.question.asked": frozenset({"question_id"}),
    "session.question.answered": frozenset({"question_id"}),
    "session.attached_to_run": frozenset({"run_id"}),
    "session.closed": frozenset({"status"}),
    "agent_run.started": frozenset(
        {"agent_run_id", "phase", "session_kind", "slice_id", "generation"}
    ),
    "agent_run.terminal": frozenset(
        {
            "agent_run_id",
            "phase",
            "session_kind",
            "slice_id",
            "generation",
            "exit",
            "receipt_category",
        }
    ),
}


def _prepare_draft_events(
    events: Sequence[DraftSessionEventSpec], registry: SecretRegistry
) -> tuple[tuple[str, dict[str, object]], ...]:
    prepared = []
    for event in events:
        fields = _DRAFT_EVENT_FIELDS.get(event.event_type)
        if fields is None:
            raise StoreCommitError("Draft session event type is not durable")
        if event.event_type in {"session.question.asked", "session.question.answered"}:
            required = {"question_id"}
        elif event.event_type == "session.attached_to_run":
            required = {"run_id"}
        elif event.event_type == "session.closed":
            required = {"status"}
        else:
            required = {"agent_run_id", "phase", "session_kind"}
            if event.event_type == "agent_run.terminal":
                required |= {"exit", "receipt_category"}
        if not required <= event.data.keys():
            raise StoreCommitError("Draft session event fields are incomplete")
        projected = {key: value for key, value in event.data.items() if key in fields}
        if event.event_type == "session.closed" and projected != {"status": "CLOSED"}:
            raise StoreCommitError("Draft close event status is invalid")
        try:
            if event.event_type.startswith("agent_run."):
                if projected["phase"] not in {item.value for item in Phase} or projected[
                    "session_kind"
                ] not in {item.value for item in SessionKind}:
                    raise ValueError("AgentRun public enum is invalid")
                if event.event_type == "agent_run.terminal" and (
                    projected["exit"] not in {item.value for item in SessionExit}
                    or not isinstance(projected["receipt_category"], str)
                    or re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", projected["receipt_category"])
                    is None
                ):
                    raise ValueError("AgentRun terminal summary is invalid")
            for key in ("question_id", "run_id", "agent_run_id", "slice_id"):
                if key in projected:
                    UUID(str(projected[key]))
            if ("slice_id" in projected) != ("generation" in projected):
                raise ValueError("slice fields must be paired")
            if "generation" in projected and (
                type(projected["generation"]) is not int or projected["generation"] < 0
            ):
                raise ValueError("slice generation is invalid")
            projected = _redact_event_data(projected, registry)
            projected = _decode_draft_fact(canonical_json_bytes(projected))
        except (TypeError, ValueError) as exc:
            raise StoreCommitError("Draft session event data is invalid") from exc
        prepared.append((event.event_type, projected))
    return tuple(prepared)


def _make_draft_receipt(
    draft_id: UUID,
    receipt_key: str,
    category: str,
    fact: Mapping[str, object],
) -> tuple[DraftOwnerReceipt, dict[str, object]]:
    if not isinstance(draft_id, UUID) or not isinstance(fact, Mapping):
        raise StoreCommitError("Draft owner fact identity or body is invalid")
    if not isinstance(receipt_key, str) or not receipt_key or len(receipt_key) > 256:
        raise StoreCommitError("Draft owner fact receipt key is invalid")
    if not isinstance(category, str) or not category or len(category) > 64:
        raise StoreCommitError("Draft owner fact category is invalid")
    try:
        encoded = canonical_json_bytes(dict(fact))
        payload = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise StoreCommitError("Draft owner fact is not canonical JSON") from exc
    if not isinstance(payload, dict):
        raise StoreCommitError("Draft owner fact must be a JSON object")
    receipt = DraftOwnerReceipt(
        draft_id=draft_id,
        receipt_key=receipt_key,
        category=category,
        fact_sha256=hashlib.sha256(encoded).hexdigest(),
    )
    return receipt, cast(dict[str, object], payload)


def _decode_draft_fact(value: Any) -> dict[str, object]:
    try:
        payload = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else dict(value)
    except (TypeError, ValueError) as exc:
        raise StoreCommitError("stored Draft owner fact is invalid") from exc
    if not isinstance(payload, dict):
        raise StoreCommitError("stored Draft owner fact is not an object")
    return cast(dict[str, object], payload)


def _verify_draft_fact_digest(receipt: DraftOwnerReceipt, fact: Mapping[str, object]) -> None:
    digest = hashlib.sha256(canonical_json_bytes(dict(fact))).hexdigest()
    if digest != receipt.fact_sha256:
        raise StoreCommitError("stored Draft owner fact digest mismatch")


def _row_value(row: Any, key: str) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[key]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json", by_alias=True))
    if is_dataclass(value):
        return _json_value(asdict(cast(Any, value)))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value.value if hasattr(value, "value") else value


def _dump_json(state: RunState) -> str:
    return json.dumps(_json_value(state), sort_keys=True, separators=(",", ":"))


def _dump_agent_run(record: AgentRun) -> str:
    return json.dumps(_json_value(record), sort_keys=True, separators=(",", ":"))


def _decode_agent_run(value: Any) -> AgentRun:
    payload = json.loads(value) if isinstance(value, str) else dict(value)
    return AgentRun(
        agent_run_id=AgentRunId(UUID(payload["agent_run_id"])),
        owner_kind=str(payload["owner_kind"]),
        owner_id=UUID(payload["owner_id"]),
        logical_task_key=str(payload["logical_task_key"]),
        phase=Phase(payload["phase"]),
        session_kind=SessionKind(payload["session_kind"]),
        thread_id=str(payload["thread_id"]),
        model_binding_sha256=str(payload["model_binding_sha256"]),
        context_sha256=str(payload["context_sha256"]),
        toolset_sha256=str(payload["toolset_sha256"]),
        template_sha256=str(payload["template_sha256"]),
        slice_ref=(
            SliceGenerationRef.model_validate(payload["slice_ref"])
            if payload.get("slice_ref") is not None
            else None
        ),
        write_scope_sha256=payload.get("write_scope_sha256"),
        checkpoint_sha256=payload.get("checkpoint_sha256"),
        candidate_checkpoint_sha256=payload.get("candidate_checkpoint_sha256"),
        result_sha256=payload.get("result_sha256"),
        usage_sha256=payload.get("usage_sha256"),
        retry_of=(AgentRunId(UUID(payload["retry_of"])) if payload.get("retry_of") else None),
        continuation_of=(
            AgentRunId(UUID(payload["continuation_of"])) if payload.get("continuation_of") else None
        ),
        restarted_from=(
            AgentRunId(UUID(payload["restarted_from"])) if payload.get("restarted_from") else None
        ),
        state=SessionState(payload["state"]),
        exit=SessionExit(payload["exit"]) if payload.get("exit") else None,
    )


def _decode_state(value: Any) -> RunState:
    payload = json.loads(value) if isinstance(value, str) else dict(value)
    return RunState(
        run_id=RunId(UUID(payload["run_id"])),
        status=RunStatus(payload["status"]),
        version=int(payload["version"]),
        cancel_requested=bool(payload["cancel_requested"]),
        failure_reason=(
            FailureReason(payload["failure_reason"])
            if payload.get("failure_reason") is not None
            else None
        ),
        new_calls_enabled=bool(payload["new_calls_enabled"]),
        budget_usage=BudgetUsage(**payload["budget_usage"]),
        budget_warning_emitted=bool(payload["budget_warning_emitted"]),
        active_dispatches=tuple(
            ActiveDispatch.model_validate(item) for item in payload["active_dispatches"]
        ),
        continuation_counts=tuple(
            (int(item[0]), int(item[1])) for item in payload["continuation_counts"]
        ),
        terminal_slice_failures=tuple(payload["terminal_slice_failures"]),
        adopted_advice_ids=tuple(payload.get("adopted_advice_ids", ())),
        pending_advice_ids=tuple(payload.get("pending_advice_ids", ())),
        create_request=(
            CreateRun.model_validate(payload["create_request"])
            if payload.get("create_request") is not None
            else None
        ),
        frozen_plan_sha256=payload.get("frozen_plan_sha256"),
        candidate_checkpoints=tuple(
            CandidateCheckpointFact(
                agent_run_id=AgentRunId(UUID(item["agent_run_id"])),
                slice_id=SliceId(UUID(item["slice_id"])),
                generation=int(item["generation"]),
                expected_candidate_oid=str(item["expected_candidate_oid"]),
                candidate_oid=str(item["candidate_oid"]),
                receipt_sha256=str(item["receipt_sha256"]),
            )
            for item in payload.get("candidate_checkpoints", ())
        ),
    )


__all__ = [
    "InMemoryRuntimeStore",
    "PostgreSQLRuntimeStore",
    "RuntimeStore",
    "StoreCommitError",
]
