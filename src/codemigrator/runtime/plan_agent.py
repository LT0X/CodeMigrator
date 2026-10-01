"""Production PLAN AgentRun session and stage factories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from codemigrator.core import (
    ContextPackIdentity,
    ModelProfile,
    Phase,
    RunId,
    SessionKind,
    Sha256,
    canonical_json_bytes,
    load_resource,
)
from codemigrator.core.models.plan import PlanProposal, PlanViolation
from codemigrator.planning import PlanLedger, PlanningInputs, PlanValidator
from codemigrator.planning.models import FrozenPlan

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .binding import LockedModelBinding
from .cas import FileHostCAS
from .context import ContextEnvelope
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
from .run_graph import (
    PlanAgentCompletion,
    PlanAgentSessionFactory,
    PlanAgentSessionPort,
    PlanOwnerPort,
    PlanProposalRejected,
    PlanProposalWorkflow,
)
from .store import RuntimeStore

_PLAN_TASK_PREFIX = "Propose a complete migration PlanProposal"


@dataclass(frozen=True, slots=True, init=False)
class PlanSessionMaterial:
    """Detached planning snapshot and locked model binding for one Run's PLAN."""

    _planning_inputs_payload: bytes
    binding: LockedModelBinding
    envelope: ContextEnvelope = ContextEnvelope()

    def __init__(
        self,
        planning_inputs: PlanningInputs,
        binding: LockedModelBinding,
        envelope: ContextEnvelope = ContextEnvelope(),
    ) -> None:
        if not isinstance(planning_inputs, PlanningInputs):
            raise TypeError("PLAN material must contain validated PlanningInputs")
        object.__setattr__(
            self,
            "_planning_inputs_payload",
            canonical_json_bytes(planning_inputs.model_dump(mode="json", by_alias=True)),
        )
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "envelope", envelope)

    @property
    def planning_inputs(self) -> PlanningInputs:
        """Return a fresh model parsed from the immutable input snapshot."""

        return PlanningInputs.model_validate_json(self._planning_inputs_payload)

    @property
    def planning_material_sha256(self) -> Sha256:
        return Sha256(hashlib.sha256(self._planning_inputs_payload).hexdigest())


class PlanSessionMaterialLoader(Protocol):
    """Resolve CreateRun-frozen artifacts and analysis facts for PLAN."""

    async def load(self, run_id: RunId) -> PlanSessionMaterial: ...


class PlanAgentGatewayFactory(Protocol):
    """Build a ToolGateway whose context is fixed to this AgentRun."""

    def __call__(self, record: AgentRun) -> ToolGatewayPort: ...


class PlanRunActorPort(PlanOwnerPort, Protocol):
    """PLAN actor capability including its immutable owner identity."""

    run_id: RunId


@dataclass(frozen=True, slots=True)
class PersistentPlanAgentSessionFactory(PlanAgentSessionFactory):
    """Create receipt-gated structured PLAN sessions on isolated CAS threads."""

    infrastructure: AgentGraphInfrastructure
    actor: PlanRunActorPort
    material_loader: PlanSessionMaterialLoader
    gateway_factory: PlanAgentGatewayFactory

    async def get_or_create(
        self, run_id: RunId, logical_task_key: str
    ) -> PlanAgentSessionPort:
        if self.actor.run_id != run_id:
            raise ValueError("PLAN stage actor does not own the requested Run")
        expected_key = f"plan:{run_id}"
        if logical_task_key != expected_key:
            raise ValueError("PLAN AgentRun logical task key is invalid")
        material = await self.material_loader.load(run_id)
        if not isinstance(material, PlanSessionMaterial):
            raise TypeError("PLAN material loader returned an invalid result")
        if material.binding.profile is not ModelProfile.Reasoning:
            raise ValueError("PLAN AgentRun requires a Reasoning model binding")
        planning_inputs_payload = material._planning_inputs_payload
        planning_inputs = PlanningInputs.model_validate_json(planning_inputs_payload)
        owner_snapshot = await self.infrastructure.runtime_store.load(run_id)
        if owner_snapshot is None or owner_snapshot.state.create_request is None:
            raise ValueError("PLAN material requires a committed CreateRun owner")
        if (
            owner_snapshot.state.create_request.frozen_artifacts
            != planning_inputs.frozen_artifacts
        ):
            raise ValueError("PLAN material differs from the Run's frozen artifacts")

        template = self.infrastructure.context_manager.template_catalog.template(
            SessionKind.PlanAuxiliary.value
        )
        identity = _context_identity(
            run_id,
            planning_inputs,
            material.binding,
            material.planning_material_sha256,
        )
        schema_definitions = agent_tool_definitions(
            phase=Phase.Plan,
            session_kind=SessionKind.PlanAuxiliary,
            owner_kind="run",
            response_format=PlanProposal,
        )
        schema_counter = getattr(
            self.infrastructure.context_manager.token_counter, "count_tool_schemas", None
        )
        if not callable(schema_counter):
            raise ContextBudgetError(
                "exact provider tool schema counter is required",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        schema_tokens = schema_counter(schema_definitions)
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
            session=SessionKind.PlanAuxiliary.value,
            template=template,
        )
        candidate = AgentRun(
            agent_run_id=AgentRunId(uuid4()),
            owner_kind="run",
            owner_id=run_id,
            logical_task_key=logical_task_key,
            phase=Phase.Plan,
            session_kind=SessionKind.PlanAuxiliary,
            thread_id=str(uuid4()),
            model_binding_sha256=material.binding.digest,
            context_sha256=agent_context_digest(
                context_identity=assembled.pack.identity,
                envelope=material.envelope,
                phase=Phase.Plan,
                session_kind=SessionKind.PlanAuxiliary,
                template_sha256=template_digest,
                budget=assembled.pack.budget,
            ),
            toolset_sha256=agent_toolset_digest(
                phase=Phase.Plan,
                session_kind=SessionKind.PlanAuxiliary,
                owner_kind="run",
                response_format=PlanProposal,
            ),
            template_sha256=template_digest,
        )
        record = await self.infrastructure.runtime_store.create_or_get_agent_run(candidate)
        if not candidate.same_creation_identity(record):
            raise ValueError("persisted PLAN AgentRun differs from frozen Run inputs")

        bound = create_bound_agent(
            agent_run=record,
            binding=material.binding,
            registry=self.infrastructure.provider_registry,
            context_manager=self.infrastructure.context_manager,
            template=template,
            envelope=material.envelope,
            gateway=self.gateway_factory(record),
            usage_sink=self.infrastructure.usage_sink,
            context_identity=identity,
            checkpointer=self.infrastructure.agent_run_checkpointer_for("run", run_id),
            response_format=PlanProposal,
        )
        return _PersistentPlanAgentSession(
            agent_run=record,
            planning_inputs_payload=planning_inputs_payload,
            actor=self.actor,
            runtime_store=self.infrastructure.runtime_store,
            host_cas=self.infrastructure.host_cas,
            bound=bound,
        )


