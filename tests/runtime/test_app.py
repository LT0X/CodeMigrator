from __future__ import annotations

import pytest

from codemigrator.core import SecretRegistry
from codemigrator.runtime.app import (
    AppLifecycle,
    AppState,
    AsyncAppLifecycle,
    InMemoryAdvisoryLock,
    PostgreSQLUnavailable,
    RuntimeApplication,
    _database_dsn_from_environment,
    run_from_environment,
)
from codemigrator.runtime.graph_composition import RuntimeGraphConfigurationError


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


@pytest.mark.asyncio
async def test_async_composition_root_stays_not_ready_when_readiness_check_fails():
    lock = AsyncLock()
    app = AsyncAppLifecycle(lock, readiness_check=lambda: False)

    await app.start()

    assert app.ready is False
    assert app.state is AppState.Exited
    assert app.last_error == "ObservationSentinelFailed"
    assert lock.acquired is False


def test_production_composition_root_always_binds_observation_readiness():
    registry = SecretRegistry()
    registry.register("sentinel-secret")
    application = RuntimeApplication.from_dsn(
        "postgresql://localhost/codemigrator",
        secret_registry=registry,
        sentinel_outputs={"stdout": "sentinel-secret"},
    )

    assert application.lifecycle.readiness_check is not None
    assert application.lifecycle.readiness_check() is False


def test_application_fails_closed_when_workflow_graphs_are_not_configured():
    application = RuntimeApplication.from_dsn("postgresql://localhost/codemigrator")

    with pytest.raises(RuntimeGraphConfigurationError, match="graph assembly is not configured"):
        application.build_run_graph(object())

    with pytest.raises(RuntimeGraphConfigurationError, match="graph assembly is not configured"):
        application.build_draft_graph(object())


def test_entrypoint_requires_environment_dsn(monkeypatch):
    monkeypatch.delenv("CODEMIGRATOR_DATABASE_URL", raising=False)
    for name in (
        "CODEMIGRATOR_DATABASE_HOST",
        "CODEMIGRATOR_DATABASE_NAME",
        "CODEMIGRATOR_DATABASE_USER",
        "CODEMIGRATOR_DATABASE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    assert run_from_environment() == 1


def test_database_dsn_escapes_component_environment_values():
    dsn = _database_dsn_from_environment(
        {
            "CODEMIGRATOR_DATABASE_HOST": "db.internal",
            "CODEMIGRATOR_DATABASE_NAME": "code/migrator",
            "CODEMIGRATOR_DATABASE_USER": "migrator@service",
            "CODEMIGRATOR_DATABASE_PASSWORD": "p@ss:/?#[]",
            "CODEMIGRATOR_DATABASE_PORT": "5544",
        }
    )

    assert dsn == (
        "postgresql://migrator%40service:p%40ss%3A%2F%3F%23%5B%5D"
        "@db.internal:5544/code%2Fmigrator"
    )
    ipv6_dsn = _database_dsn_from_environment(
        {
            "CODEMIGRATOR_DATABASE_HOST": "::1",
            "CODEMIGRATOR_DATABASE_NAME": "codemigrator",
            "CODEMIGRATOR_DATABASE_USER": "codemigrator",
            "CODEMIGRATOR_DATABASE_PASSWORD": "password",
        }
    )
    assert ipv6_dsn == "postgresql://codemigrator:password@[::1]:5432/codemigrator"


def test_database_dsn_rejects_incomplete_or_invalid_component_configuration():
    assert _database_dsn_from_environment({"CODEMIGRATOR_DATABASE_HOST": "postgres"}) is None
    assert (
        _database_dsn_from_environment(
            {
                "CODEMIGRATOR_DATABASE_HOST": "postgres",
                "CODEMIGRATOR_DATABASE_NAME": "codemigrator",
                "CODEMIGRATOR_DATABASE_USER": "codemigrator",
                "CODEMIGRATOR_DATABASE_PASSWORD": "secret",
                "CODEMIGRATOR_DATABASE_PORT": "70000",
            }
        )
        is None
    )
    assert (
        _database_dsn_from_environment(
            {
                "CODEMIGRATOR_DATABASE_HOST": "db host",
                "CODEMIGRATOR_DATABASE_NAME": "codemigrator",
                "CODEMIGRATOR_DATABASE_USER": "codemigrator",
                "CODEMIGRATOR_DATABASE_PASSWORD": "secret",
            }
        )
        is None
    )
    assert (
        _database_dsn_from_environment(
            {
                "CODEMIGRATOR_DATABASE_HOST": "[not-an-ipv6-address]",
                "CODEMIGRATOR_DATABASE_NAME": "codemigrator",
                "CODEMIGRATOR_DATABASE_USER": "codemigrator",
                "CODEMIGRATOR_DATABASE_PASSWORD": "secret",
            }
        )
        is None
    )


def test_entrypoint_requires_api_token(monkeypatch):
    monkeypatch.setenv("CODEMIGRATOR_DATABASE_URL", "postgresql://localhost/codemigrator")
    monkeypatch.delenv("CODEMIGRATOR_API_TOKEN", raising=False)

    assert run_from_environment() == 1


def test_entrypoint_runs_production_asgi_app_with_locked_configuration(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setenv("CODEMIGRATOR_DATABASE_URL", "postgresql://localhost/codemigrator")
    monkeypatch.setenv("CODEMIGRATOR_API_TOKEN", "test-api-token")
    monkeypatch.setenv("CODEMIGRATOR_HTTP_HOST", "127.0.0.1")
    monkeypatch.setenv("CODEMIGRATOR_HTTP_PORT", "8123")

    app_marker = object()
    app_factory_calls: list[tuple[object, object, object]] = []
    server_instances: list[object] = []

    def create_production_app(dsn, *, config, stop_server):
        app_factory_calls.append((dsn, config, stop_server))
        return app_marker

    class FakeConfig:
        def __init__(self, app, *, host, port, log_level):
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            server_instances.append(self)

        async def serve(self):
            self.started = True
            await app_factory_calls[0][2]()

    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )
    monkeypatch.setattr("codemigrator.asgi.create_production_app", create_production_app)

    assert run_from_environment() == 0

    assert len(app_factory_calls) == 1
    dsn, config, stop_server = app_factory_calls[0]
    assert dsn == "postgresql://localhost/codemigrator"
    assert config.token == "test-api-token"
    assert callable(stop_server)
    assert len(server_instances) == 1
    server = server_instances[0]
    assert server.config.app is app_marker
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8123
    assert server.should_exit is True
