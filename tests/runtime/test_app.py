from __future__ import annotations

import pytest

from codemigrator.runtime.app import (
    AppLifecycle,
    AppState,
    AsyncAppLifecycle,
    InMemoryAdvisoryLock,
    PostgreSQLUnavailable,
    run_from_environment,
)


class AsyncLock:
    def __init__(self):
        self.acquired = False

    async def try_acquire(self):
        self.acquired = True
        return True

    async def release(self):
        self.acquired = False


@pytest.mark.asyncio
async def test_second_instance_is_not_ready_and_performs_zero_writes():
    lock = InMemoryAdvisoryLock()
    first = AppLifecycle(lock)
    second = AppLifecycle(lock)
    await first.start()
    await second.start()
    assert first.state is AppState.Ready
    assert second.state is AppState.Exited
    assert second.write_count == 0
    await first.stop()


@pytest.mark.asyncio
async def test_lock_loss_closes_readiness_and_requests_shutdown():
    cgroup_events: list[str] = []
    app = AppLifecycle(InMemoryAdvisoryLock(), cgroup_stop=lambda: cgroup_events.append("stop"))
    await app.start()
    app.lock_connection_lost()
    assert app.ready is False
    assert app.state is AppState.Exited
    assert app.shutdown_requested is True
    assert cgroup_events == ["stop"]


@pytest.mark.asyncio
async def test_postgres_unavailable_is_not_ready():
    app = AppLifecycle(InMemoryAdvisoryLock(unavailable=True))
    await app.start()
    assert app.state is AppState.Exited
    assert app.ready is False
    assert app.last_error == PostgreSQLUnavailable.__name__


@pytest.mark.asyncio
async def test_async_composition_root_recovers_before_readiness():
    order: list[str] = []

    async def recover():
        order.append("recovery")

    lock = AsyncLock()
    app = AsyncAppLifecycle(lock, recovery=recover)
    await app.start()
    assert app.ready is True
    assert order == ["recovery"]
    await app.stop()


def test_entrypoint_requires_environment_dsn(monkeypatch):
    monkeypatch.delenv("CODEMIGRATOR_DATABASE_URL", raising=False)
    assert run_from_environment() == 1
