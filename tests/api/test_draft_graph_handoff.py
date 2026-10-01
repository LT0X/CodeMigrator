from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from codemigrator.analysis import InMemorySnapshotSource
from codemigrator.api.backend import DraftCommandResult, ProductionApiBackend
from codemigrator.api.deps import ApiRequest
from codemigrator.api.draft_host import RegisteredSnapshot
from codemigrator.api.dto import SessionCreateRequest
from codemigrator.api.problems import ApiError
from codemigrator.core import MigrationSessionStatus, RegisteredProject
from codemigrator.runtime.store import InMemoryRuntimeStore


class CommittingDraftCommands:
    def __init__(self, store: InMemoryRuntimeStore) -> None:
        self.store = store
        self.draft_id = uuid4()

    async def create_session(self, principal_id, payload, snapshot, transaction):  # type: ignore[no-untyped-def]
        assert principal_id == "test-user"
        del payload
        assert snapshot.project == self.source
        receipt = await self.store.commit_draft_owner_fact(
            self.draft_id,
            "draft.api-command:create",
            "draft.api-command",
            {"operation": "create_session"},
            transaction=transaction,
        )
        return DraftCommandResult(
            session_id=self.draft_id,
            status=MigrationSessionStatus.Drafting,
            revision=0,
            owner_receipt=receipt,
        )

    @property
    def source(self) -> RegisteredProject:
        return self.project


class SyntheticSnapshotResolver:
    def __init__(self, project: RegisteredProject) -> None:
        self.project = project

    async def resolve_snapshot(self, principal_id, project):  # type: ignore[no-untyped-def]
        assert principal_id == "test-user"
        if project != self.project:
            return None
        return RegisteredSnapshot(
            project=project,
            source=InMemorySnapshotSource(str(project.snapshot_id), {"src/main.py": b"pass\n"}),
            module_files={".": ("src/main.py",)},
        )


def _project() -> RegisteredProject:
    return RegisteredProject(project_id=uuid4(), snapshot_id=uuid4())


class RecordingDraftGraphStarter:
    supported_receipt_categories = frozenset({"draft.api-command"})

    def __init__(self, store: InMemoryRuntimeStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.started = asyncio.Event()
        self.receipts: list[tuple[UUID, str]] = []
        self.saw_committed_owner_fact = False

    async def start_graph(self, draft_id: UUID, receipt_key: str) -> None:
        self.receipts.append((draft_id, receipt_key))
        self.saw_committed_owner_fact = (
            await self.store.load_draft_owner_fact(draft_id, receipt_key) is not None
        )
        self.started.set()
        if self.fail:
            raise RuntimeError("synthetic Draft graph start failure")


class BlockingDraftGraphStarter(RecordingDraftGraphStarter):
    def __init__(self, store: InMemoryRuntimeStore) -> None:
        super().__init__(store)
        self.cancelled = asyncio.Event()

    async def start_graph(self, draft_id: UUID, receipt_key: str) -> None:
        await super().start_graph(draft_id, receipt_key)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _create_request(draft_owner: CommittingDraftCommands) -> ApiRequest:
    draft_owner.project = _project()
    return ApiRequest(
        operation="create_session",
        principal_id="test-user",
        payload=SessionCreateRequest(
            kind="DRAFT",
            payload={
                "source": draft_owner.project.model_dump(mode="json"),
                "goal": "Translate the source project.",
            },
        ),
    )


async def _create_draft(backend: ProductionApiBackend, owner: CommittingDraftCommands) -> object:
    request = _create_request(owner)
    return await backend.execute_idempotent(
        request,
        route="/api/v1/sessions",
        key="draft-create",
        canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
        status_code=201,
    )


@pytest.mark.asyncio
async def test_draft_graph_start_waits_for_committed_api_owner_receipt() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    starter = RecordingDraftGraphStarter(store)
    request = _create_request(owner)
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
        registered_snapshot_resolver=SyntheticSnapshotResolver(owner.project),
    )

    response = await backend.execute_idempotent(
        request,
        route="/api/v1/sessions",
        key="draft-create",
        canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
        status_code=201,
    )
    await asyncio.wait_for(starter.started.wait(), timeout=2)

    assert response == {
        "session_id": str(owner.draft_id),
        "status": MigrationSessionStatus.Drafting.value,
        "revision": 0,
    }
    assert starter.saw_committed_owner_fact is True
    assert starter.receipts == [(owner.draft_id, "draft.api-command:create")]
    assert await store.list_pending_draft_graph_starts() == ()
    await backend.close()


