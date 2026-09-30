from __future__ import annotations

import uuid

from codemigrator.core import Phase, SessionKind, StableErrorCode, load_resource
from codemigrator.workspace import (
    CallbackExecEngine,
    ExecExecution,
    GatewayContext,
    GatewayRoots,
    SecureRoot,
    ToolError,
    ToolGateway,
)


def test_draft_gateway_has_no_run_or_writable_workspace(tmp_path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "source.py").write_text("answer = 42\n", encoding="utf-8")
    draft_id = uuid.uuid4()
    agent_run_id = uuid.uuid4()
    context = GatewayContext(
        draft_id=draft_id,
        agent_run_id=agent_run_id,
        phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
        phase=Phase.Plan,
        session_kind=SessionKind.ExploreCoordinator,
    )
    audit = []
    gateway = ToolGateway(
        context=context,
        roots=GatewayRoots(snapshot=SecureRoot("snapshot", snapshot), workspace=None),
        audit_sink=audit.append,
    )

    read = gateway.dispatch({"tool": "ReadFile", "path": "source.py"})
    write = gateway.dispatch({"tool": "WriteFile", "path": "source.py", "content": "changed"})

    assert not isinstance(read, ToolError)
    assert read.body.endswith("answer = 42")
    assert audit and all(
        event.run_id is None and event.draft_id == draft_id and event.agent_run_id == agent_run_id
        for event in audit
    )
    assert isinstance(write, ToolError)
    assert write.code is StableErrorCode.TOOL_PHASE_DENIED
    assert not (snapshot / "source.py").read_text(encoding="utf-8").startswith("changed")


def test_gateway_context_requires_exactly_one_owner() -> None:
    policy = load_resource("core://phase-tool-policy/v2").sha256
    common = {
        "phase_policy_sha256": policy,
        "phase": Phase.Plan,
        "session_kind": SessionKind.PlanAuxiliary,
    }
    try:
        GatewayContext(**common)
    except ValueError:
        pass
    else:
        raise AssertionError("gateway context must identify a Run or Draft owner")

    try:
        GatewayContext(run_id=uuid.uuid4(), draft_id=uuid.uuid4(), **common)
    except ValueError:
        pass
    else:
        raise AssertionError("gateway context cannot have two owners")


def test_draft_exec_only_has_the_read_only_gateway_bridge(tmp_path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    captured = []

    def execute(script, bridge):
        captured.append(bridge.call({"tool": "WriteFile", "path": "generated.py", "content": "x"}))
        return ExecExecution(result="attempted", step_count=1)

    gateway = ToolGateway(
        context=GatewayContext(
            draft_id=uuid.uuid4(),
            agent_run_id=uuid.uuid4(),
            phase_policy_sha256=load_resource("core://phase-tool-policy/v2").sha256,
            phase=Phase.Plan,
            session_kind=SessionKind.ExploreCoordinator,
        ),
        roots=GatewayRoots(snapshot=SecureRoot("snapshot", snapshot), workspace=None),
        exec_engine=CallbackExecEngine(execute),
    )

    result = gateway.dispatch({"tool": "Exec", "script": "read only"})

    assert not isinstance(result, ToolError)
    assert len(captured) == 1
    assert isinstance(captured[0], ToolError)
    assert captured[0].code is StableErrorCode.TOOL_PHASE_DENIED
    assert not (snapshot / "generated.py").exists()
