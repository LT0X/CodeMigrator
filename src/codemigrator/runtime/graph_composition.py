"""Fail-closed assembly of the persistent Draft and Run graph families."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver

from .actor import RunActor
from .cas import CasReferenceStore, FileHostCAS
from .checkpointer import CasCheckpointSaver
from .create_run import CreateRunService, RunWorkflowGraphStarter
from .draft_graph import (
    DraftAgentRunnerPort,
    DraftOwnerPort,
    MigrationSessionGraph,
)
from .langchain_agent import AgentUsageSink
from .loop import ToolGatewayPort
from .memory import ContextManager
from .provider import ProviderRegistry
from .run_graph import (
    DeterministicStagePort,
    PlanStagePort,
    RunGraphActorPort,
    RunWorkflowGraph,
)
from .store import RuntimeStore


class RuntimeGraphConfigurationError(ValueError):
    """Required graph infrastructure is absent or shares an unsafe boundary."""


@dataclass(frozen=True, slots=True)
class AgentGraphInfrastructure:
    """Shared runtime capabilities; graphs and AgentRuns use separate savers."""

    provider_registry: ProviderRegistry
    context_manager: ContextManager
    tool_gateway: ToolGatewayPort
    runtime_store: RuntimeStore
    host_cas: FileHostCAS
    cas_references: CasReferenceStore
    usage_sink: AgentUsageSink
    run_checkpointer: BaseCheckpointSaver[Any]
    draft_graph_checkpointer: BaseCheckpointSaver[Any]
    agent_run_checkpointer: BaseCheckpointSaver[Any]

    def __post_init__(self) -> None:
        for field_name in (
            "provider_registry",
            "context_manager",
            "tool_gateway",
            "runtime_store",
            "host_cas",
            "cas_references",
            "usage_sink",
            "run_checkpointer",
            "draft_graph_checkpointer",
            "agent_run_checkpointer",
        ):
            if getattr(self, field_name) is None:
                raise RuntimeGraphConfigurationError(
                    f"runtime graph infrastructure requires {field_name}"
                )
        savers = (
            self.run_checkpointer,
            self.draft_graph_checkpointer,
            self.agent_run_checkpointer,
        )
        if len({id(saver) for saver in savers}) != len(savers):
            raise RuntimeGraphConfigurationError(
                "Run, Draft, and AgentRun require isolated checkpointer instances"
            )

    def validate_durable_persistence_bindings(self) -> None:
        """Keep production CAS and checkpoint writes on the application store."""

        if cast(object, self.cas_references) is not self.runtime_store:
            raise RuntimeGraphConfigurationError(
                "CAS reference store must use the application RuntimeStore"
            )
        for name, saver in (
            ("run_checkpointer", self.run_checkpointer),
            ("draft_graph_checkpointer", self.draft_graph_checkpointer),
            ("agent_run_checkpointer", self.agent_run_checkpointer),
        ):
            if not isinstance(saver, CasCheckpointSaver):
                raise RuntimeGraphConfigurationError(
                    f"production {name} must use CasCheckpointSaver"
                )
            if cast(object, saver.store) is not self.runtime_store:
                raise RuntimeGraphConfigurationError(
                    f"production {name} must use the application RuntimeStore"
                )
            if saver.cas is not self.host_cas:
                raise RuntimeGraphConfigurationError(
                    f"production {name} must use the application host CAS"
                )


PlanStageFactory = Callable[[AgentGraphInfrastructure, RunGraphActorPort], PlanStagePort]
DeterministicStageFactory = Callable[
    [AgentGraphInfrastructure, RunGraphActorPort], DeterministicStagePort
]
DraftAgentRunnerFactory = Callable[
    [AgentGraphInfrastructure, DraftOwnerPort], DraftAgentRunnerPort
]
CreateRunServiceFactory = Callable[
    [AgentGraphInfrastructure, DraftOwnerPort], CreateRunService
]


class RuntimeGraphAssembly:
    """Compile both graph families from one explicit, validated dependency bundle."""

    def __init__(
        self,
        infrastructure: AgentGraphInfrastructure,
        *,
        plan_stage_factory: PlanStageFactory,
        verifier_factory: DeterministicStageFactory,
        reporter_factory: DeterministicStageFactory,
        draft_agent_runner_factory: DraftAgentRunnerFactory,
        create_run_service_factory: CreateRunServiceFactory,
    ) -> None:
        self.infrastructure = infrastructure
        factories = {
            "plan_stage_factory": plan_stage_factory,
            "verifier_factory": verifier_factory,
            "reporter_factory": reporter_factory,
            "draft_agent_runner_factory": draft_agent_runner_factory,
            "create_run_service_factory": create_run_service_factory,
        }
        for name, factory in factories.items():
            if not callable(factory):
                raise RuntimeGraphConfigurationError(f"runtime graph requires {name}")
        self.plan_stage_factory = plan_stage_factory
        self.verifier_factory = verifier_factory
        self.reporter_factory = reporter_factory
        self.draft_agent_runner_factory = draft_agent_runner_factory
        self.create_run_service_factory = create_run_service_factory

    def build_run_graph(self, actor: RunGraphActorPort) -> RunWorkflowGraph:
        planner = self.plan_stage_factory(self.infrastructure, actor)
        verifier = self.verifier_factory(self.infrastructure, actor)
        reporter = self.reporter_factory(self.infrastructure, actor)
        if planner is None or verifier is None or reporter is None:
            raise RuntimeGraphConfigurationError("Run graph stage factory returned no component")
        return RunWorkflowGraph(
            actor=actor,
            planner=planner,
            verifier=verifier,
            reporter=reporter,
            checkpointer=self.infrastructure.run_checkpointer,
        )

    def build_run_graph_starter(
        self, *, durable_checkpointer: Literal[True]
    ) -> RunWorkflowGraphStarter:
        """Create an idempotent starter bound to this assembly's Run graph factory.

        The host attests that the injected saver persists across process restarts;
        an in-memory saver is valid for tests but cannot satisfy this production gate.
        """

        if durable_checkpointer is not True:
            raise ValueError("Run graph recovery requires a durable checkpointer")
        self.infrastructure.validate_durable_persistence_bindings()
        graph_factory = cast(
            Callable[[RunActor], RunWorkflowGraph], self.build_run_graph
        )
        return RunWorkflowGraphStarter(
            graph_factory,
            durable_checkpointer=durable_checkpointer,
        )

    def build_draft_graph(self, owner: DraftOwnerPort) -> MigrationSessionGraph:
        if not isinstance(owner.draft_id, UUID):
            raise RuntimeGraphConfigurationError("Draft graph owner must expose a UUID DraftId")
        runner = self.draft_agent_runner_factory(self.infrastructure, owner)
        create_run_service = self.create_run_service_factory(self.infrastructure, owner)
        if runner is None or create_run_service is None:
            raise RuntimeGraphConfigurationError("Draft graph factory returned no component")
        return MigrationSessionGraph(
            owner=owner,
            agent_runs=self.infrastructure.runtime_store,
            checkpointer=self.infrastructure.draft_graph_checkpointer,
            agent_checkpointer=self.infrastructure.agent_run_checkpointer,
            create_run_service=create_run_service,
            agent_runner=runner,
        )


__all__ = [
    "AgentGraphInfrastructure",
    "RuntimeGraphAssembly",
    "RuntimeGraphConfigurationError",
]
