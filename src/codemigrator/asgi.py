"""Root-level composition of the API boundary and runtime owner adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import cast

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI

from codemigrator.api.backend import ApiCommandStorePort, RunCreationOwnerPort
from codemigrator.api.deps import ApiConfig
from codemigrator.api.production import create_production_app as _create_api_app
from codemigrator.runtime.create_run import (
    CreateRunPreflightPort,
    RunCreationOwner,
    RunGraphStarter,
)
from codemigrator.runtime.store import PostgreSQLRuntimeStore, RuntimeStore


def create_production_app(
    dsn: str,
    *,
    config: ApiConfig,
    preflight: CreateRunPreflightPort | None = None,
    graph_starter: RunGraphStarter | None = None,
    shutdown: Callable[[], Awaitable[None]] | None = None,
    pool_server_settings: Mapping[str, str] | None = None,
) -> FastAPI:
    """Bind API ports to PostgreSQLRuntimeStore and the RunActor owner adapter."""

    def store_factory(pool: asyncpg.Pool[asyncpg.Record]) -> ApiCommandStorePort:
        return cast(ApiCommandStorePort, PostgreSQLRuntimeStore(pool))

    def owner_factory(store: ApiCommandStorePort) -> RunCreationOwnerPort | None:
        if preflight is None or graph_starter is None:
            return None
        runtime_store = cast(RuntimeStore, store)
        return cast(
            RunCreationOwnerPort,
            RunCreationOwner(
                store=runtime_store,
                preflight=preflight,
                graph_starter=graph_starter,
            ),
        )

    return _create_api_app(
        dsn,
        config=config,
        store_factory=store_factory,
        owner_factory=owner_factory,
        shutdown=shutdown,
        pool_server_settings=pool_server_settings,
    )


__all__ = ["create_production_app"]
