from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from codemigrator.core import (
    Phase,
    SessionKind,
    WriteScope,
    WriteScopeOut,
    load_resource,
)
from codemigrator.workspace import GatewayContext, GatewayRoots, SecureRoot


@pytest.fixture
def run_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def roots(tmp_path: Path) -> Iterator[GatewayRoots]:
    snapshot = tmp_path / "snapshot"
    workspace = tmp_path / "workspace"
    contracts = tmp_path / "contracts"
    verified = tmp_path / "verified"
    for root in (snapshot, workspace, contracts, verified):
        root.mkdir()
    (snapshot / "src").mkdir()
    (workspace / "src").mkdir()
    (workspace / "generated").mkdir()
    yield GatewayRoots(
        snapshot=SecureRoot("snapshot", snapshot),
        workspace=SecureRoot("workspace", workspace),
        contract_roots=(SecureRoot("contracts", contracts),),
        verified=SecureRoot("verified", verified),
    )


@pytest.fixture
def execute_context(run_id: uuid.UUID) -> GatewayContext:
    return GatewayContext(
        run_id=run_id,
        phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
        phase=Phase.Execute,
        session_kind=SessionKind.Implementation,
        slice_id=uuid.uuid4(),
        generation=0,
    )


@pytest.fixture
def write_scope() -> WriteScope:
    return WriteScope(
        out=WriteScopeOut(
            write_paths=["src/out.py"],
            create_roots=["generated"],
        )
    )
