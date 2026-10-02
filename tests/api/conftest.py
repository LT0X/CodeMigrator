from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from codemigrator.analysis import (
    AnalysisCapability,
    AnalysisResult,
    ModuleBoundary,
    ModuleFact,
    ModuleRole,
)
from codemigrator.api.deps import ApiRequest, EventRecord
from codemigrator.api.idempotency import IdempotencyStore
from codemigrator.api.problems import ApiError
from codemigrator.core import (
    ArtifactRef,
    CheckAction,
    DescriptorLock,
    DossierBudgetTier,
    DossierEntry,
    FrozenArtifactBundle,
    MigrationRulebook,
    MigrationSpec,
    PlanEdgeKind,
    ProjectModuleId,
    RequiredCheckSelection,
    SliceKind,
    SpecScope,
    TargetProjectBlueprint,
    UnderstandingDossier,
)
from codemigrator.planning import (
    EdgeProvenance,
    PlanEdgeProposal,
    PlanLedger,
    PlanningInputs,
    PlanProposal,
    PlanSliceProposal,
)
from codemigrator.runtime.contracts import DraftSessionEventSpec


def draft_question_event(question_id: str | None = None) -> DraftSessionEventSpec:
    """Build the complete public AskUser event accepted by the Draft allowlist."""

    return DraftSessionEventSpec(
        "session.question.asked",
        {
            "question_id": question_id or str(uuid4()),
            "revision": 1,
            "prompt": "Choose how this migration should handle compatibility.",
            "options": [
                {
                    "key": "preserve",
                    "label": "Preserve behavior",
                    "impact": "Keeps current user-visible behavior.",
                    "recommended": True,
                },
                {
                    "key": "simplify",
                    "label": "Simplify behavior",
                    "impact": "May change existing edge cases.",
                    "recommended": False,
                },
            ],
            "allow_free_text": True,
        },
    )


def build_plan_agent_inputs() -> PlanningInputs:
    def module_id(number: int) -> UUID:
        return UUID(f"00000000-0000-7000-8000-{number:012d}")

    def artifact_ref(char: str) -> ArtifactRef:
        return ArtifactRef(sha256=char * 64, size=1, media_type="application/json")

    entry = DossierEntry(
        kind="architecture", content="module facts", anchors=[], advisory=True
    )
    return PlanningInputs(
        frozen_artifacts=FrozenArtifactBundle(
            spec=artifact_ref("1"),
            understanding_dossier=artifact_ref("2"),
            target_project_blueprint=artifact_ref("3"),
            migration_rulebook=artifact_ref("4"),
        ),
        spec=MigrationSpec(
            schema="codemigrator.migration-spec",
            version=3,
            name="production-plan-test",
            source_language_id="typescript",
            target_language_id="python",
            descriptor_lock=DescriptorLock(
                descriptor_version="1.0.0",
                source_descriptor_sha256="a" * 64,
                target_descriptor_sha256="b" * 64,
                toolchain_image_digest="c" * 64,
            ),
            scope=SpecScope(include=["src/", "tests/"]),
            required_checks=[
                RequiredCheckSelection(action=CheckAction.Compile, template_sha256="d" * 64)
            ],
        ),
        understanding_dossier=UnderstandingDossier(
            architecture_narrative=[entry],
            semantic_modules=[],
            dependency_resolutions=[],
            test_map=[],
            risk_hotspots=[],
            strategy_advice=[],
            coverage_self_report={},
            budget_tier=DossierBudgetTier.Shallow,
        ),
        target_project_blueprint=TargetProjectBlueprint(
            module_boundaries=[],
            granularity_principles=["preserve module boundaries"],
            target_layout_principles=["use src layout"],
            parallelism_rules=["independent modules may run in parallel"],
            generated_artifact_policy="regenerate generated code",
            version=1,
        ),
        migration_rulebook=MigrationRulebook(entries=[], version=1),
        analysis=AnalysisResult(
            snapshot_oid="snapshot-production-plan",
            descriptor_sha256="e" * 64,
            capability=AnalysisCapability.Full,
            modules=[
                ModuleFact(
                    module_id=module_id(1),
                    file_paths=["src/a.ts"],
                    role=ModuleRole.Source,
                    boundary=ModuleBoundary.File,
                    exported_symbols=[],
                    capability=AnalysisCapability.Full,
                    degraded_files=[],
                ),
                ModuleFact(
                    module_id=module_id(2),
                    file_paths=["src/b.ts"],
                    role=ModuleRole.Source,
                    boundary=ModuleBoundary.File,
                    exported_symbols=[],
                    capability=AnalysisCapability.Full,
                    degraded_files=[],
                ),
            ],
            imports=[],
            coverage=[],
            coverage_status=[],
            conservation=[],
            manifests=[],
            artifacts=[],
            symbol_bindings=[],
            reference_sites=[],
            symbol_coverage=[],
        ),
        snapshot_oid="snapshot-production-plan",
    )


