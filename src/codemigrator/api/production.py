"""PostgreSQL-owned ASGI lifecycle around injected API and domain ports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI
from starlette.types import Lifespan

from .backend import (
    ApiCommandStorePort,
    ApiProductionCapabilities,
    ProductionApiBackend,
    RunCreationOwnerPort,
)
from .deps import ApiBackend, ApiConfig, ApiRequest, EventRecord
from .problems import ApiError
from .routes import create_app

_APPLICATION_LOCK_KEY = 0x436F64654D696772


@dataclass(frozen=True, slots=True)
class ApiApplicationResources:
    """PostgreSQL resources available to the runtime composition factory."""

    pool: asyncpg.Pool[asyncpg.Record]
    write_connection: asyncpg.Connection[asyncpg.Record]


class _BackendSlot:
    """Bind the concrete backend only after startup recovery succeeds."""

    def __init__(self) -> None:
        self._backend: ProductionApiBackend | None = None

    def bind(self, backend: ProductionApiBackend | None) -> None:
        self._backend = backend

    def unbind(self) -> ProductionApiBackend | None:
        backend, self._backend = self._backend, None
        return backend

    def close_admission(self) -> None:
        if self._backend is not None:
            self._backend.close_admission()

    def _require_backend(self) -> ProductionApiBackend:
        if self._backend is None:
            raise ApiError(
                503,
                "application is not ready",
                "DEPENDENCY_UNAVAILABLE",
                retryable=True,
            )
        return self._backend

    async def execute(self, request: ApiRequest) -> object:
        return await self._require_backend().execute(request)

    async def execute_idempotent(
        self,
        request: ApiRequest,
        *,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
    ) -> object:
        return await self._require_backend().execute_idempotent(
            request,
            route=route,
            key=key,
            canonical_body=canonical_body,
            status_code=status_code,
        )

    async def read_events(self, run_id: UUID, after_sequence: int) -> Sequence[EventRecord]:
        return await self._require_backend().read_events(run_id, after_sequence)

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        await self._require_backend().wait_for_events(run_id, after_sequence)

    async def is_stream_terminal(self, run_id: UUID, after_sequence: int) -> bool:
        return await self._require_backend().is_stream_terminal(run_id, after_sequence)

    async def read_session_events(
        self, session_id: UUID, after_sequence: int
    ) -> Sequence[EventRecord]:
        return await self._require_backend().read_session_events(session_id, after_sequence)

    async def wait_for_session_events(self, session_id: UUID, after_sequence: int) -> None:
        await self._require_backend().wait_for_session_events(session_id, after_sequence)

    async def is_session_stream_terminal(self, session_id: UUID, after_sequence: int) -> bool:
        return await self._require_backend().is_session_stream_terminal(
            session_id, after_sequence
        )


class _PostgreSQLApplicationLock:
    def __init__(self, dsn: str, *, server_settings: Mapping[str, str] | None = None) -> None:
        self._dsn = dsn
        self._server_settings = dict(server_settings) if server_settings is not None else None
        self._connection: asyncpg.Connection[asyncpg.Record] | None = None
        self._termination_callback: Callable[[], None] | None = None
        self._intentional_release = False

    @property
    def connection(self) -> asyncpg.Connection[asyncpg.Record] | None:
        return self._connection

    def set_termination_callback(self, callback: Callable[[], None]) -> None:
        self._termination_callback = callback

    async def acquire(self) -> bool:
        connection = await asyncpg.connect(self._dsn, server_settings=self._server_settings)
        try:
            acquired = bool(
                await connection.fetchval(
                    "SELECT pg_try_advisory_lock($1::bigint)", _APPLICATION_LOCK_KEY
                )
            )
        except Exception:
            await connection.close()
            raise
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        connection.add_termination_listener(self._on_connection_termination)
        return True

    def _on_connection_termination(
        self, connection: asyncpg.Connection[asyncpg.Record]
    ) -> None:
        if (
            connection is self._connection
            and not self._intentional_release
            and self._termination_callback is not None
        ):
            self._termination_callback()

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        self._intentional_release = True
        failure = False
        try:
            try:
                connection.remove_termination_listener(self._on_connection_termination)
            except Exception:
                failure = True
        finally:
            try:
                if not connection.is_closed():
                    await connection.fetchval(
                        "SELECT pg_advisory_unlock($1::bigint)", _APPLICATION_LOCK_KEY
                    )
            except Exception:
                failure = True
            finally:
                if not connection.is_closed():
                    try:
                        await connection.close()
                    except Exception:
                        failure = True
        if failure:
            raise RuntimeError("application lock release failed") from None


def create_production_app(
    dsn: str,
    *,
    config: ApiConfig,
    store_factory: Callable[
        [asyncpg.Pool[asyncpg.Record], asyncpg.Connection[asyncpg.Record]],
        ApiCommandStorePort,
    ],
    owner_factory: Callable[
        [ApiCommandStorePort, ApiApplicationResources],
        RunCreationOwnerPort | ApiProductionCapabilities | None,
    ],
    stop_server: Callable[[], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]] | None = None,
    pool_server_settings: Mapping[str, str] | None = None,
) -> FastAPI:
    """Create an ASGI app that owns its async pool, schema, lock, and recovery.

    A root-level composition module supplies the concrete runtime store and owner
    factories. This keeps ``codemigrator.api`` independent of ``codemigrator.runtime``.
    """

    backend_slot = _BackendSlot()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lifespan_loop = asyncio.get_running_loop()
        pool: asyncpg.Pool[asyncpg.Record] | None = None
        settings = dict(pool_server_settings) if pool_server_settings is not None else None
        lock = _PostgreSQLApplicationLock(dsn, server_settings=settings)
        backend: ProductionApiBackend | None = None
        lock_acquired = False
        lock_lost = False
        lock_loss_task: asyncio.Task[None] | None = None
        lock_loss_event = asyncio.Event()

        def on_lock_termination() -> None:
            nonlocal lock_lost, lock_loss_task
            try:
                lock_lost = True
                app.state.runtime_ready = False
                app.state.runtime_shutdown_requested = True
                backend_slot.close_admission()
                detached_backend = backend_slot.unbind()
                try:
                    lock_loss_event.set()
                except RuntimeError:
                    app.state.runtime_shutdown_error = "EventLoopUnavailable"
                if detached_backend is not None and lock_loss_task is None:
                    if lifespan_loop.is_closed():
                        app.state.runtime_shutdown_error = "EventLoopUnavailable"
                    else:
                        shutdown_coroutine = close_after_lock_loss(detached_backend)
                        try:
                            lock_loss_task = lifespan_loop.create_task(
                                shutdown_coroutine,
                                name="codemigrator-lock-loss-shutdown",
                            )
                            app.state.lock_loss_task = lock_loss_task
                        except RuntimeError:
                            shutdown_coroutine.close()
                            app.state.runtime_shutdown_error = "EventLoopUnavailable"
            except Exception:
                # Termination callbacks run on the driver boundary and must not
                # surface callback failures or leave the API accepting requests.
                app.state.runtime_ready = False
                app.state.runtime_shutdown_requested = True
                backend_slot.unbind()

        async def close_after_lock_loss(lost_backend: ProductionApiBackend) -> None:
            try:
                await lost_backend.close()
            except Exception:
                app.state.runtime_shutdown_error = "ResourceShutdownFailed"
            finally:
                try:
                    await stop_server()
                except Exception:
                    app.state.runtime_shutdown_error = "ServerStopFailed"

        try:
            pool = await asyncpg.create_pool(dsn, server_settings=settings)
            lock.set_termination_callback(on_lock_termination)
            lock_acquired = await lock.acquire()
            if not lock_acquired:
                raise RuntimeError("another CodeMigrator application instance is active")
            lock_connection = lock.connection
            if lock_connection is None:
                raise RuntimeError("application lock connection is unavailable")
            store = store_factory(pool, lock_connection)
            await store.initialize()
            async with pool.acquire() as connection:
                if await connection.fetchval("SELECT 1") != 1:
                    raise RuntimeError("PostgreSQL readiness query failed")
            owner_result = owner_factory(
                store,
                ApiApplicationResources(pool=pool, write_connection=lock_connection),
            )
            if isinstance(owner_result, ApiProductionCapabilities):
                owner = owner_result.run_owner
                run_read_projection = owner_result.run_read_projection
            else:
                owner = owner_result
                run_read_projection = None

            async def check_health() -> Mapping[str, object]:
                if not app.state.runtime_ready or pool is None or pool.is_closing():
                    raise ApiError(
                        503,
                        "application is not ready",
                        "DEPENDENCY_UNAVAILABLE",
                        retryable=True,
                    )
                async with pool.acquire(timeout=1.0) as connection:
                    if await connection.fetchval("SELECT 1") != 1:
                        raise ApiError(
                            503,
                            "PostgreSQL is unavailable",
                            "DEPENDENCY_UNAVAILABLE",
                            retryable=True,
                        )
                return {
                    "app": "READY",
                    "postgres": "READY",
                    "sandbox": "NOT_CONFIGURED",
                    "optional_profiles": {},
                }

            backend = ProductionApiBackend(
                store,
                run_owner=owner,
                run_read_projection=run_read_projection,
                shutdown=shutdown,
                health_check=check_health,
            )
            await backend.recover_pending_graph_starts()
            async with pool.acquire() as connection:
                if await connection.fetchval("SELECT 1") != 1:
                    raise RuntimeError("PostgreSQL readiness query failed")

            if lock_lost:
                raise RuntimeError("application lock was lost during startup")
            backend_slot.bind(backend)
            app.state.runtime_ready = True
            app.state.runtime_shutdown_requested = False
            app.state.runtime_pool = pool
            app.state.runtime_lock = lock
            app.state.runtime_lock_loss_event = lock_loss_event
            yield
        except Exception:
            raise RuntimeError("production API startup failed") from None
        finally:
            app.state.runtime_ready = False
            app.state.runtime_pool = None
            backend_slot.unbind()
            try:
                if lock_loss_task is not None:
                    await lock_loss_task
            finally:
                try:
                    try:
                        if backend is not None:
                            await backend.close()
                    finally:
                        if lock_acquired:
                            await lock.release()
                finally:
                    if pool is not None:
                        await pool.close()

    app = create_app(
        cast(ApiBackend, backend_slot),
        config=config,
        lifespan=cast(Lifespan[FastAPI], lifespan),
    )
    app.state.runtime_ready = False
    app.state.runtime_pool = None
    app.state.runtime_lock = None
    app.state.runtime_lock_loss_event = None
    app.state.runtime_shutdown_requested = False
    app.state.runtime_shutdown_error = None
    app.state.lock_loss_task = None
    return app


__all__ = ["create_production_app"]
