from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from codemigrator.api.backend import DraftCommandResult, ProductionApiBackend
from codemigrator.api.deps import ApiRequest
from codemigrator.api.dto import SessionCreateRequest
from codemigrator.api.problems import ApiError
from codemigrator.core import MigrationSessionStatus
from codemigrator.runtime.store import InMemoryRuntimeStore


class CommittingDraftCommands:
    def __init__(self, store: InMemoryRuntimeStore) -> None:
        self.store = store
        self.draft_id = uuid4()

    async def create_session(self, payload, transaction):  # type: ignore[no-untyped-def]
        del payload
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
    return ApiRequest(
        operation="create_session",
        principal_id="test-user",
        payload=SessionCreateRequest(kind="DRAFT", payload={}),
    )


async def _create_draft(backend: ProductionApiBackend, owner: CommittingDraftCommands) -> object:
    return await backend.execute_idempotent(
        _create_request(owner),
        route="/api/v1/sessions",
        key="draft-create",
        canonical_body=b'{"kind":"DRAFT","payload":{}}',
        status_code=201,
    )


@pytest.mark.asyncio
async def test_draft_graph_start_waits_for_committed_api_owner_receipt() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    starter = RecordingDraftGraphStarter(store)
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
    )

    response = await _create_draft(backend, owner)
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
    first_starter = RecordingDraftGraphStarter(store, fail=True)
    first_backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=first_starter,
    )

    await _create_draft(first_backend, owner)
    await asyncio.wait_for(first_starter.started.wait(), timeout=2)
    pending = await store.list_pending_draft_graph_starts()
    assert pending == ((owner.draft_id, "draft.api-command:create"),)
    await first_backend.close()

    recovered_starter = RecordingDraftGraphStarter(store)
    restarted_backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=recovered_starter,
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
    starter = RecordingDraftGraphStarter(store)
    starter.supported_receipt_categories = frozenset({"draft.ask_user.answer"})
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
    )

    with pytest.raises(ApiError) as raised:
        await _create_draft(backend, owner)

    assert raised.value.status_code == 503
    assert await store.list_draft_owner_facts(owner.draft_id) == ()
    assert await store.list_pending_draft_graph_starts() == ()
    await backend.close()


@pytest.mark.asyncio
async def test_backend_close_cancels_and_drains_draft_graph_start_tasks() -> None:
    store = InMemoryRuntimeStore()
    owner = CommittingDraftCommands(store)
    starter = BlockingDraftGraphStarter(store)
    backend = ProductionApiBackend(
        store,
        draft_owner=owner,
        draft_graph_starter=starter,
    )

    await _create_draft(backend, owner)
    await asyncio.wait_for(starter.started.wait(), timeout=2)
    await backend.close()

    assert starter.cancelled.is_set()
    assert backend._draft_graph_start_tasks == {}
