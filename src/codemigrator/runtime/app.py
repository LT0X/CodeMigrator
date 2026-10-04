"""Application lock and readiness lifecycle for the runtime composition root."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.parse import quote, urlunsplit

import asyncpg  # type: ignore[import-untyped]

from codemigrator.core import SecretRegistry

from .draft_graph import DraftOwnerPort, MigrationSessionGraph
from .graph_composition import RuntimeGraphAssembly, RuntimeGraphConfigurationError
from .observability import DEFAULT_SENTINEL_SINKS, SentinelSuite
from .run_graph import RunGraphActorPort, RunWorkflowGraph


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
    graph_assembly: RuntimeGraphAssembly | None = None

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        graph_assembly: RuntimeGraphAssembly | None = None,
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
            ),
            graph_assembly,
        )

    def build_run_graph(self, actor: RunGraphActorPort) -> RunWorkflowGraph:
        if self.graph_assembly is None:
            raise RuntimeGraphConfigurationError("runtime graph assembly is not configured")
        return self.graph_assembly.build_run_graph(actor)

    def build_draft_graph(self, owner: DraftOwnerPort) -> MigrationSessionGraph:
        if self.graph_assembly is None:
            raise RuntimeGraphConfigurationError("runtime graph assembly is not configured")
        return self.graph_assembly.build_draft_graph(owner)

    async def run(self) -> int:
        await self.lifecycle.start()
        if not self.lifecycle.ready:
            return 1
        await asyncio.Event().wait()
        return 0


def run_from_environment() -> int:
    """Run the production ASGI application from deployment-provided configuration."""

    dsn = _database_dsn_from_environment(os.environ)
    token = os.environ.get("CODEMIGRATOR_API_TOKEN")
    if not dsn or not token:
        return 1

    import uvicorn

    from codemigrator.api.deps import ApiConfig
    from codemigrator.asgi import create_production_app

    server_ref: list[Any] = []

    async def stop_server() -> None:
        if server_ref:
            server_ref[0].should_exit = True

    app = create_production_app(
        dsn,
        config=ApiConfig(token=token),
        stop_server=stop_server,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=os.environ.get("CODEMIGRATOR_HTTP_HOST", "0.0.0.0"),
            port=_http_port_from_environment(os.environ),
            log_level=os.environ.get("CODEMIGRATOR_LOG_LEVEL", "info"),
        )
    )
    server_ref.append(server)

    async def serve() -> int:
        await server.serve()
        return 0 if server.started else 1

    return asyncio.run(serve())


def _database_dsn_from_environment(environ: Mapping[str, str]) -> str | None:
    configured_dsn = environ.get("CODEMIGRATOR_DATABASE_URL")
    if configured_dsn:
        return configured_dsn

    host = environ.get("CODEMIGRATOR_DATABASE_HOST")
    database = environ.get("CODEMIGRATOR_DATABASE_NAME")
    username = environ.get("CODEMIGRATOR_DATABASE_USER")
    password = environ.get("CODEMIGRATOR_DATABASE_PASSWORD")
    if not host or not database or not username or not password:
        return None

    try:
        port = int(environ.get("CODEMIGRATOR_DATABASE_PORT", "5432"))
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    authority_host = _database_authority_host(host)
    if authority_host is None:
        return None

    authority = (
        f"{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{authority_host}:{port}"
    )
    return urlunsplit(("postgresql", authority, f"/{quote(database, safe='')}", "", ""))


def _database_authority_host(host: str) -> str | None:
    bracketed = host.startswith("[") or host.endswith("]")
    if bracketed and not (host.startswith("[") and host.endswith("]")):
        return None
    candidate = host[1:-1] if bracketed else host

    if ":" in candidate:
        if "%" in candidate:
            return None
        try:
            ipaddress.IPv6Address(candidate)
        except ValueError:
            return None
        return f"[{candidate}]"
    if bracketed:
        return None

    hostname = candidate[:-1] if candidate.endswith(".") else candidate
    if not hostname or len(hostname) > 253:
        return None
    labels = hostname.split(".")
    if any(
        len(label) > 63
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
        for label in labels
    ):
        return None
    return host


def _http_port_from_environment(environ: Mapping[str, str]) -> int:
    try:
        port = int(environ.get("CODEMIGRATOR_HTTP_PORT", "8080"))
    except ValueError as error:
        raise ValueError("CODEMIGRATOR_HTTP_PORT must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError("CODEMIGRATOR_HTTP_PORT must be between 1 and 65535")
    return port


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