@pytest.mark.asyncio
async def test_pending_draft_graph_handoff_recovers_after_backend_restart() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    request = _create_request(owner)
    first_starter = RecordingDraftGraphStarter(store, fail=True)
    first_backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=first_starter,
        registered_snapshot_resolver=SyntheticSnapshotResolver(owner.project),
    )

    await first_backend.execute_idempotent(
        request,
        route="/api/v1/sessions",
        key="draft-create",
        canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
        status_code=201,
    )
    await asyncio.wait_for(first_starter.started.wait(), timeout=2)
    pending = await store.list_pending_draft_graph_starts()
    assert pending == ((owner.draft_id, "draft.api-command:create"),)
    await first_backend.close()

    recovered_starter = RecordingDraftGraphStarter(store)
    restarted_backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=recovered_starter,
        registered_snapshot_resolver=SyntheticSnapshotResolver(owner.project),
    )
    await restarted_backend.recover_pending_graph_starts()
    await asyncio.wait_for(recovered_starter.started.wait(), timeout=2)

    assert recovered_starter.saw_committed_owner_fact is True
    assert recovered_starter.receipts == [(owner.draft_id, "draft.api-command:create")]
    assert await store.list_pending_draft_graph_starts() == ()
    await restarted_backend.close()


@pytest.mark.asyncio
async def test_unsupported_draft_receipt_rolls_back_without_leaving_pending_handoff() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    request = _create_request(owner)
    starter = RecordingDraftGraphStarter(store)
    starter.supported_receipt_categories = frozenset({"draft.ask_user.answer"})
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
        registered_snapshot_resolver=SyntheticSnapshotResolver(owner.project),
    )

    with pytest.raises(ApiError) as raised:
        await backend.execute_idempotent(
            request,
            route="/api/v1/sessions",
            key="draft-create",
            canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
            status_code=201,
        )

    assert raised.value.status_code == 503
    assert await store.list_draft_owner_facts(owner.draft_id) == ()
    assert await store.list_pending_draft_graph_starts() == ()
    await backend.close()


@pytest.mark.asyncio
async def test_draft_create_fails_closed_without_registered_snapshot_resolver() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    request = _create_request(owner)
    backend = ProductionApiBackend(store, draft_owner=owner)

    with pytest.raises(ApiError) as raised:
        await backend.execute_idempotent(
            request,
            route="/api/v1/sessions",
            key="draft-create",
            canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
            status_code=201,
        )

    assert raised.value.status_code == 503
    assert await store.list_draft_owner_facts(owner.draft_id) == ()
    await backend.close()


@pytest.mark.asyncio
async def test_backend_close_cancels_and_drains_draft_graph_start_tasks() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    starter = BlockingDraftGraphStarter(store)
    request = _create_request(owner)
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
        registered_snapshot_resolver=SyntheticSnapshotResolver(owner.project),
    )

    await backend.execute_idempotent(
        request,
        route="/api/v1/sessions",
        key="draft-create",
        canonical_body=b'{"kind":"DRAFT","payload":{"goal":"Translate the source project."}}',
        status_code=201,
    )
    await asyncio.wait_for(starter.started.wait(), timeout=2)
    await backend.close()

    assert starter.cancelled.is_set()
    assert backend._draft_graph_start_tasks == {}
