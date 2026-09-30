"""PostgreSQL-owned ASGI lifecycle around injected API and domain ports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI
from starlette.types import Lifespan

from .backend import ApiCommandStorePort, ProductionApiBackend, RunCreationOwnerPort
from .deps import ApiBackend, ApiConfig, ApiRequest, EventRecord
from .problems import ApiError
from .routes import create_app

_APPLICATION_LOCK_KEY = 0x436F64654D696772


class _BackendSlot:
    """Bind the concrete backend only after startup recovery succeeds."""

    def __init__(self) -> None:
        self._backend: ProductionApiBackend | None = None

    def bind(self, backend: ProductionApiBackend | None) -> None:
        self._backend = backend

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
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._connection: asyncpg.Connection[asyncpg.Record] | None = None

    async def acquire(self) -> bool:
        connection = await asyncpg.connect(self._dsn)
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
        return True

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            await connection.fetchval(
                "SELECT pg_advisory_unlock($1::bigint)", _APPLICATION_LOCK_KEY
            )
        finally:
            await connection.close()


def create_production_app(
    dsn: str,
    *,
    config: ApiConfig,
    store_factory: Callable[[asyncpg.Pool[asyncpg.Record]], ApiCommandStorePort],
    owner_factory: Callable[[ApiCommandStorePort], RunCreationOwnerPort | None],
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
        pool: asyncpg.Pool[asyncpg.Record] | None = None
        lock = _PostgreSQLApplicationLock(dsn)
        backend: ProductionApiBackend | None = None
        lock_acquired = False
        try:
            settings = dict(pool_server_settings) if pool_server_settings is not None else None
            pool = await asyncpg.create_pool(dsn, server_settings=settings)
            lock_acquired = await lock.acquire()
            if not lock_acquired:
                raise RuntimeError("another CodeMigrator application instance is active")
            store = store_factory(pool)
            await store.initialize()
            async with pool.acquire() as connection:
                if await connection.fetchval("SELECT 1") != 1:
                    raise RuntimeError("PostgreSQL readiness query failed")
            owner = owner_factory(store)
            backend = ProductionApiBackend(store, run_owner=owner, shutdown=shutdown)
            await backend.recover_pending_graph_starts()
            async with pool.acquire() as connection:
                if await connection.fetchval("SELECT 1") != 1:
                    raise RuntimeError("PostgreSQL readiness query failed")

            backend_slot.bind(backend)
            app.state.runtime_ready = True
            app.state.runtime_pool = pool
            yield
        except Exception:
            raise RuntimeError("production API startup failed") from None
        finally:
            app.state.runtime_ready = False
            app.state.runtime_pool = None
            backend_slot.bind(None)
            try:
                if backend is not None:
                    await backend.close()
            finally:
                try:
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
    return app


__all__ = ["create_production_app"]