@dataclass(frozen=True, slots=True)
class PersistentPlanStageFactory:
    """Build the PLAN AgentRun workflow with the existing M-07 validator."""

    material_loader: PlanSessionMaterialLoader
    gateway_factory: PlanAgentGatewayFactory
    validator: PlanValidator | None = None
    ledger: PlanLedger | None = None

    def __call__(
        self, infrastructure: AgentGraphInfrastructure, actor: PlanOwnerPort
    ) -> PlanProposalWorkflow:
        validator = self.validator or PlanValidator()
        ledger = self.ledger or PlanLedger(validator)
        return PlanProposalWorkflow(
            sessions=PersistentPlanAgentSessionFactory(
                infrastructure=infrastructure,
                actor=actor,
                material_loader=self.material_loader,
                gateway_factory=self.gateway_factory,
            ),
            validator=validator,
            ledger=ledger,
            owner=actor,
        )


@dataclass(slots=True)
class _PersistentPlanAgentSession(PlanAgentSessionPort):
    agent_run: AgentRun
    planning_inputs_payload: bytes
    actor: PlanOwnerPort
    runtime_store: RuntimeStore
    host_cas: FileHostCAS
    bound: BoundAgentRun
    _latest_proposal: PlanProposal | None = None
    _completion: PlanAgentCompletion | None = None

    @property
    def inputs(self) -> PlanningInputs:
        """Return an isolated model view of the AgentRun's frozen inputs."""

        return PlanningInputs.model_validate_json(self.planning_inputs_payload)

    async def propose(self, feedback: tuple[object, ...]) -> PlanProposal:
        await self._require_started_receipt()
        if not feedback and self._latest_proposal is None:
            recovered = await self._recovered_proposal()
            if recovered is not None:
                self._latest_proposal = recovered
                return recovered
        max_proposals = PlanProposalWorkflow.feedback_limit + 1
        if feedback and await self._proposal_prompt_count() >= max_proposals:
            raise PlanProposalRejected("PLAN validation feedback limit exhausted")
        inputs = PlanningInputs.model_validate_json(self.planning_inputs_payload)
        task = _proposal_task(inputs, feedback)
        result = await self.bound.ainvoke(task=task)
        proposal = result.structured_response
        if (
            result.agent_run_id != self.agent_run.agent_run_id
            or result.exit is not SessionExit.Completed
            or result.state is not SessionState.CheckpointPending
            or not isinstance(proposal, PlanProposal)
        ):
            raise RuntimeError("PLAN AgentRun did not return a completed structured proposal")
        self._latest_proposal = proposal
        return proposal

    async def complete(
        self, proposal: PlanProposal, frozen_plan: FrozenPlan
    ) -> PlanAgentCompletion:
        await self._require_started_receipt()
        if proposal != self._latest_proposal or frozen_plan.proposal != proposal:
            raise ValueError("PLAN completion differs from the latest validated proposal")
        if (
            self._completion is not None
            and self._completion.plan_object.digest
            == hashlib.sha256(frozen_plan.canonical_payload()).hexdigest()
        ):
            return self._completion
        result_object = self.host_cas.put(canonical_json_bytes(proposal))
        plan_object = self.host_cas.put(frozen_plan.canonical_payload())
        checkpoint_state = await self.bound.graph.aget_state(self._thread_config())
        indexes = await self.runtime_store.list_checkpoint_indexes(self.agent_run.thread_id)
        latest_checkpoint = indexes[0] if indexes else None
        checkpoint_owner = (
            latest_checkpoint.graph_family,
            latest_checkpoint.owner_kind,
            latest_checkpoint.owner_id,
            latest_checkpoint.thread_id,
        ) if latest_checkpoint is not None else None
        expected_owner = (
            "agent",
            self.agent_run.owner_kind,
            self.agent_run.owner_id,
            self.agent_run.thread_id,
        )
        if (
            checkpoint_state.values.get("structured_response") != proposal
            or latest_checkpoint is None
            or checkpoint_owner != expected_owner
        ):
            raise RuntimeError("PLAN AgentRun completion lacks a durable proposal checkpoint")
        completed = replace(
            self.agent_run,
            state=SessionState.Closed,
            exit=SessionExit.Completed,
            result_sha256=result_object.digest,
            checkpoint_sha256=latest_checkpoint.object.digest,
        )
        receipt = AgentRunReceipt(
            receipt_id=uuid4(),
            agent_run_id=completed.agent_run_id,
            category="plan.accepted",
        )
        self._completion = PlanAgentCompletion(
            record=completed,
            receipt=receipt,
            result_object=result_object,
            plan_object=plan_object,
        )
        return self._completion

    async def _require_started_receipt(self) -> None:
        key = f"agent_run.started:{self.agent_run.agent_run_id}"
        if not await self.actor.has_receipt(RunId(self.agent_run.owner_id), key):
            raise ValueError("PLAN provider call requires a committed started AgentRun receipt")

    async def _recovered_proposal(self) -> PlanProposal | None:
        config = self._thread_config()
        checkpoint = await self.bound.checkpointer.aget_tuple(config)
        if checkpoint is None:
            return None
        snapshot = await self.bound.graph.aget_state(config)
        if snapshot.next:
            return None
        proposal = snapshot.values.get("structured_response")
        return proposal if isinstance(proposal, PlanProposal) else None

    async def _proposal_prompt_count(self) -> int:
        snapshot = await self.bound.graph.aget_state(self._thread_config())
        messages = snapshot.values.get("messages", ())
        if not isinstance(messages, (list, tuple)):
            return 0
        return sum(
            getattr(message, "type", None) == "human"
            and isinstance(getattr(message, "content", None), str)
            and message.content.startswith(_PLAN_TASK_PREFIX)
            for message in messages
        )

    def _thread_config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.agent_run.thread_id}}


