"""Root-level composition of the API boundary and runtime owner adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI

from codemigrator.api.backend import ApiCommandStorePort, RunCreationOwnerPort
from codemigrator.api.deps import ApiConfig
from codemigrator.api.production import (
    ApiApplicationResources,
)
from codemigrator.api.production import (
    create_production_app as _create_api_app,
)
from codemigrator.runtime.actor import RunActorFactory
from codemigrator.runtime.create_run import (
    CreateRunPreflightPort,
    RunCreationOwner,
    RunGraphStarter,
)
from codemigrator.runtime.graph_composition import (
    RuntimeGraphAssembly,
    RuntimeGraphConfigurationError,
)
from codemigrator.runtime.store import PostgreSQLRuntimeStore, RuntimeStore


@dataclass(frozen=True, slots=True)
class ProductionRunComponents:
    """Host-provided Run capabilities assembled after PostgreSQL startup."""

    preflight: CreateRunPreflightPort
    graph_assembly: RuntimeGraphAssembly
    actor_factory: RunActorFactory
    durable_checkpointer: Literal[True]

    def __post_init__(self) -> None:
        if self.preflight is None or self.graph_assembly is None:
            raise RuntimeGraphConfigurationError(
                "production Run components require preflight and graph assembly"
            )
        if not callable(self.actor_factory):
            raise RuntimeGraphConfigurationError(
                "production Run components require an actor factory"
            )
        if self.durable_checkpointer is not True:
            raise RuntimeGraphConfigurationError(
                "production Run components require durable checkpointer attestation"
            )


ProductionRunComponentsFactory = Callable[
    [RuntimeStore, asyncpg.Pool, asyncpg.Connection],
    ProductionRunComponents,
]


def create_production_run_owner(
    store: RuntimeStore, components: ProductionRunComponents
) -> RunCreationOwner:
    """Bind one validated runtime assembly to the PostgreSQL Run owner."""

    if components.graph_assembly.infrastructure.runtime_store is not store:
        raise RuntimeGraphConfigurationError(
            "production Run graph assembly must use the application RuntimeStore"
        )
    return RunCreationOwner(
        store=store,
        preflight=components.preflight,
        graph_starter=components.graph_assembly.build_run_graph_starter(
            durable_checkpointer=components.durable_checkpointer
        ),
        actor_factory=components.actor_factory,
    )


def create_production_app(
    dsn: str,
    *,
    config: ApiConfig,
    preflight: CreateRunPreflightPort | None = None,
    graph_starter: RunGraphStarter | None = None,
    run_components_factory: ProductionRunComponentsFactory | None = None,
    stop_server: Callable[[], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]] | None = None,
    pool_server_settings: Mapping[str, str] | None = None,
) -> FastAPI:
    """Bind API ports to PostgreSQLRuntimeStore and the RunActor owner adapter."""

    if run_components_factory is not None and (
        preflight is not None or graph_starter is not None
    ):
        raise ValueError(
            "production Run components cannot be combined with direct preflight/starter ports"
        )

    def store_factory(
        pool: asyncpg.Pool[asyncpg.Record],
        write_connection: asyncpg.Connection[asyncpg.Record],
    ) -> ApiCommandStorePort:
        return cast(
            ApiCommandStorePort,
            PostgreSQLRuntimeStore(pool, write_connection=write_connection),
        )

    def owner_factory(
        store: ApiCommandStorePort,
        resources: ApiApplicationResources,
    ) -> RunCreationOwnerPort | None:
        runtime_store = cast(RuntimeStore, store)
        if run_components_factory is not None:
            components = run_components_factory(
                runtime_store,
                resources.pool,
                resources.write_connection,
            )
            return cast(
                RunCreationOwnerPort,
                create_production_run_owner(runtime_store, components),
            )
        if preflight is None or graph_starter is None:
            return None
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
        stop_server=stop_server,
        pool_server_settings=pool_server_settings,
    )


__all__ = [
    "ProductionRunComponents",
    "ProductionRunComponentsFactory",
    "create_production_app",
    "create_production_run_owner",
]
