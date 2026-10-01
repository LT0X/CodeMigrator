from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from codemigrator.analysis import InMemorySnapshotSource
from codemigrator.api.draft_host import RegisteredSnapshot
from codemigrator.api.dto import SessionCreateRequest, SessionMessageRequest
from codemigrator.asgi import RuntimeDraftSessionCommands
from codemigrator.core import MigrationSessionStatus, RegisteredProject
from codemigrator.runtime.contracts import RuntimeStoreTransaction
from codemigrator.runtime.store import InMemoryRuntimeStore


class _SnapshotResolver:
    def __init__(self, snapshot: RegisteredSnapshot) -> None:
        self.snapshot = snapshot

    async def resolve_snapshot(self, principal_id: str, project: RegisteredProject):
        assert principal_id == "principal-1"
        return self.snapshot if project == self.snapshot.project else None


def _snapshot() -> RegisteredSnapshot:
    return RegisteredSnapshot(
        project=RegisteredProject(project_id=uuid4(), snapshot_id=uuid4()),
        source=InMemorySnapshotSource("a" * 40, {"src/main.py": b"print('x')"}),
        module_files={".": ("src/main.py",)},
    )


@pytest.mark.asyncio
async def test_create_session_persists_seed_in_command_transaction() -> None:
    store = InMemoryRuntimeStore()
    snapshot = _snapshot()
    owner = RuntimeDraftSessionCommands(store, _SnapshotResolver(snapshot))
    request = SessionCreateRequest(
        kind="DRAFT",
        payload={"source": snapshot.project.model_dump(mode="json"), "goal": "Migrate module"},
    )
    transaction = RuntimeStoreTransaction(store, None)

    result = await owner.create_session("principal-1", request, snapshot, transaction)
    transaction.finish(committed=True)

    receipt, fact = await store.load_draft_owner_fact(
        result.session_id, result.owner_receipt.receipt_key
    )
    assert receipt == result.owner_receipt
    assert receipt.category == "draft.session.created"
    assert fact["principal_id"] == "principal-1"
    assert fact["goal"] == "Migrate module"
    assert result.status is MigrationSessionStatus.Drafting
    assert result.revision == 0


@pytest.mark.asyncio
async def test_message_command_persists_private_turn_input_before_graph_handoff() -> None:
    store = InMemoryRuntimeStore()
    snapshot = _snapshot()
    owner = RuntimeDraftSessionCommands(store, _SnapshotResolver(snapshot))
    create_transaction = RuntimeStoreTransaction(store, None)
    created = await owner.create_session(
        "principal-1",
        SessionCreateRequest(
            kind="DRAFT",
            payload={"source": snapshot.project.model_dump(mode="json"), "goal": "Migrate"},
        ),
        snapshot,
        create_transaction,
    )
    create_transaction.finish(committed=True)

    message_transaction = RuntimeStoreTransaction(store, None)
    result = await owner.send_message(
        created.session_id,
        SessionMessageRequest(message="Use a pure function", revision=0),
        message_transaction,
    )
    message_transaction.finish(committed=True)

    receipt, fact = await store.load_draft_owner_fact(
        result.session_id, result.owner_receipt.receipt_key
    )
    assert receipt.category == "draft.turn.requested"
    assert fact["message"] == "Use a pure function"
    assert result.revision == 0
    assert await store.read_draft_session_events(result.session_id, 0) == ()


def test_calibration_paths_prioritize_registered_risk_hotspots() -> None:
    from codemigrator.asgi import _calibration_paths
    from tests.draft.conftest import make_artifacts

    dossier = make_artifacts().understanding_dossier
    risky_dossier = dossier.model_copy(
        update={
            "risk_hotspots": [
                {
                    "kind": "risk",
                    "content": "A synthetic hotspot",
                    "anchors": [{"file": "src/c.py", "start_line": 1, "end_line": 1}],
                    "advisory": False,
                }
            ]
        }
    )
    artifacts = make_artifacts().model_copy(update={"understanding_dossier": risky_dossier})
    revision = SimpleNamespace(artifacts=artifacts)
    owner = SimpleNamespace(flow=SimpleNamespace(ledger=SimpleNamespace(current_revision=revision)))
    snapshot = RegisteredSnapshot(
        project=RegisteredProject(project_id=uuid4(), snapshot_id=uuid4()),
        source=InMemorySnapshotSource(
            "a" * 40,
            {
                "src/a.py": b"a",
                "src/b.py": b"b",
                "src/c.py": b"c",
            },
        ),
        module_files={".": ("src/a.py", "src/b.py", "src/c.py")},
    )

    assert _calibration_paths(owner, snapshot) == ("src/c.py", "src/a.py", "src/b.py")