def _context_identity(
    run_id: RunId,
    inputs: PlanningInputs,
    binding: LockedModelBinding,
    planning_material_sha256: Sha256,
) -> ContextPackIdentity:
    frozen = inputs.frozen_artifacts
    contract_refs = {
        "understanding_dossier": frozen.understanding_dossier.model_dump(mode="json"),
        "target_project_blueprint": frozen.target_project_blueprint.model_dump(mode="json"),
        "migration_rulebook": frozen.migration_rulebook.model_dump(mode="json"),
    }
    return ContextPackIdentity(
        run_id=run_id,
        phase=Phase.Plan,
        session=SessionKind.PlanAuxiliary,
        slice=None,
        spec_sha256=frozen.spec.sha256,
        model_binding_sha256=Sha256(binding.digest),
        phase_policy_sha256=Sha256(load_resource("core://phase-tool-policy/v2").sha256),
        contract_refs_sha256=Sha256(
            hashlib.sha256(canonical_json_bytes(contract_refs)).hexdigest()
        ),
        planning_material_sha256=planning_material_sha256,
    )


def _proposal_task(
    inputs: PlanningInputs, feedback: tuple[object, ...]
) -> str:
    violations = []
    for item in feedback:
        if not isinstance(item, PlanViolation):
            raise TypeError("PLAN feedback must contain typed M-07 violations")
        violations.append(
            {
                "code": item.code.value,
                "pointer": item.pointer,
                "message": item.message,
            }
        )
    payload = {
        "planning_inputs": inputs.model_dump(mode="json", by_alias=True),
        "validation_feedback": violations,
    }
    return (
        f"{_PLAN_TASK_PREFIX} from the frozen artifacts and mechanical "
        "analysis facts below. Use only the authorized read-only tools when exploration is "
        "needed. Return the required structured PlanProposal.\n"
        + canonical_json_bytes(payload).decode("utf-8")
    )


__all__ = [
    "PersistentPlanAgentSessionFactory",
    "PersistentPlanStageFactory",
    "PlanSessionMaterial",
    "PlanSessionMaterialLoader",
]
