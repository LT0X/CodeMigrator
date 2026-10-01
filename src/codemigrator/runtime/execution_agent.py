"""Persistent LangChain AgentRun sessions for frozen EXECUTE Slice work."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol, cast
from uuid import UUID, uuid4

from codemigrator.core import (
    ContextPackIdentity,
    MigrationSlice,
    ModelProfile,
    Phase,
    PlanEdgeKind,
    RunId,
    RunStatus,
    SessionKind,
    Sha256,
    SliceGenerationRef,
    SliceKind,
    WriteScope,
    canonical_json_bytes,
)
from codemigrator.planning import FrozenPlan
from codemigrator.workspace import CheckpointReceipt, checkpoint_receipt_digest

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .binding import LockedModelBinding
from .cas import CasObject, FileHostCAS
from .context import ContextEnvelope
from .contracts import (
    ActorPhaseReceipt,
    CandidateCheckpointClaim,
    ExecutionRoundDecision,
)
from .graph_composition import AgentGraphInfrastructure
from .langchain_agent import (
    BoundAgentRun,
    agent_context_digest,
    agent_template_digest,
    agent_tool_definitions,
    agent_toolset_digest,
    create_bound_agent,
)
from .loop import ToolGatewayPort
from .loop_contracts import SessionExit, SessionState
from .memory import ContextBudgetError
from .recovery import write_scope_digest
from .scheduler import FairScheduler, ReadySlice, ResourcePool
from .store import RuntimeStore, StoreCommitError


class ExecutionCandidateCheckpointPort(Protocol):
    """Commit one completed write session through the M-08 owner service."""

    async def checkpoint(
        self, record: AgentRun, material: ExecutionSessionMaterial
    ) -> CheckpointReceipt: ...


class ExecutionGatewayFactory(Protocol):
    """Bind a ToolGateway to one persisted AgentRun and frozen Slice material."""

    def __call__(
        self, record: AgentRun, material: ExecutionSessionMaterial
    ) -> ExecutionToolGatewayPort: ...


class ExecutionToolGatewayPort(ToolGatewayPort, Protocol):
    @property
    def write_scope(self) -> WriteScope | None: ...


@dataclass(frozen=True, slots=True, init=False)
class ExecutionSessionMaterial:
    """Detached prompt, Slice and context snapshot for one write AgentRun."""

    run_id: RunId
    _slice_payload: bytes
    slice_ref: SliceGenerationRef
    logical_task_key: str
    binding: LockedModelBinding
    context_identity: ContextPackIdentity
    envelope: ContextEnvelope
    task: str
    candidate_checkpoint: ExecutionCandidateCheckpointPort
    restarted_from: AgentRunId | None
    continuation_of: AgentRunId | None

    def __init__(
        self,
        *,
        run_id: RunId,
        slice_: MigrationSlice,
        slice_ref: SliceGenerationRef,
        logical_task_key: str,
        binding: LockedModelBinding,
        context_identity: ContextPackIdentity,
        envelope: ContextEnvelope,
        task: str,
        candidate_checkpoint: ExecutionCandidateCheckpointPort,
        restarted_from: AgentRunId | None = None,
        continuation_of: AgentRunId | None = None,
    ) -> None:
        if not isinstance(run_id, UUID) or not isinstance(slice_, MigrationSlice):
            raise TypeError("EXECUTE material requires a RunId and frozen MigrationSlice")
        if not isinstance(slice_ref, SliceGenerationRef):
            raise TypeError("EXECUTE material requires a typed SliceGenerationRef")
        expected_session = _SESSION_KIND_BY_SLICE.get(slice_.kind)
        if (
            expected_session is None
            or slice_ref.slice_id != slice_.id
            or slice_ref.baseline_candidate_oid is None
            or context_identity.run_id != run_id
            or context_identity.phase is not Phase.Execute
            or context_identity.session is not expected_session
            or context_identity.slice != slice_ref
            or context_identity.model_binding_sha256 != binding.digest
            or binding.profile is not ModelProfile.Code
        ):
            raise ValueError("EXECUTE material identities do not match the frozen Slice")
        task_prefix = f"execute:{slice_.id}:g{slice_ref.generation}"
        if (
            not logical_task_key.startswith(task_prefix)
            or len(logical_task_key) > 256
            or (
                logical_task_key != task_prefix
                and not logical_task_key.startswith(task_prefix + ":")
            )
        ):
            raise ValueError(
                "EXECUTE AgentRun logical task key does not match its Slice generation"
            )
        if not isinstance(task, str) or not task.strip():
            raise ValueError("EXECUTE AgentRun task must be non-empty text")
        if not callable(getattr(candidate_checkpoint, "checkpoint", None)):
            raise TypeError("EXECUTE material requires an M-08 candidate checkpoint port")
        if restarted_from is not None and continuation_of is not None:
            raise ValueError("EXECUTE AgentRun lineage cannot restart and continue at once")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(
            self,
            "_slice_payload",
            canonical_json_bytes(slice_.model_dump(mode="json", by_alias=True)),
        )
        object.__setattr__(self, "slice_ref", slice_ref)
        object.__setattr__(self, "logical_task_key", logical_task_key)
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "context_identity", context_identity)
        object.__setattr__(self, "envelope", envelope)
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "candidate_checkpoint", candidate_checkpoint)
        object.__setattr__(self, "restarted_from", restarted_from)
        object.__setattr__(self, "continuation_of", continuation_of)

    @property
    def slice(self) -> MigrationSlice:
        """Return a fresh typed view so callers cannot mutate the captured plan."""

        return MigrationSlice.model_validate_json(self._slice_payload)


@dataclass(frozen=True, slots=True)
class ExecutionAgentOutcome:
    """Terminal AgentRun facts and the optional, independently verified M-08 claim."""

    record: AgentRun
    candidate_receipt: CheckpointReceipt | None


@dataclass(frozen=True, slots=True)
class ExecutionRecoveredTerminal:
    """An Actor-committed AgentRun terminal awaiting its EXECUTE round receipt."""

    record: AgentRun
    candidate_receipt: CheckpointReceipt | None


class ExecutionAgentSessionPort(Protocol):
    agent_run: AgentRun

    async def run(
        self,
        *,
        on_agent_run_started: Callable[[RunId, AgentRunId], Awaitable[ActorPhaseReceipt]],
        on_agent_run_terminal: Callable[
            [RunId, AgentRun, AgentRunReceipt, CasObject], Awaitable[ActorPhaseReceipt]
        ],
    ) -> ExecutionAgentOutcome: ...


@dataclass(frozen=True, slots=True)
class ExecutionWorkItem:
    """One immutable M-07 Slice dispatch admitted to the round scheduler."""

    ready: ReadySlice
    material: ExecutionSessionMaterial

    def __post_init__(self) -> None:
        if not isinstance(self.ready, ReadySlice) or not isinstance(
            self.material, ExecutionSessionMaterial
        ):
            raise TypeError("execution work item requires ready metadata and frozen material")
        slice_ = self.material.slice
        if (
            self.ready.run_id != str(self.material.run_id)
            or self.ready.slice_id != str(slice_.id)
            or self.ready.generation != self.material.slice_ref.generation
            or self.ready.write_scope
            != frozenset(
                str(path)
                for path in (
                    *slice_.write_scope.out.write_paths,
                    *slice_.write_scope.out.create_roots,
                )
            )
        ):
            raise ValueError("ready Slice metadata differs from its frozen material")


@dataclass(frozen=True, slots=True)
class ExecutionRoundPlan:
    """A host view of frozen work and Actor-committed execution facts.

    ``completed_slice_ids`` is retained for compatibility; it means formally
    integrated Slices, never candidate checkpoints or completed AgentRuns.
    """

    run_id: RunId
    work_items: tuple[ExecutionWorkItem, ...]
    completed_slice_ids: frozenset[str]
    all_slice_ids: frozenset[str]
    available_pools: frozenset[ResourcePool]
    recovered_terminals: tuple[ExecutionRecoveredTerminal, ...] = ()
    terminal_slice_failures: frozenset[str] = frozenset()
    pending_integrations: frozenset[tuple[str, int]] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_items", tuple(self.work_items))
        object.__setattr__(self, "completed_slice_ids", frozenset(self.completed_slice_ids))
        object.__setattr__(self, "all_slice_ids", frozenset(self.all_slice_ids))
        object.__setattr__(self, "available_pools", frozenset(self.available_pools))
        object.__setattr__(self, "recovered_terminals", tuple(self.recovered_terminals))
        object.__setattr__(self, "terminal_slice_failures", frozenset(self.terminal_slice_failures))
        object.__setattr__(self, "pending_integrations", frozenset(self.pending_integrations))
        ids = [item.ready.slice_id for item in self.work_items]
        recovered_ids = [
            str(terminal.record.slice_ref.slice_id)
            for terminal in self.recovered_terminals
            if terminal.record.slice_ref is not None
        ]
        if (
            len(ids) != len(set(ids))
            or len(recovered_ids) != len(set(recovered_ids))
            or not self.completed_slice_ids.issubset(self.all_slice_ids)
            or not self.terminal_slice_failures.issubset(self.all_slice_ids)
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                for item in self.pending_integrations
            )
            or any(
                type(generation) is not int
                or generation < 0
                or slice_id not in self.all_slice_ids
                for slice_id, generation in self.pending_integrations
            )
            or not self.completed_slice_ids.isdisjoint((*ids, *recovered_ids))
            or not set(ids).isdisjoint(recovered_ids)
            or any(
                (item.ready.slice_id, item.ready.generation) in self.pending_integrations
                for item in self.work_items
            )
            or any(
                item.material.run_id != self.run_id or item.ready.run_id != str(self.run_id)
                for item in self.work_items
            )
            or any(item.ready.slice_id not in self.all_slice_ids for item in self.work_items)
            or any(item_id not in self.all_slice_ids for item_id in recovered_ids)
            or any(
                not item.ready.dependencies.issubset(self.all_slice_ids) for item in self.work_items
            )
            or not self.available_pools
            and bool(self.work_items)
            or any(
                not isinstance(terminal, ExecutionRecoveredTerminal)
                or terminal.record.owner_kind != "run"
                or terminal.record.owner_id != self.run_id
                or terminal.record.phase is not Phase.Execute
                or terminal.record.slice_ref is None
                or not terminal.record.is_terminal
                or (terminal.record.exit is SessionExit.Completed)
                != (terminal.candidate_receipt is not None)
                for terminal in self.recovered_terminals
            )
        ):
            raise ValueError("EXECUTE round plan has duplicate or inconsistent Slice facts")
        for terminal in self.recovered_terminals:
            receipt = terminal.candidate_receipt
            if receipt is not None:
                if terminal.record.slice_ref is None:
                    raise ValueError("recovered terminal has no Slice generation identity")
                _validate_candidate_receipt(self.run_id, terminal.record.slice_ref, receipt)
                if terminal.record.candidate_checkpoint_sha256 != checkpoint_receipt_digest(
                    receipt
                ):
                    raise ValueError(
                        "recovered AgentRun candidate digest differs from its M-08 receipt"
                    )
            elif terminal.record.candidate_checkpoint_sha256 is not None:
                raise ValueError("failed recovered AgentRun cannot claim an M-08 candidate")

    @property
    def complete(self) -> bool:
        return self.all_slice_ids.issubset(self.completed_slice_ids)


class ExecutionRoundLoader(Protocol):
    """Resolve the plan and durable owner facts required to build one round.

    ``completed_slice_ids`` must be sourced from successful M-11 integration
    receipts. ``terminal_slice_failures`` must be sourced from RunActor facts.
    ``pending_integrations`` must contain accepted M-08 candidate Slice/generation
    pairs without a corresponding M-11 integration receipt. A candidate checkpoint
    or terminal AgentRun alone satisfies neither completion nor readiness.
    """

    async def load(self, run_id: RunId, logical_key: str) -> ExecutionRoundPlan: ...


class ExecutionSessionFactoryPort(Protocol):
    """Create or recover one owner-scoped session for frozen Slice material."""

    async def get_or_create(
        self, material: ExecutionSessionMaterial
    ) -> ExecutionAgentSessionPort: ...


class ExecutionScheduleStalled(RuntimeError):
    """No DAG-ready, resource-available work exists for an incomplete Run."""


@dataclass(frozen=True, slots=True)
class SliceDependencyProjection:
    """Typed predecessor sets projected from one immutable FrozenPlan."""

    requires: frozenset[str] = frozenset()
    ordered_before: frozenset[str] = frozenset()


def project_frozen_plan_dependencies(plan: FrozenPlan) -> dict[str, SliceDependencyProjection]:
    """Preserve Requires and OrderedBefore kinds while projecting a FrozenPlan DAG."""

    if not isinstance(plan, FrozenPlan):
        raise TypeError("dependency projection requires a FrozenPlan")
    slice_ids = {str(slice_.id) for slice_ in plan.slices}
    requires_by_slice: dict[str, set[str]] = {slice_id: set() for slice_id in slice_ids}
    ordered_by_slice: dict[str, set[str]] = {slice_id: set() for slice_id in slice_ids}
    for edge in plan.edges:
        predecessor = str(edge.from_)
        successor = str(edge.to)
        if predecessor not in slice_ids or successor not in slice_ids:
            raise ValueError("FrozenPlan edge endpoint is absent from its Slice set")
        if edge.kind is PlanEdgeKind.Requires:
            requires_by_slice[successor].add(predecessor)
        elif edge.kind is PlanEdgeKind.OrderedBefore:
            ordered_by_slice[successor].add(predecessor)
        else:
            raise ValueError("FrozenPlan edge kind is not supported by the runtime scheduler")
    return {
        slice_id: SliceDependencyProjection(
            requires=frozenset(requires_by_slice[slice_id]),
            ordered_before=frozenset(ordered_by_slice[slice_id]),
        )
        for slice_id in slice_ids
    }


class PersistentExecutionScheduler:
    """Dispatch concurrent Slice AgentRuns while preserving Actor receipt gates."""

    def __init__(
        self,
        *,
        round_loader: ExecutionRoundLoader,
        sessions: ExecutionSessionFactoryPort,
        fair_scheduler: FairScheduler | None = None,
        max_parallelism: int = 4,
    ) -> None:
        if type(max_parallelism) is not int or max_parallelism < 1:
            raise ValueError("EXECUTE max_parallelism must be a positive integer")
        self.round_loader = round_loader
        self.sessions = sessions
        self.fair_scheduler = fair_scheduler or FairScheduler()
        self.max_parallelism = max_parallelism

    async def advance_one_round(
        self,
        run_id: RunId,
        logical_key: str,
        *,
        on_agent_run_started: Callable[[RunId, AgentRunId], Awaitable[ActorPhaseReceipt]],
        on_agent_run_terminal: Callable[
            [RunId, AgentRun, AgentRunReceipt, CasObject], Awaitable[ActorPhaseReceipt]
        ],
    ) -> ExecutionRoundDecision:
        plan = await self.round_loader.load(run_id, logical_key)
        if not isinstance(plan, ExecutionRoundPlan) or plan.run_id != run_id:
            raise ValueError("EXECUTE round loader returned facts for another Run")
        run_key = str(run_id)
        self.fair_scheduler.restore_committed_facts(
            run_key,
            integrated_slice_ids=plan.completed_slice_ids,
            terminal_failure_slice_ids=plan.terminal_slice_failures,
        )
        for slice_id, generation in plan.pending_integrations:
            self.fair_scheduler.await_integration(
                run_key, slice_id, generation=generation
            )
        for item in plan.work_items:
            self.fair_scheduler.submit(item.ready)
        if plan.complete:
            return ExecutionRoundDecision(complete=True, dispatch_count=0)

        work_by_slice = {item.ready.slice_id: item for item in plan.work_items}
        active_scopes: set[str] = set()
        selected: list[ExecutionWorkItem] = []
        while len(selected) < self.max_parallelism:
            ready = self.fair_scheduler.next(
                frozenset(active_scopes), plan.available_pools, only_run_id=run_key
            )
            if ready is None:
                break
            selected_item = work_by_slice.get(ready.slice_id)
            if selected_item is None or selected_item.ready != ready:
                self.fair_scheduler.release(
                    ready.run_id, ready.slice_id, generation=ready.generation
                )
                for item in selected:
                    self.fair_scheduler.release(
                        run_key, item.ready.slice_id, generation=item.ready.generation
                    )
                raise ExecutionScheduleStalled(
                    "EXECUTE scheduler selected a Slice absent from the frozen round plan"
                )
            selected.append(selected_item)
            active_scopes.update(ready.write_scope)
        if not selected and not plan.recovered_terminals:
            raise ExecutionScheduleStalled(
                "EXECUTE Run is incomplete but has no DAG-ready, resource-available Slice"
            )

        completed_write_ids: list[AgentRunId] = []
        candidate_claims: list[CandidateCheckpointClaim] = []
        terminal_ids = [terminal.record.agent_run_id for terminal in plan.recovered_terminals]
        for terminal in plan.recovered_terminals:
            if terminal.candidate_receipt is None:
                continue
            assert terminal.record.slice_ref is not None
            self.fair_scheduler.await_integration(
                run_key,
                str(terminal.record.slice_ref.slice_id),
                generation=terminal.record.slice_ref.generation,
            )
            completed_write_ids.append(terminal.record.agent_run_id)
            candidate_claims.append(
                CandidateCheckpointClaim(terminal.record.agent_run_id, terminal.candidate_receipt)
            )

        dispatch_tasks = [
            asyncio.create_task(
                self._dispatch(
                    item,
                    on_agent_run_started=on_agent_run_started,
                    on_agent_run_terminal=on_agent_run_terminal,
                )
            )
            for item in selected
        ]
        try:
            results = await asyncio.gather(*dispatch_tasks, return_exceptions=True)
        except BaseException:
            for task in dispatch_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*dispatch_tasks, return_exceptions=True)
            for item in selected:
                self.fair_scheduler.release(
                    run_key, item.ready.slice_id, generation=item.ready.generation
                )
            raise
        dispatch_error = next(
            (result for result in results if isinstance(result, BaseException)), None
        )
        if dispatch_error is not None:
            for item in selected:
                self.fair_scheduler.release(
                    run_key, item.ready.slice_id, generation=item.ready.generation
                )
            raise dispatch_error
        outcomes = cast(list[ExecutionAgentOutcome], results)

        for item, outcome in zip(selected, outcomes, strict=True):
            record = outcome.record
            terminal_ids.append(record.agent_run_id)
            if outcome.candidate_receipt is None:
                self.fair_scheduler.release(
                    run_key, item.ready.slice_id, generation=item.ready.generation
                )
                continue
            self.fair_scheduler.await_integration(
                run_key, item.ready.slice_id, generation=item.ready.generation
            )
            completed_write_ids.append(record.agent_run_id)
            candidate_claims.append(
                CandidateCheckpointClaim(record.agent_run_id, outcome.candidate_receipt)
            )
        return ExecutionRoundDecision(
            complete=plan.complete,
            dispatch_count=len(selected),
            completed_write_agent_run_ids=tuple(completed_write_ids),
            candidate_claims=tuple(candidate_claims),
            terminal_agent_run_ids=tuple(terminal_ids),
        )

    async def _dispatch(
        self,
        item: ExecutionWorkItem,
        *,
        on_agent_run_started: Callable[[RunId, AgentRunId], Awaitable[ActorPhaseReceipt]],
        on_agent_run_terminal: Callable[
            [RunId, AgentRun, AgentRunReceipt, CasObject], Awaitable[ActorPhaseReceipt]
        ],
    ) -> ExecutionAgentOutcome:
        session = await self.sessions.get_or_create(item.material)
        expected = session.agent_run
        if (
            expected.owner_kind != "run"
            or expected.owner_id != item.material.run_id
            or expected.phase is not Phase.Execute
            or expected.slice_ref != item.material.slice_ref
            or expected.logical_task_key != item.material.logical_task_key
        ):
            raise ValueError("EXECUTE session factory returned another Slice AgentRun")
        outcome = await session.run(
            on_agent_run_started=on_agent_run_started,
            on_agent_run_terminal=on_agent_run_terminal,
        )
        record = outcome.record
        if (
            record.agent_run_id != expected.agent_run_id
            or record.owner_kind != "run"
            or record.owner_id != item.material.run_id
            or record.phase is not Phase.Execute
            or record.slice_ref != item.material.slice_ref
            or not record.is_terminal
            or (record.exit is SessionExit.Completed) != (outcome.candidate_receipt is not None)
        ):
            raise ValueError("EXECUTE session returned mismatched terminal Slice facts")
        if outcome.candidate_receipt is not None:
            _validate_candidate_receipt(
                item.material.run_id, item.material.slice_ref, outcome.candidate_receipt
            )
            if record.candidate_checkpoint_sha256 != checkpoint_receipt_digest(
                outcome.candidate_receipt
            ):
                raise ValueError("EXECUTE AgentRun candidate digest differs from its M-08 receipt")
        return outcome


@dataclass(frozen=True, slots=True)
class PersistentExecutionAgentSessionFactory:
    """Create owner-scoped `create_agent` sessions for M-07 Slice work."""

    infrastructure: AgentGraphInfrastructure
    gateway_factory: ExecutionGatewayFactory

    async def get_or_create(self, material: ExecutionSessionMaterial) -> ExecutionAgentSessionPort:
        if not isinstance(material, ExecutionSessionMaterial):
            raise TypeError("EXECUTE session factory requires frozen material")
        if material.binding.profile is not ModelProfile.Code:
            raise ValueError("EXECUTE AgentRun requires a Code model binding")
        owner_snapshot = await self.infrastructure.runtime_store.load(material.run_id)
        if (
            owner_snapshot is None
            or owner_snapshot.state.status is not RunStatus.Executing
            or owner_snapshot.state.frozen_plan_sha256 is None
        ):
            raise ValueError("EXECUTE material requires an active frozen Run")

        slice_ = material.slice
        session_kind = _SESSION_KIND_BY_SLICE[slice_.kind]
        template = self.infrastructure.context_manager.template_catalog.template(session_kind.value)
        identity = _frozen_context_identity(material)
        definitions = agent_tool_definitions(
            phase=Phase.Execute,
            session_kind=session_kind,
            owner_kind="run",
        )
        schema_counter = getattr(
            self.infrastructure.context_manager.token_counter, "count_tool_schemas", None
        )
        if not callable(schema_counter):
            raise ContextBudgetError(
                "exact provider tool schema counter is required",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        schema_tokens = schema_counter(definitions)
        if type(schema_tokens) is not int or schema_tokens < 0:
            raise ContextBudgetError(
                "exact provider tool schema count is invalid",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        assembled = self.infrastructure.context_manager.fit(
            identity=identity,
            template=template,
            envelope=material.envelope,
            context_window=material.binding.context_window,
            reserved_output=material.binding.output_cap,
            tool_schema_tokens=schema_tokens,
            envelope_margin=0,
        )
        template_digest = agent_template_digest(
            session=session_kind.value,
            template=template,
        )
        candidate = AgentRun(
            agent_run_id=AgentRunId(uuid4()),
            owner_kind="run",
            owner_id=material.run_id,
            logical_task_key=material.logical_task_key,
            phase=Phase.Execute,
            session_kind=session_kind,
            thread_id=str(uuid4()),
            model_binding_sha256=material.binding.digest,
            context_sha256=agent_context_digest(
                context_identity=assembled.pack.identity,
                envelope=material.envelope,
                phase=Phase.Execute,
                session_kind=session_kind,
                template_sha256=template_digest,
                budget=assembled.pack.budget,
            ),
            toolset_sha256=agent_toolset_digest(
                phase=Phase.Execute,
                session_kind=session_kind,
                owner_kind="run",
            ),
            template_sha256=template_digest,
            slice_ref=material.slice_ref,
            write_scope_sha256=write_scope_digest(slice_.write_scope),
            restarted_from=material.restarted_from,
            continuation_of=material.continuation_of,
        )
        record = await self.infrastructure.runtime_store.create_or_get_agent_run(candidate)
        if not candidate.same_creation_identity(record):
            raise StoreCommitError("persisted EXECUTE AgentRun differs from its frozen material")
        if record.is_terminal:
            raise ExecutionSessionRecoveryRequired(
                "terminal EXECUTE AgentRun requires a new logical task key"
            )
        if record.agent_run_id != candidate.agent_run_id and any(
            event.data.get("receipt_key") == f"agent_run.started:{record.agent_run_id}"
            for event in owner_snapshot.events
        ):
            raise ExecutionSessionRecoveryRequired(
                "started write AgentRun must rebuild from its latest M-08 candidate"
            )

        gateway = self.gateway_factory(record, material)
        gateway_scope = gateway.write_scope
        expected_scope_digest = write_scope_digest(slice_.write_scope)
        if (
            not isinstance(gateway_scope, WriteScope)
            or record.write_scope_sha256 != expected_scope_digest
            or write_scope_digest(gateway_scope) != expected_scope_digest
        ):
            raise ValueError("EXECUTE gateway write scope differs from frozen Slice")

        bound = create_bound_agent(
            agent_run=record,
            binding=material.binding,
            registry=self.infrastructure.provider_registry,
            context_manager=self.infrastructure.context_manager,
            template=template,
            envelope=material.envelope,
            gateway=gateway,
            usage_sink=self.infrastructure.usage_sink,
            context_identity=identity,
            checkpointer=self.infrastructure.agent_run_checkpointer_for("run", material.run_id),
        )
        return _PersistentExecutionAgentSession(
            agent_run=record,
            material=material,
            runtime_store=self.infrastructure.runtime_store,
            host_cas=self.infrastructure.host_cas,
            bound=bound,
        )


class ExecutionSessionRecoveryRequired(RuntimeError):
    """A write AgentRun must be restarted from a committed M-08 code checkpoint."""


@dataclass(slots=True)
class _PersistentExecutionAgentSession(ExecutionAgentSessionPort):
    agent_run: AgentRun
    material: ExecutionSessionMaterial
    runtime_store: RuntimeStore
    host_cas: FileHostCAS
    bound: BoundAgentRun

    async def run(
        self,
        *,
        on_agent_run_started: Callable[[RunId, AgentRunId], Awaitable[ActorPhaseReceipt]],
        on_agent_run_terminal: Callable[
            [RunId, AgentRun, AgentRunReceipt, CasObject], Awaitable[ActorPhaseReceipt]
        ],
    ) -> ExecutionAgentOutcome:
        started = await on_agent_run_started(self.material.run_id, self.agent_run.agent_run_id)
        if (
            started.run_id != self.material.run_id
            or started.receipt_key != f"agent_run.started:{self.agent_run.agent_run_id}"
            or not await self._has_owner_receipt(started.receipt_key)
        ):
            raise ValueError("EXECUTE provider call requires a committed started AgentRun receipt")

        candidate_receipt: CheckpointReceipt | None = None
        result_exit = SessionExit.Failed
        result_state = SessionState.Failed
        rounds = 0
        checkpoint_sha256: str | None = None
        try:
            result = await self.bound.ainvoke(task=self.material.task)
            rounds = result.rounds
            checkpoint_sha256 = await self._latest_checkpoint_digest()
            if result.exit is SessionExit.Completed:
                if result.state is not SessionState.CheckpointPending or checkpoint_sha256 is None:
                    raise RuntimeError("completed EXECUTE AgentRun has no durable graph checkpoint")
                candidate_receipt = await self.material.candidate_checkpoint.checkpoint(
                    self.agent_run, self.material
                )
                _validate_candidate_receipt(
                    self.material.run_id, self.material.slice_ref, candidate_receipt
                )
                result_exit = SessionExit.Completed
                result_state = SessionState.Closed
            elif result.exit is SessionExit.SegmentStopped:
                result_exit = SessionExit.SegmentStopped
                result_state = SessionState.Closed
            elif result.exit is SessionExit.BudgetExhausted:
                result_exit = SessionExit.BudgetExhausted
            elif result.exit is SessionExit.Invalidated:
                result_exit = SessionExit.Invalidated
                result_state = SessionState.Invalidated
            else:
                result_exit = SessionExit.Failed
        except Exception:
            candidate_receipt = None
            result_exit = SessionExit.Failed
            result_state = SessionState.Failed

        result_payload = canonical_json_bytes(
            {
                "agent_run_id": str(self.agent_run.agent_run_id),
                "exit": result_exit.value,
                "rounds": rounds,
            }
        )
        result_object = self.host_cas.put(result_payload)
        record = replace(
            self.agent_run,
            state=result_state,
            exit=result_exit,
            checkpoint_sha256=checkpoint_sha256,
            candidate_checkpoint_sha256=(
                checkpoint_receipt_digest(candidate_receipt)
                if candidate_receipt is not None
                else None
            ),
            result_sha256=result_object.digest,
        )
        receipt = AgentRunReceipt(
            receipt_id=uuid4(),
            agent_run_id=record.agent_run_id,
            category="session.terminal",
        )
        terminal = await on_agent_run_terminal(self.material.run_id, record, receipt, result_object)
        if (
            terminal.run_id != self.material.run_id
            or terminal.receipt_key != f"agent_run.terminal:{record.agent_run_id}"
            or not await self._has_owner_receipt(terminal.receipt_key)
        ):
            raise ValueError("EXECUTE terminal facts lack a committed RunActor receipt")
        return ExecutionAgentOutcome(record, candidate_receipt)

    async def _has_owner_receipt(self, receipt_key: str) -> bool:
        snapshot = await self.runtime_store.load(self.material.run_id)
        return snapshot is not None and any(
            event.data.get("receipt_key") == receipt_key for event in snapshot.events
        )

    async def _latest_checkpoint_digest(self) -> str | None:
        indexes = await self.runtime_store.list_checkpoint_indexes(self.agent_run.thread_id)
        latest = indexes[0] if indexes else None
        if latest is None:
            return None
        if (
            latest.graph_family,
            latest.owner_kind,
            latest.owner_id,
            latest.thread_id,
        ) != ("agent", "run", self.material.run_id, self.agent_run.thread_id):
            raise RuntimeError("EXECUTE AgentRun checkpoint belongs to another owner")
        return latest.object.digest


def _frozen_context_identity(material: ExecutionSessionMaterial) -> ContextPackIdentity:
    base = material.context_identity
    payload = {
        "plan_revision_sha256": str(base.plan_revision_sha256),
        "slice": material._slice_payload.decode("utf-8"),
        "slice_ref": material.slice_ref.model_dump(mode="json", by_alias=True),
        "task_sha256": hashlib.sha256(material.task.encode("utf-8")).hexdigest(),
    }
    revision_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return base.model_copy(update={"plan_revision_sha256": Sha256(revision_digest)})


def _validate_candidate_receipt(
    run_id: RunId, slice_ref: SliceGenerationRef, receipt: CheckpointReceipt
) -> None:
    expected_oid = str(slice_ref.baseline_candidate_oid)
    candidate = receipt.manifest.slice_candidate
    if (
        receipt.run_id != run_id
        or receipt.slice_id != slice_ref.slice_id
        or receipt.generation != slice_ref.generation
        or receipt.expected_candidate_oid != expected_oid
        or candidate.run_id != run_id
        or candidate.slice_id != slice_ref.slice_id
        or candidate.generation != slice_ref.generation
        or str(candidate.candidate_commit_oid) != expected_oid
        or not receipt.manifest.scope_check_passed
        or receipt.new_candidate_oid == expected_oid
    ):
        raise ValueError("M-08 candidate checkpoint receipt does not match the Slice AgentRun")


_SESSION_KIND_BY_SLICE: dict[SliceKind, SessionKind] = {
    SliceKind.Contract: SessionKind.Contract,
    SliceKind.Implementation: SessionKind.Implementation,
    SliceKind.TestTranslation: SessionKind.TestTranslation,
    SliceKind.TestGeneration: SessionKind.TestGeneration,
}


__all__ = [
    "ExecutionAgentOutcome",
    "ExecutionAgentSessionPort",
    "ExecutionCandidateCheckpointPort",
    "ExecutionGatewayFactory",
    "ExecutionRecoveredTerminal",
    "ExecutionSessionMaterial",
    "ExecutionSessionRecoveryRequired",
    "ExecutionSessionFactoryPort",
    "ExecutionRoundLoader",
    "ExecutionRoundPlan",
    "ExecutionScheduleStalled",
    "ExecutionWorkItem",
    "PersistentExecutionAgentSessionFactory",
    "PersistentExecutionScheduler",
    "SliceDependencyProjection",
    "project_frozen_plan_dependencies",
]
