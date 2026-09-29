"""Runtime persistence ports and a deterministic transactional test adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from codemigrator.core import (
    ActiveDispatch,
    FailureReason,
    Phase,
    RunId,
    RunStatus,
    SecretRegistry,
    SessionKind,
    SliceGenerationRef,
)

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .budget import BudgetUsage
from .contracts import EventSpec, RunState, RuntimeEvent, RuntimeSnapshot
from .loop_contracts import SessionExit, SessionState
from .memory import EvolutionSegment, EvolutionSegmentDraft
from .schema import RUNTIME_SCHEMA_SQL


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

    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun:
        """Create one session per owner logical task, or return its frozen identity."""

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Load private session metadata."""

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None:
        """Load the committed owner receipt, if any."""

    async def commit_agent_run_receipt(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        *,
        state: RunState | None = None,
        events: Sequence[EventSpec] = (),
    ) -> AgentRunReceipt:
        """Commit terminal metadata and owner facts/events in one transaction."""


class StoreCommitError(RuntimeError):
    """Raised when the persistence transaction cannot be committed."""


def _validate_new_agent_run(record: AgentRun) -> None:
    if record.state is not SessionState.Created or record.exit is not None:
        raise StoreCommitError("new AgentRun must be created without an exit")
    if any(
        getattr(record, name) is not None
        for name in ("checkpoint_sha256", "result_sha256", "usage_sha256")
    ):
        raise StoreCommitError("new AgentRun cannot have terminal references")


def _validate_agent_receipt(
    current: AgentRun | None,
    record: AgentRun,
    receipt: AgentRunReceipt,
    state: RunState | None,
    events: Sequence[EventSpec],
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
        self._agent_runs: dict[AgentRunId, AgentRun] = {}
        self._agent_run_keys: dict[tuple[str, UUID, str], AgentRunId] = {}
        self._agent_threads: dict[UUID, AgentRunId] = {}
        self._agent_receipts: dict[AgentRunId, AgentRunReceipt] = {}
        self._agent_lock = asyncio.Lock()

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

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None:
        return self._agent_receipts.get(agent_run_id)

    async def commit_agent_run_receipt(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        *,
        state: RunState | None = None,
        events: Sequence[EventSpec] = (),
    ) -> AgentRunReceipt:
        async with self._agent_lock:
            existing_receipt = self._agent_receipts.get(record.agent_run_id)
            if existing_receipt is not None:
                if (
                    existing_receipt.category != receipt.category
                    or existing_receipt.agent_run_id != receipt.agent_run_id
                    or self._agent_runs[record.agent_run_id] != record
                ):
                    raise StoreCommitError("AgentRun receipt replay mismatch")
                return existing_receipt
            current = self._agent_runs.get(record.agent_run_id)
            _validate_agent_receipt(current, record, receipt, state, events)
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
                self._snapshots[RunId(record.owner_id)] = snapshot
            self._agent_runs[record.agent_run_id] = record
            self._agent_receipts[record.agent_run_id] = receipt
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
                    return existing_receipt
                _validate_agent_receipt(current, record, receipt, state, events)
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
                await connection.execute(
                    """INSERT INTO agent_run_receipts(receipt_id, agent_run_id, category)
                    VALUES ($1,$2,$3)""",
                    receipt.receipt_id,
                    receipt.agent_run_id,
                    receipt.category,
                )
                return receipt

    async def initialize(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(RUNTIME_SCHEMA_SQL)

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
                SELECT sequence, event_type, data
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
                    data=dict(_row_value(event, "data")),
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
            INSERT INTO runtime_events(run_id, sequence, event_type, data)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            run_id,
            index,
            event.event_type,
            json.dumps(data, sort_keys=True, separators=(",", ":")),
        )


def _redact_event_data(value: dict[str, object], registry: SecretRegistry) -> dict[str, object]:
    result = registry.redact(value)
    if not result.accepted or not isinstance(result.value, dict):
        raise ValueError("observation rejected")
    return cast(dict[str, object], result.value)


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
        checkpoint_sha256=payload.get("checkpoint_sha256"),
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
    )


__all__ = [
    "InMemoryRuntimeStore",
    "PostgreSQLRuntimeStore",
    "RuntimeStore",
    "StoreCommitError",
]
