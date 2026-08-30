"""Application lock and readiness lifecycle for the runtime composition root."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import asyncpg  # type: ignore[import-untyped]

from codemigrator.core import SecretRegistry

from .observability import DEFAULT_SENTINEL_SINKS, SentinelSuite


class PostgreSQLUnavailable(ConnectionError):
    """Raised when the control-plane dependency cannot be reached."""


class AdvisoryLockPort(Protocol):
    def try_acquire(self) -> bool:
        """Acquire the process-wide session advisory lock if available."""

    def release(self) -> None:
        """Release the session advisory lock."""


class AsyncAdvisoryLockPort(Protocol):
    async def try_acquire(self) -> bool:
        """Acquire a lock using an async, dedicated database session."""

    async def release(self) -> None:
        """Release the session advisory lock and close its connection."""


class AppState(str, Enum):
    NotReady = "NOT_READY"
    Starting = "STARTING"
    Ready = "READY"
    Stopping = "STOPPING"
    Exited = "EXITED"


class InMemoryAdvisoryLock:
    """Deterministic stand-in for a PostgreSQL session advisory lock."""

    _held = False

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.acquired = False

    def try_acquire(self) -> bool:
        if self.unavailable:
            raise PostgreSQLUnavailable("postgresql unavailable")
        if self.acquired or InMemoryAdvisoryLock._held:
            return False
        InMemoryAdvisoryLock._held = True
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self.acquired = False
            InMemoryAdvisoryLock._held = False


class PostgreSQLAdvisoryLock:
    """A session-scoped advisory lock backed by a dedicated PostgreSQL connection."""

    def __init__(self, dsn: str, *, key: int = 0x436F64654D696772) -> None:
        self.dsn = dsn
        self.key = key
        self._connection: Any | None = None

    async def try_acquire(self) -> bool:
        try:
            connection = await asyncpg.connect(self.dsn)
            acquired = bool(
                await connection.fetchval("SELECT pg_try_advisory_lock($1::bigint)", self.key)
            )
        except (OSError, asyncpg.PostgresError) as exc:
            raise PostgreSQLUnavailable("postgresql unavailable") from exc
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def release(self) -> None:
        if self._connection is None:
            return
        connection, self._connection = self._connection, None
        try:
            await connection.fetchval("SELECT pg_advisory_unlock($1::bigint)", self.key)
        finally:
            await connection.close()

    def connection_alive(self) -> bool:
        return self._connection is not None and not self._connection.is_closed()


@dataclass
class AppLifecycle:
    lock: AdvisoryLockPort
    cgroup_stop: Callable[[], None] | None = None
    state: AppState = AppState.NotReady
    write_count: int = 0
    shutdown_requested: bool = False
    last_error: str | None = None
    readiness_check: Callable[[], bool] | None = None

    @property
    def ready(self) -> bool:
        return self.state is AppState.Ready

    async def start(self) -> None:
        self.state = AppState.Starting
        try:
            acquired = self.lock.try_acquire()
        except PostgreSQLUnavailable as exc:
            self.last_error = type(exc).__name__
            self.state = AppState.Exited
            return
        if not acquired:
            self.state = AppState.Exited
            return
        if self.readiness_check is not None:
            try:
                ready = self.readiness_check()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.lock.release()
                self.state = AppState.Exited
                return
            if not ready:
                self.last_error = "ObservationSentinelFailed"
                self.lock.release()
                self.state = AppState.Exited
                return
        self.write_count += 1
        self.state = AppState.Ready

    def lock_connection_lost(self) -> None:
        if self.state is not AppState.Ready:
            return
        self.state = AppState.Stopping
        self.shutdown_requested = True
        if self.cgroup_stop is not None:
            self.cgroup_stop()
        self.lock.release()
        self.state = AppState.Exited

    async def stop(self) -> None:
        if self.state is AppState.Ready:
            self.state = AppState.Stopping
            self.lock.release()
        self.state = AppState.Exited


@dataclass
class AsyncAppLifecycle:
    """Async lifecycle used by the production application composition root."""

    lock: AsyncAdvisoryLockPort
    recovery: Callable[[], Awaitable[None]] | None = None
    cgroup_stop: Callable[[], None] | None = None
    state: AppState = AppState.NotReady
    shutdown_requested: bool = False
    last_error: str | None = None
    readiness_check: Callable[[], bool | Awaitable[bool]] | None = None

    @property
    def ready(self) -> bool:
        return self.state is AppState.Ready

    async def start(self) -> None:
        self.state = AppState.Starting
        try:
            acquired = await self.lock.try_acquire()
        except PostgreSQLUnavailable as exc:
            self.last_error = type(exc).__name__
            self.state = AppState.Exited
            return
        if not acquired:
            self.state = AppState.Exited
            return
        try:
            if self.recovery is not None:
                await self.recovery()
        except Exception as exc:
            self.last_error = type(exc).__name__
            await self.lock.release()
            self.state = AppState.Exited
            return
        if self.readiness_check is not None:
            try:
                ready = self.readiness_check()
                if isinstance(ready, Awaitable):
                    ready = await ready
            except Exception as exc:
                self.last_error = type(exc).__name__
                await self.lock.release()
                self.state = AppState.Exited
                return
            if not ready:
                self.last_error = "ObservationSentinelFailed"
                await self.lock.release()
                self.state = AppState.Exited
                return
        self.state = AppState.Ready

    async def lock_connection_lost(self) -> None:
        if self.state is not AppState.Ready:
            return
        self.state = AppState.Stopping
        self.shutdown_requested = True
        if self.cgroup_stop is not None:
            self.cgroup_stop()
        await self.lock.release()
        self.state = AppState.Exited

    async def stop(self) -> None:
        if self.state is AppState.Ready:
            self.state = AppState.Stopping
            await self.lock.release()
        self.state = AppState.Exited


@dataclass
class RuntimeApplication:
    """Small production composition root; adapters are supplied at construction."""

    lifecycle: AsyncAppLifecycle

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        secret_registry: SecretRegistry | None = None,
        sentinel_outputs: Mapping[str, object] | None = None,
    ) -> RuntimeApplication:
        registry = secret_registry or SecretRegistry()
        sentinel = SentinelSuite(registry)
        outputs = (
            dict(sentinel_outputs)
            if sentinel_outputs is not None
            else {sink: {} for sink in DEFAULT_SENTINEL_SINKS}
        )

        def readiness_check() -> bool:
            return sentinel.run(outputs).passed

        return cls(
            AsyncAppLifecycle(
                PostgreSQLAdvisoryLock(dsn),
                readiness_check=readiness_check,
            )
        )

    async def run(self) -> int:
        await self.lifecycle.start()
        if not self.lifecycle.ready:
            return 1
        await asyncio.Event().wait()
        return 0


def run_from_environment() -> int:
    """Start the application using a DSN supplied by the deployment environment."""

    dsn = os.environ.get("CODEMIGRATOR_DATABASE_URL")
    if not dsn:
        return 1
    return asyncio.run(RuntimeApplication.from_dsn(dsn).run())


__all__ = [
    "AsyncAdvisoryLockPort",
    "AsyncAppLifecycle",
    "AdvisoryLockPort",
    "AppLifecycle",
    "AppState",
    "InMemoryAdvisoryLock",
    "PostgreSQLAdvisoryLock",
    "PostgreSQLUnavailable",
    "RuntimeApplication",
    "run_from_environment",
]
