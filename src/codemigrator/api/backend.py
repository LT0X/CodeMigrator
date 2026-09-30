"""Concrete API command adapter over injected durable domain-owner ports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol, cast
from uuid import UUID

from codemigrator.core import CreateRun, RunStatus

from .deps import ApiRequest, EventRecord, PersistedEvent
from .problems import ApiError


class RunCreatedProjection(Protocol):
    run_id: UUID
    receipt_key: str
    state_version: int


class ApiCommandStorePort(Protocol):
    async def initialize(self) -> None: ...

    async def execute_api_command(
        self,
        *,
        principal_id: str,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
        command: Callable[[object], Awaitable[object]],
        project_response: Callable[[object], object],
        owner_receipt: Callable[[object], tuple[str, UUID, str] | None],
    ) -> Mapping[str, object]: ...

    async def list_pending_graph_starts(self) -> tuple[tuple[UUID, str], ...]: ...

    async def mark_graph_start_started(self, run_id: UUID, receipt_key: str) -> None: ...

    async def read_run_events(
        self, run_id: UUID, after_sequence: int
    ) -> Sequence[PersistedEvent]: ...

    async def wait_for_run_events(self, run_id: UUID, after_sequence: int) -> None: ...

    async def is_run_stream_terminal(self, run_id: UUID, after_sequence: int) -> bool: ...

    async def read_draft_session_events(
        self, draft_id: UUID, after_sequence: int
    ) -> Sequence[PersistedEvent]: ...

    async def wait_for_draft_session_events(self, draft_id: UUID, after_sequence: int) -> None: ...

    async def is_draft_session_terminal(self, draft_id: UUID, after_sequence: int) -> bool: ...


class RunCreationOwnerPort(Protocol):
    async def create_run(self, request: CreateRun, transaction: object) -> RunCreatedProjection: ...

    async def start_graph(self, run_id: UUID, receipt: RunCreatedProjection) -> None: ...

    async def load_run_created_receipt(
        self, run_id: UUID, receipt_key: str
    ) -> RunCreatedProjection: ...


class ProductionApiBackend:
    """Dispatch only commands and streams backed by durable owner capabilities."""

    def __init__(
        self,
        store: ApiCommandStorePort,
        *,
        run_owner: RunCreationOwnerPort | None = None,
        shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._run_owner = run_owner
        self._shutdown = shutdown
        self._handoff_lock = asyncio.Lock()

    async def close(self) -> None:
        """Release host-owned graph managers after request handling stops."""

        if self._shutdown is not None:
            await self._shutdown()

    async def execute(self, request: ApiRequest) -> object:
        if request.operation == "create_run":
            raise ApiError(
                422,
                "CreateRun requires an Idempotency-Key",
                "IDEMPOTENCY_KEY_REQUIRED",
            )
        raise _unavailable()

    async def execute_idempotent(
        self,
        request: ApiRequest,
        *,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
    ) -> object:
        owner = self._run_owner
        payload = request.payload
        if request.operation != "create_run" or not isinstance(payload, CreateRun) or owner is None:
            raise _unavailable()
        if not key.strip() or len(key) > 256:
            raise ApiError(422, "Idempotency-Key is invalid", "INVALID_REQUEST")

        async def command(transaction: object) -> RunCreatedProjection:
            return await owner.create_run(payload, transaction)

        def project_response(value: object) -> dict[str, object]:
            receipt = cast(RunCreatedProjection, value)
            return {
                "run_id": str(receipt.run_id),
                "status": RunStatus.Planning.value,
                "version": receipt.state_version,
            }

        def owner_receipt(value: object) -> tuple[str, UUID, str]:
            receipt = cast(RunCreatedProjection, value)
            return "run", UUID(str(receipt.run_id)), receipt.receipt_key

        try:
            outcome = await self._store.execute_api_command(
                principal_id=request.principal_id,
                route=route,
                key=key,
                canonical_body=canonical_body,
                status_code=status_code,
                command=command,
                project_response=project_response,
                owner_receipt=owner_receipt,
            )
        except ApiError:
            raise
        except Exception:
            raise _unavailable() from None

        if outcome.get("conflict") is True:
            raise ApiError(
                409,
                "idempotency key was reused with a different body",
                "IDEMPOTENCY_CONFLICT",
                retryable=False,
            )
        response = outcome.get("response")
        if not isinstance(response, dict):
            raise ApiError(500, "stored command receipt is invalid", "INTERNAL_ERROR")
        if outcome.get("owner_kind") == "run":
            owner_id = outcome.get("owner_id")
            receipt_key = outcome.get("owner_receipt_key")
            if owner_id is not None and isinstance(receipt_key, str):
                await self._try_start_pending_graph(UUID(str(owner_id)), receipt_key)
        return response

    async def read_events(self, run_id: UUID, after_sequence: int) -> Sequence[EventRecord]:
        events = await self._store.read_run_events(run_id, after_sequence)
        return tuple(EventRecord.from_persisted(run_id, event) for event in events)

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        await self._store.wait_for_run_events(run_id, after_sequence)

    async def is_stream_terminal(self, run_id: UUID, after_sequence: int) -> bool:
        return await self._store.is_run_stream_terminal(run_id, after_sequence)

    async def read_session_events(
        self, session_id: UUID, after_sequence: int
    ) -> Sequence[EventRecord]:
        events = await self._store.read_draft_session_events(session_id, after_sequence)
        return tuple(EventRecord.from_persisted(session_id, event) for event in events)

    async def wait_for_session_events(self, session_id: UUID, after_sequence: int) -> None:
        await self._store.wait_for_draft_session_events(session_id, after_sequence)

    async def is_session_stream_terminal(self, session_id: UUID, after_sequence: int) -> bool:
        return await self._store.is_draft_session_terminal(session_id, after_sequence)

    async def recover_pending_graph_starts(self) -> None:
        pending = await self._store.list_pending_graph_starts()
        if pending and self._run_owner is None:
            raise RuntimeError("Run graph-start recovery is not configured")
        for run_id, receipt_key in pending:
            await self._start_pending_graph(run_id, receipt_key, fail_closed=True)

    async def _try_start_pending_graph(self, run_id: UUID, receipt_key: str) -> None:
        await self._start_pending_graph(run_id, receipt_key, fail_closed=False)

    async def _start_pending_graph(
        self, run_id: UUID, receipt_key: str, *, fail_closed: bool
    ) -> None:
        owner = self._run_owner
        if owner is None:
            if fail_closed:
                raise RuntimeError("Run graph-start recovery is not configured")
            return
        # One API process owns the PostgreSQL advisory lock. This process-wide
        # handoff lock also serializes concurrent idempotent HTTP replays.
        async with self._handoff_lock:
            if (run_id, receipt_key) not in await self._store.list_pending_graph_starts():
                return
            try:
                receipt = await owner.load_run_created_receipt(run_id, receipt_key)
                await owner.start_graph(run_id, receipt)
                await self._store.mark_graph_start_started(run_id, receipt_key)
            except Exception:
                if fail_closed:
                    raise RuntimeError("pending Run graph-start recovery failed") from None
                # The committed handoff remains pending and will be retried at startup.
                return


def _unavailable() -> ApiError:
    return ApiError(
        503,
        "the requested API capability is unavailable",
        "DEPENDENCY_UNAVAILABLE",
        retryable=True,
    )


__all__ = [
    "ApiCommandStorePort",
    "ProductionApiBackend",
    "RunCreationOwnerPort",
    "RunCreatedProjection",
]
