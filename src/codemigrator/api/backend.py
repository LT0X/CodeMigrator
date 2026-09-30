"""Concrete API command adapter over injected durable domain-owner ports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from threading import Lock
from typing import Protocol, cast
from uuid import UUID

from codemigrator.core import CreateRun, FailureReason, RunStatus, StableErrorCode

from .deps import ApiRequest, EventRecord, PersistedEvent
from .problems import ApiError


class RunCreatedProjection(Protocol):
    run_id: UUID
    receipt_key: str
    state_version: int


class RunStateProjection(Protocol):
    run_id: UUID
    status: RunStatus
    version: int


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
    recovery_safe: bool

    async def create_run(self, request: CreateRun, transaction: object) -> RunCreatedProjection: ...

    async def cancel_run(self, run_id: UUID, expected_version: int) -> RunStateProjection: ...

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
        health_check: Callable[[], Awaitable[Mapping[str, object]]] | None = None,
    ) -> None:
        self._store = store
        self._run_owner = run_owner
        self._shutdown = shutdown
        self._health_check = health_check
        self._admission_lock = Lock()
        self._admission_open = True
        self._active_command_tasks: set[asyncio.Task[object]] = set()
        self._active_commands_drained = asyncio.Event()
        self._active_commands_drained.set()
        self._graph_start_tasks: dict[tuple[UUID, str], asyncio.Task[None]] = {}
        self._close_lock = asyncio.Lock()
        self._closed = False
        if run_owner is not None and getattr(run_owner, "recovery_safe", None) is not True:
            raise ValueError("Run owner does not guarantee graph receipt recovery")

    async def close(self) -> None:
        """Stop admission and drain commands/graphs before releasing owners."""

        async with self._close_lock:
            if self._closed:
                return
            self.close_admission()
            await self.drain_active_commands()
            await self.cancel_graph_tasks()
            self._closed = True
            failures = False
            close_owner = getattr(self._run_owner, "close", None)
            for close_resource in (close_owner, self._shutdown):
                if close_resource is None:
                    continue
                try:
                    await close_resource()
                except Exception:
                    failures = True
            if failures:
                raise RuntimeError("API backend shutdown failed")

    def close_admission(self) -> None:
        """Synchronously reject new commands and cancel admitted writes."""

        with self._admission_lock:
            if not self._admission_open:
                return
            self._admission_open = False
            active = tuple(self._active_command_tasks)
        for task in active:
            loop = task.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
        for task in tuple(self._graph_start_tasks.values()):
            loop = task.get_loop()
            if not loop.is_closed():
                loop.call_soon_threadsafe(task.cancel)
        close_owner_admission = getattr(self._run_owner, "close_admission", None)
        if callable(close_owner_admission):
            close_owner_admission()

    async def drain_active_commands(self) -> None:
        await self._active_commands_drained.wait()

    async def cancel_graph_tasks(self) -> None:
        tasks = tuple(self._graph_start_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._graph_start_tasks.clear()

    def _begin_command(self) -> None:
        task = asyncio.current_task()
        if task is None:
            raise _unavailable()
        with self._admission_lock:
            if not self._admission_open or self._closed:
                raise _unavailable()
            self._active_command_tasks.add(cast(asyncio.Task[object], task))
            self._active_commands_drained.clear()

    def _finish_command(self) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        with self._admission_lock:
            self._active_command_tasks.discard(cast(asyncio.Task[object], task))
            drained = not self._active_command_tasks
        if drained:
            self._active_commands_drained.set()

    def _is_admission_open(self) -> bool:
        with self._admission_lock:
            return self._admission_open and not self._closed

    async def execute(self, request: ApiRequest) -> object:
        self._begin_command()
        try:
            return await self._execute_admitted(request)
        except asyncio.CancelledError:
            if not self._is_admission_open():
                raise _unavailable() from None
            raise
        finally:
            self._finish_command()

    async def _execute_admitted(self, request: ApiRequest) -> object:
        if request.operation == "health":
            if self._health_check is None:
                raise _unavailable()
            try:
                return await self._health_check()
            except ApiError:
                raise
            except Exception:
                raise _unavailable() from None
        if request.operation == "cancel_run":
            owner = self._run_owner
            run_id = request.resource_id
            expected_version = request.expected_version
            if owner is None or run_id is None or expected_version is None:
                raise _unavailable()
            try:
                state = await owner.cancel_run(run_id, expected_version)
            except ApiError:
                raise
            except KeyError:
                raise ApiError(404, "resource not found", "NOT_FOUND") from None
            except Exception as error:
                code = getattr(error, "api_error_code", None)
                if code == StableErrorCode.STALE_VERSION.value:
                    raise ApiError(
                        409,
                        "Run version no longer matches If-Match",
                        StableErrorCode.STALE_VERSION.value,
                        retryable=False,
                    ) from None
                if code in {
                    StableErrorCode.PHASE_STATUS_MISMATCH.value,
                    "NOT_FOUND",
                }:
                    status = 404 if code == "NOT_FOUND" else 409
                    detail = "resource not found" if status == 404 else "Run cannot be cancelled"
                    raise ApiError(status, detail, str(code), retryable=False) from None
                raise _unavailable() from None
            return {
                "run_id": str(state.run_id),
                "status": state.status.value,
                "version": state.version,
            }
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
        self._begin_command()
        try:
            return await self._execute_idempotent_admitted(
                request,
                route=route,
                key=key,
                canonical_body=canonical_body,
                status_code=status_code,
            )
        except asyncio.CancelledError:
            if not self._is_admission_open():
                raise _unavailable() from None
            raise
        finally:
            self._finish_command()

    async def _execute_idempotent_admitted(
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
        except Exception as error:
            gate_code = _create_run_gate_code(error)
            if gate_code is not None:
                raise ApiError(
                    422,
                    "CreateRun was rejected by deterministic preflight",
                    gate_code,
                    retryable=False,
                ) from None
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
        if (
            self._run_owner is not None
            and getattr(self._run_owner, "recovery_safe", None) is not True
        ):
            raise RuntimeError("Run graph starter does not guarantee receipt recovery")
        for run_id, receipt_key in pending:
            self._schedule_pending_graph_start(run_id, receipt_key)

    async def _try_start_pending_graph(self, run_id: UUID, receipt_key: str) -> None:
        self._schedule_pending_graph_start(run_id, receipt_key)

    def _schedule_pending_graph_start(self, run_id: UUID, receipt_key: str) -> None:
        if self._run_owner is None or not self._is_admission_open():
            return
        task_key = (run_id, receipt_key)
        existing = self._graph_start_tasks.get(task_key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._run_pending_graph_start(run_id, receipt_key),
            name=f"codemigrator-graph-start-{run_id}-{receipt_key}",
        )
        self._graph_start_tasks[task_key] = task
        task.add_done_callback(lambda completed: self._graph_start_finished(task_key, completed))

    def _graph_start_finished(
        self, task_key: tuple[UUID, str], task: asyncio.Task[None]
    ) -> None:
        if self._graph_start_tasks.get(task_key) is task:
            del self._graph_start_tasks[task_key]
        if not task.cancelled():
            task.exception()

    async def _run_pending_graph_start(self, run_id: UUID, receipt_key: str) -> None:
        owner = self._run_owner
        if owner is None or not self._is_admission_open():
            return
        try:
            if (run_id, receipt_key) not in await self._store.list_pending_graph_starts():
                return
            receipt = await owner.load_run_created_receipt(run_id, receipt_key)
            await owner.start_graph(run_id, receipt)
            await self._store.mark_graph_start_started(run_id, receipt_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable handoff remains pending for receipt-idempotent recovery.
            return


def _unavailable() -> ApiError:
    return ApiError(
        503,
        "the requested API capability is unavailable",
        "DEPENDENCY_UNAVAILABLE",
        retryable=True,
    )


def _create_run_gate_code(error: Exception) -> str | None:
    value = getattr(error, "create_run_rejection_code", None)
    if isinstance(value, StableErrorCode):
        value = value.value
    if isinstance(value, FailureReason):
        value = value.value
    if not isinstance(value, str) or value == StableErrorCode.DEPENDENCY_UNAVAILABLE.value:
        return None
    try:
        return StableErrorCode(value).value
    except ValueError:
        if value == FailureReason.DossierInconsistent.value:
            return value
    return None


__all__ = [
    "ApiCommandStorePort",
    "ProductionApiBackend",
    "RunCreationOwnerPort",
    "RunCreatedProjection",
]
