"""Application lock and readiness lifecycle for the runtime composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class PostgreSQLUnavailable(ConnectionError):
    """Raised when the control-plane dependency cannot be reached."""


class AdvisoryLockPort(Protocol):
    def try_acquire(self) -> bool:
        """Acquire the process-wide session advisory lock if available."""

    def release(self) -> None:
        """Release the session advisory lock."""


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


@dataclass
class AppLifecycle:
    lock: AdvisoryLockPort
    cgroup_stop: Callable[[], None] | None = None
    state: AppState = AppState.NotReady
    write_count: int = 0
    shutdown_requested: bool = False
    last_error: str | None = None

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


__all__ = [
    "AdvisoryLockPort",
    "AppLifecycle",
    "AppState",
    "InMemoryAdvisoryLock",
    "PostgreSQLUnavailable",
]