def build_frozen_plan():
    inputs = build_plan_agent_inputs()
    proposal = PlanProposal(
        slices=(
            PlanSliceProposal(
                local_ref="A",
                kind=SliceKind.Implementation,
                source_modules=[ProjectModuleId(inputs.analysis.modules[0].module_id)],
                write_paths=["target/a.py"],
                create_roots=["target/a"],
            ),
            PlanSliceProposal(
                local_ref="B",
                kind=SliceKind.Implementation,
                source_modules=[ProjectModuleId(inputs.analysis.modules[1].module_id)],
                write_paths=["target/b.py"],
                create_roots=["target/b"],
            ),
        ),
        edges=(
            PlanEdgeProposal(
                from_="A",
                to="B",
                kind=PlanEdgeKind.Requires,
                provenance=EdgeProvenance.Structural,
            ),
        ),
        integration_ranks={"A": 0, "B": 1},
        planner_rationale=(),
    )
    return PlanLedger().freeze(proposal, inputs)


class FakeBackend:
    def __init__(self) -> None:
        self.requests: list[ApiRequest] = []
        self.events: list[EventRecord] = []
        self.session_events: list[EventRecord] = []
        self.stream_calls: list[str] = []
        self.idempotency = IdempotencyStore()
        self.idempotency_lock = asyncio.Lock()

    async def execute(self, request: ApiRequest) -> object:
        self.requests.append(request)
        if request.operation == "create_spec":
            payload = request.payload
            return {
                "spec_id": str(uuid4()),
                "canonical_sha256": "f" * 64,
                "source_language_id": payload.source_language_id,
                "target_language_id": payload.target_language_id,
                "descriptor_lock": payload.descriptor_lock,
                "required_checks": payload.required_checks,
            }
        if request.operation in {"create_run", "cancel_run"}:
            return {
                "run_id": str(request.resource_id or uuid4()),
                "status": "PLANNING",
                "version": 1,
            }
        if request.operation in {
            "list_migrations",
            "list_descriptors",
            "list_projects",
            "list_skills",
        }:
            return {"items": []}
        if request.operation == "get_workspace":
            return {"run_id": str(request.resource_id), "slices": []}
        if request.operation == "get_report":
            return {"run_id": str(request.resource_id), "status": "READY"}
        if request.operation == "get_evidence":
            return {
                "run_id": str(request.resource_id),
                "receipt_id": request.query["receipt_id"],
                "status": "READY",
            }
        if request.operation == "health":
            return {"app": "READY", "postgres": "READY", "sandbox": "READY"}
        if request.operation == "register_project":
            return {"project_id": str(uuid4())}
        if request.operation == "create_session":
            return {"session_id": str(uuid4()), "status": "DRAFTING"}
        if request.operation in {
            "session_message",
            "session_answer",
            "session_confirm",
            "correction_confirm",
        }:
            return {"session_id": str(request.resource_id), "status": "DRAFTING"}
        if request.operation == "get_changes":
            return {"run_id": str(request.resource_id), "changes": []}
        if request.operation == "get_output":
            return {"run_id": str(request.resource_id), "status": "READY", "files": []}
        return {"operation": request.operation}

    async def execute_idempotent(
        self,
        request: ApiRequest,
        *,
        route: str,
        key: str,
        canonical_body: bytes,
        status_code: int,
    ) -> object:
        async with self.idempotency_lock:
            cached = self.idempotency.lookup(
                request.principal_id, route, key, canonical_body
            )
            if cached is not None:
                if cached.conflict:
                    raise ApiError(
                        409,
                        "idempotency key was reused with a different body",
                        "IDEMPOTENCY_CONFLICT",
                    )
                return cached.body
            result = await self.execute(request)
            self.idempotency.remember(
                request.principal_id,
                route,
                key,
                canonical_body,
                status_code,
                result,
            )
            return result

    async def read_events(self, run_id: UUID, after_sequence: int) -> tuple[EventRecord, ...]:
        self.stream_calls.append("run.read")
        return tuple(
            event
            for event in self.events
            if event.run_id == run_id and event.sequence > after_sequence
        )

    async def wait_for_events(self, run_id: UUID, after_sequence: int) -> None:
        self.stream_calls.append("run.wait")
        del run_id, after_sequence
        await asyncio.sleep(60)

    async def is_stream_terminal(self, run_id: UUID, after_sequence: int) -> bool:
        self.stream_calls.append("run.terminal")
        terminal = {"COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "CANCELLED"}
        return any(
            item.run_id == run_id
            and item.sequence <= after_sequence
            and item.event_type == "run.status_changed"
            and item.data.get("run_status", item.data.get("status")) in terminal
            for item in self.events
        )

    async def read_session_events(
        self, session_id: UUID, after_sequence: int
    ) -> tuple[EventRecord, ...]:
        self.stream_calls.append("session.read")
        return tuple(
            event
            for event in self.session_events
            if event.run_id == session_id and event.sequence > after_sequence
        )

    async def wait_for_session_events(self, session_id: UUID, after_sequence: int) -> None:
        self.stream_calls.append("session.wait")
        del session_id, after_sequence
        await asyncio.sleep(60)

    async def is_session_stream_terminal(self, session_id: UUID, after_sequence: int) -> bool:
        self.stream_calls.append("session.terminal")
        return any(
            item.run_id == session_id
            and item.sequence <= after_sequence
            and item.event_type in {"session.closed", "session.attached_to_run"}
            for item in self.session_events
        )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


def artifact() -> dict[str, object]:
    return {"sha256": "a" * 64, "size": 1, "media_type": "application/json"}


def spec_payload() -> dict[str, object]:
    return {
        "schema": "codemigrator.spec",
        "version": 3,
        "name": "typescript-to-python",
        "source_language_id": "typescript",
        "target_language_id": "python",
        "descriptor_lock": {
            "descriptor_version": "1.0.0",
            "source_descriptor_sha256": "a" * 64,
            "target_descriptor_sha256": "b" * 64,
            "toolchain_image_digest": "c" * 64,
        },
        "scope": {"include": ["src/"]},
        "required_checks": [
            {"action": "COMPILE", "template_sha256": "d" * 64},
            {"action": "TEST", "template_sha256": "e" * 64},
        ],
    }


def create_run_payload() -> dict[str, object]:
    return {
        "source": {
            "repository_url": "https://github.com/example/source.git",
            "base_ref": "main",
        },
        "branch_prefix": "team/port-py",
        "frozen_artifacts": {
            "spec": artifact(),
            "understanding_dossier": artifact(),
            "target_project_blueprint": artifact(),
            "migration_rulebook": artifact(),
        },
    }


def event(run_id: UUID, sequence: int, event_type: str = "run.status_changed") -> EventRecord:
    return EventRecord(
        run_id=run_id,
        sequence=sequence,
        event_type=event_type,
        data={"status": "PLANNING"},
        timestamp_utc=datetime.now(UTC),
    )
