"""Root-level composition of the API boundary and runtime owner adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from fastapi import FastAPI

from codemigrator.api.backend import (
    ApiCommandStorePort,
    ApiProductionCapabilities,
    DraftCommandResult,
    DraftGraphStarterPort,
    DraftSessionCommandPort,
    RunCreationOwnerPort,
)
from codemigrator.api.deps import ApiConfig
from codemigrator.api.draft_host import (
    RegisteredSnapshot,
)
from codemigrator.api.draft_host import (
    RegisteredSnapshotResolver as RegisteredSnapshotResolverPort,
)
from codemigrator.api.dto import (
    SessionAnswerRequest,
    SessionConfirmRequest,
    SessionCreateRequest,
    SessionMessageRequest,
)
from codemigrator.api.problems import ApiError
from codemigrator.api.production import (
    ApiApplicationResources,
)
from codemigrator.api.production import (
    create_production_app as _create_api_app,
)
from codemigrator.api_read_model import RuntimeRunReadModel
from codemigrator.core import (
    MigrationSessionStatus,
    QuestionId,
    RegisteredProject,
    canonical_json_bytes,
    new_uuid7,
)
from codemigrator.runtime.actor import RunActorFactory
from codemigrator.runtime.contracts import (
    DraftSessionEventSpec,
    RuntimeStoreTransaction,
)
from codemigrator.runtime.create_run import (
    CreateRunPreflightPort,
    RunCreationOwner,
    RunGraphStarter,
)
from codemigrator.runtime.draft import DraftConflictError, DraftFlow
from codemigrator.runtime.draft_graph import DraftFlowOwner, MigrationSessionGraph
from codemigrator.runtime.draft_models import AskUserAnswer as DraftAskUserAnswer
from codemigrator.runtime.draft_validation import build_domain_skeleton
from codemigrator.runtime.graph_composition import (
    RuntimeGraphAssembly,
    RuntimeGraphConfigurationError,
)
from codemigrator.runtime.store import PostgreSQLRuntimeStore, RuntimeStore, StoreCommitError


class RuntimeDraftSessionCommands:
    """Persist Draft command facts before their graph continuation is scheduled."""

    def __init__(
        self,
        store: RuntimeStore,
        snapshot_resolver: RegisteredSnapshotResolverPort,
    ) -> None:
        self._store = store
        self._snapshot_resolver = snapshot_resolver

    async def create_session(
        self,
        principal_id: str,
        payload: SessionCreateRequest,
        snapshot: RegisteredSnapshot,
        transaction: object,
    ) -> DraftCommandResult:
        if not principal_id.strip() or snapshot.project != payload.payload.source:
            raise ApiError(404, "registered snapshot not found", "NOT_FOUND")
        draft_id = new_uuid7()
        fact = {
            "principal_id": principal_id,
            "source": snapshot.project.model_dump(mode="json"),
            "snapshot_oid": snapshot.source.snapshot_oid,
            "module_files": {
                path: list(files) for path, files in sorted(snapshot.module_files.items())
            },
            "goal": payload.payload.goal,
        }
        receipt = await self._store.commit_draft_owner_fact(
            draft_id,
            "draft.session.created",
            "draft.session.created",
            fact,
            transaction=cast(RuntimeStoreTransaction, transaction),
        )
        return DraftCommandResult(
            session_id=draft_id,
            status=MigrationSessionStatus.Drafting,
            revision=0,
            owner_receipt=receipt,
        )

    async def send_message(
        self,
        session_id: UUID,
        payload: SessionMessageRequest,
        transaction: object,
    ) -> DraftCommandResult:
        owner, seed, _snapshot = await self._load_owner(session_id)
        self._ensure_open(owner)
        revision = owner.current_revision_number or 0
        if payload.revision is not None and payload.revision != revision:
            raise ApiError(409, "Draft revision is stale", "STALE_VERSION")
        digest = hashlib.sha256(
            canonical_json_bytes({"message": payload.message, "revision": revision})
        ).hexdigest()
        receipt_key = f"draft.turn.requested:{revision}:{digest}"
        receipt = await self._store.commit_draft_owner_fact(
            session_id,
            receipt_key,
            "draft.turn.requested",
            {"message": payload.message, "revision": revision},
            transaction=cast(RuntimeStoreTransaction, transaction),
        )
        del seed
        return DraftCommandResult(
            session_id=session_id,
            status=MigrationSessionStatus.Drafting,
            revision=revision,
            owner_receipt=receipt,
        )

    async def answer_question(
        self,
        session_id: UUID,
        payload: SessionAnswerRequest,
        transaction: object,
    ) -> DraftCommandResult:
        owner, _seed, _snapshot = await self._load_owner(session_id)
        self._ensure_open(owner)
        current = owner.current_revision_number or 0
        if payload.revision != current:
            raise ApiError(409, "Draft revision is stale", "STALE_VERSION")
        question = await owner.load_question(str(payload.question_id))
        current_revision = owner.flow.ledger.current_revision
        if (
            question is None
            or current_revision is None
            or question.revision_id != current_revision.revision_id
        ):
            raise ApiError(409, "AskUser question is stale", "STALE_VERSION")
        answer = DraftAskUserAnswer(
            question_id=QuestionId(payload.question_id),
            revision_id=question.revision_id,
            selected_option=payload.answer.selected_option,
            free_text=payload.answer.free_text,
        )
        try:
            owner.flow.answer_user(answer)
        except DraftConflictError as exc:
            raise ApiError(
                409,
                "AskUser answer conflicts with the current question",
                "STALE_VERSION",
            ) from exc
        receipt = await self._store.commit_draft_owner_fact(
            session_id,
            f"draft.answer:{payload.question_id}",
            "draft.ask_user.answer",
            answer.model_dump(mode="json"),
            events=(
                DraftSessionEventSpec(
                    "session.question.answered", {"question_id": str(payload.question_id)}
                ),
            ),
            transaction=cast(RuntimeStoreTransaction, transaction),
        )
        return DraftCommandResult(
            session_id=session_id,
            status=MigrationSessionStatus.Drafting,
            revision=current,
            owner_receipt=receipt,
        )

    async def confirm_session(
        self,
        session_id: UUID,
        payload: SessionConfirmRequest,
        transaction: object,
    ) -> DraftCommandResult:
        owner, _seed, snapshot = await self._load_owner(session_id)
        if owner.freeze_receipt is not None:
            raise ApiError(409, "Draft session is already confirmed", "STALE_VERSION")
        current = owner.current_revision_number or 0
        if payload.revision != current:
            raise ApiError(409, "Draft revision is stale", "STALE_VERSION")
        try:
            revision = owner.flow.ledger.current_revision
            if revision is None:
                raise DraftConflictError("Draft has no current revision")
            unanswered = {
                question.question_id
                for question in owner.flow.ledger.questions
                if question.revision_id == revision.revision_id
            } - {answer.question_id for answer in owner.flow.ledger.answers}
            if unanswered:
                raise DraftConflictError("Draft has unanswered AskUser questions")
            _calibration_paths(owner, snapshot)
        except DraftConflictError as exc:
            raise ApiError(409, "Draft is not ready to confirm", "PHASE_STATUS_MISMATCH") from exc
        except ApiError:
            raise
        receipt_key = f"draft.confirm.requested:{current}"
        receipt = await self._store.commit_draft_owner_fact(
            session_id,
            receipt_key,
            "draft.confirm.requested",
            {"revision": current},
            events=(
                DraftSessionEventSpec(
                    "session.draft_revision.confirmation_requested",
                    {"revision": current},
                ),
            ),
            transaction=cast(RuntimeStoreTransaction, transaction),
        )
        return DraftCommandResult(
            session_id=session_id,
            status=MigrationSessionStatus.Drafting,
            revision=current,
            owner_receipt=receipt,
        )

    async def _load_owner(
        self, session_id: UUID
    ) -> tuple[DraftFlowOwner, dict[str, object], RegisteredSnapshot]:
        seed_record = await self._store.load_draft_owner_fact(session_id, "draft.session.created")
        if seed_record is None or seed_record[0].category != "draft.session.created":
            raise ApiError(404, "Draft session not found", "NOT_FOUND")
        seed = seed_record[1]
        try:
            principal_id = seed["principal_id"]
            project = RegisteredProject.model_validate(seed["source"])
            expected_oid = seed["snapshot_oid"]
            expected_modules = seed["module_files"]
            if (
                not isinstance(principal_id, str)
                or not isinstance(expected_oid, str)
                or not isinstance(expected_modules, Mapping)
                or not isinstance(seed.get("goal"), str)
            ):
                raise ValueError("invalid Draft seed")
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreCommitError("stored Draft session seed is invalid") from exc
        snapshot = await self._snapshot_resolver.resolve_snapshot(principal_id, project)
        if (
            snapshot is None
            or snapshot.project != project
            or snapshot.source.snapshot_oid != expected_oid
            or {path: list(files) for path, files in snapshot.module_files.items()}
            != dict(expected_modules)
        ):
            raise ApiError(404, "registered snapshot is unavailable", "NOT_FOUND")
        owner = DraftFlowOwner(
            draft_id=session_id,
            flow=DraftFlow(module_files=snapshot.module_files),
            store=self._store,
        )
        await owner.restore_ledger()
        return owner, seed, snapshot

    @staticmethod
    def _ensure_open(owner: DraftFlowOwner) -> None:
        if owner.freeze_receipt is not None:
            raise ApiError(409, "Draft session is already confirmed", "STALE_VERSION")


class RuntimeDraftGraphStarter:
    """Recover and advance a Draft graph only after its owner receipt commits."""

    supported_receipt_categories = frozenset(
        {
            "draft.session.created",
            "draft.turn.requested",
            "draft.ask_user.answer",
            "draft.confirm.requested",
            "draft.freeze",
        }
    )

    def __init__(
        self,
        store: RuntimeStore,
        snapshot_resolver: RegisteredSnapshotResolverPort,
        graph_assembly: RuntimeGraphAssembly,
    ) -> None:
        graph_assembly.infrastructure.validate_durable_persistence_bindings()
        self._commands = RuntimeDraftSessionCommands(store, snapshot_resolver)
        self._assembly = graph_assembly

    async def start_graph(self, draft_id: UUID, receipt_key: str) -> None:
        owner, seed, snapshot = await self._commands._load_owner(draft_id)
        receipt = await owner.load_fact(receipt_key)
        if receipt is None or receipt[0].category not in self.supported_receipt_categories:
            raise StoreCommitError("Draft graph-start receipt is missing or unsupported")
        if owner.freeze_receipt is not None or receipt[0].category == "draft.freeze":
            return
        graph = self._assembly.build_draft_graph(owner)
        if receipt[0].category == "draft.session.created":
            await self._start_initial_draft(graph, owner, seed, snapshot)
            return
        if receipt[0].category == "draft.turn.requested":
            turn = receipt[1]
            message = turn.get("message")
            revision = turn.get("revision")
            if not isinstance(message, str) or type(revision) is not int:
                raise StoreCommitError("stored Draft turn is invalid")
            await graph.propose_turn(
                message,
                message_key=receipt_key,
                revision_number=revision or 1,
            )
            return
        if receipt[0].category == "draft.ask_user.answer":
            answer = DraftAskUserAnswer.model_validate(receipt[1])
            await graph.answer_user(answer)
            answer_text = answer.selected_option or answer.free_text or ""
            await graph.propose_turn(
                f"Use this user answer to continue the Draft: {answer_text}",
                message_key=receipt_key,
                revision_number=owner.current_revision_number or 1,
            )
            return
        if receipt[0].category == "draft.confirm.requested":
            requested_revision = receipt[1].get("revision")
            current = owner.flow.ledger.current_revision
            if (
                type(requested_revision) is not int
                or current is None
                or current.revision_number != requested_revision
            ):
                raise StoreCommitError("Draft confirmation request targets a stale revision")
            if owner.flow.stage.value == "ALIGN":
                owner.flow.finalize_alignment()
            if owner.flow.stage.value == "DRAFT":
                owner.flow.begin_calibration()
            paths = _calibration_paths(owner, snapshot)
            goal = seed.get("goal")
            if not isinstance(goal, str):
                raise StoreCommitError("stored Draft goal is invalid")
            await graph.trial_translate(
                paths,
                {
                    path: (
                        "Run a disposable read-only translation calibration for this file. "
                        f"Goal: {goal}\nRevision: {current.revision_number}\nFile: {path}"
                    )
                    for path in paths
                },
            )
            owner.flow.confirm()
            await owner.persist_freeze_receipt()
            return
        raise StoreCommitError("Draft graph-start receipt category is unsupported")

    async def _start_initial_draft(
        self,
        graph: MigrationSessionGraph,
        owner: DraftFlowOwner,
        seed: Mapping[str, object],
        snapshot: RegisteredSnapshot,
    ) -> None:
        goal = seed.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise StoreCommitError("stored Draft goal is invalid")
        skeleton = build_domain_skeleton(snapshot.module_files)
        for domain in skeleton:
            await graph.explore_domain(
                str(domain.domain_path),
                "Explore the assigned source domain for this migration goal.\n"
                f"Goal: {goal}\nDomain: {domain.domain_path}\n"
                f"Files: {canonical_json_bytes(list(domain.files)).decode('utf-8')}\n"
                f"Snapshot: {snapshot.source.snapshot_oid}",
            )
        reports = [report.model_dump(mode="json") for report in owner.flow.reports]
        await graph.coordinate_exploration(
            "Validate and merge all domain reports into the exact machine-built coverage. "
            "Return an ExplorationMerge when coverage is complete.\n"
            f"Goal: {goal}\nReports: {canonical_json_bytes(reports).decode('utf-8')}"
        )
        await graph.propose_artifacts(
            f"Generate the initial four migration artifacts from the goal and validated "
            f"source exploration. Goal: {goal}\nReports: "
            f"{canonical_json_bytes(reports).decode('utf-8')}",
            message_key="draft.session.created",
            revision_number=1,
        )
        owner.flow.finalize_alignment()


def _calibration_paths(owner: DraftFlowOwner, snapshot: RegisteredSnapshot) -> tuple[str, ...]:
    from codemigrator.core.paths import normalize_repo_relative_paths

    revision = owner.flow.ledger.current_revision
    if revision is None:
        raise DraftConflictError("Draft calibration has no current revision")
    available = {path for paths in snapshot.module_files.values() for path in paths}
    risk_hotspots: list[str] = []
    for entry in revision.artifacts.understanding_dossier.risk_hotspots:
        anchors = entry.get("anchors") if isinstance(entry, Mapping) else None
        if not isinstance(anchors, (list, tuple)):
            continue
        for anchor in anchors:
            file_path = anchor.get("file") if isinstance(anchor, Mapping) else None
            if isinstance(file_path, str) and file_path in available:
                risk_hotspots.append(file_path)
    normalized_hotspots = normalize_repo_relative_paths(risk_hotspots)
    hotspot_set = set(normalized_hotspots)
    fallback_paths = normalize_repo_relative_paths(
        [path for path in available if path not in hotspot_set]
    )
    selected = (*normalized_hotspots, *fallback_paths)[:3]
    if len(selected) < 2:
        raise ApiError(
            409,
            "at least two registered source files are required for confirmation",
            "PHASE_STATUS_MISMATCH",
        )
    return tuple(selected)


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
DraftSessionCommandOwnerFactory = Callable[
    [RuntimeStore, ApiApplicationResources], DraftSessionCommandPort | None
]
DraftGraphStarterFactory = Callable[
    [
        RuntimeStore,
        ApiApplicationResources,
        DraftSessionCommandPort,
        RuntimeGraphAssembly | None,
    ],
    DraftGraphStarterPort | None,
]
RegisteredSnapshotResolverFactory = Callable[
    [RuntimeStore, ApiApplicationResources], RegisteredSnapshotResolverPort | None
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
    draft_command_owner_factory: DraftSessionCommandOwnerFactory | None = None,
    draft_graph_starter_factory: DraftGraphStarterFactory | None = None,
    registered_snapshot_resolver_factory: RegisteredSnapshotResolverFactory | None = None,
    stop_server: Callable[[], Awaitable[None]],
    shutdown: Callable[[], Awaitable[None]] | None = None,
    pool_server_settings: Mapping[str, str] | None = None,
) -> FastAPI:
    """Bind API ports to PostgreSQLRuntimeStore and the RunActor owner adapter."""

    if run_components_factory is not None and (preflight is not None or graph_starter is not None):
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
    ) -> RunCreationOwnerPort | ApiProductionCapabilities | None:
        runtime_store = cast(RuntimeStore, store)
        draft_owner_candidate = (
            draft_command_owner_factory(runtime_store, resources)
            if draft_command_owner_factory is not None
            else None
        )
        snapshot_resolver = (
            registered_snapshot_resolver_factory(runtime_store, resources)
            if registered_snapshot_resolver_factory is not None
            else None
        )
        components = (
            run_components_factory(
                runtime_store,
                resources.pool,
                resources.write_connection,
            )
            if run_components_factory is not None
            else None
        )
        if (
            draft_owner_candidate is None
            and draft_command_owner_factory is None
            and snapshot_resolver is not None
        ):
            draft_owner_candidate = RuntimeDraftSessionCommands(runtime_store, snapshot_resolver)
        draft_graph_starter = (
            draft_graph_starter_factory(
                runtime_store,
                resources,
                draft_owner_candidate,
                components.graph_assembly if components is not None else None,
            )
            if draft_graph_starter_factory is not None and draft_owner_candidate is not None
            else None
        )
        if (
            draft_graph_starter is None
            and draft_graph_starter_factory is None
            and draft_owner_candidate is not None
            and snapshot_resolver is not None
            and components is not None
        ):
            draft_graph_starter = RuntimeDraftGraphStarter(
                runtime_store,
                snapshot_resolver,
                components.graph_assembly,
            )
        # Draft commands are only a usable production capability when their
        # committed receipts can be handed to a durable graph continuation.
        draft_owner = draft_owner_candidate if draft_graph_starter is not None else None
        if run_components_factory is not None:
            assert components is not None
            components_run_owner = create_production_run_owner(runtime_store, components)
            return ApiProductionCapabilities(
                run_owner=cast(RunCreationOwnerPort, components_run_owner),
                run_read_projection=RuntimeRunReadModel(
                    runtime_store, components.graph_assembly.infrastructure.host_cas
                ),
                draft_owner=draft_owner,
                draft_graph_starter=draft_graph_starter,
                registered_snapshot_resolver=snapshot_resolver,
            )
        run_owner: RunCreationOwnerPort | None = None
        if preflight is not None and graph_starter is not None:
            run_owner = cast(
                RunCreationOwnerPort,
                RunCreationOwner(
                    store=runtime_store,
                    preflight=preflight,
                    graph_starter=graph_starter,
                ),
            )
        if run_owner is None and draft_owner is None:
            return None
        return ApiProductionCapabilities(
            run_owner=run_owner,
            draft_owner=draft_owner,
            draft_graph_starter=draft_graph_starter,
            registered_snapshot_resolver=snapshot_resolver,
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
    "DraftSessionCommandOwnerFactory",
    "DraftGraphStarterFactory",
    "RegisteredSnapshotResolverFactory",
    "create_production_app",
    "create_production_run_owner",
]
