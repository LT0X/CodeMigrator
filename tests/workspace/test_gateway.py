from __future__ import annotations

import json

import pytest

from codemigrator.core import Phase, SessionKind, StableErrorCode, load_resource
from codemigrator.workspace import (
    EditFileOutput,
    ReadFileOutput,
    ToolError,
    ToolGateway,
    WriteFileOutput,
)


def test_verify_denies_before_open_or_execution(roots, execute_context, write_scope) -> None:
    context = execute_context.model_copy(update={"phase": Phase.Verify})
    gateway = ToolGateway(context=context, roots=roots, write_scope=write_scope)

    result = gateway.dispatch({"tool": "ReadFile", "path": "src/a.py"})

    assert isinstance(result, ToolError)
    assert result.code is StableErrorCode.TOOL_PHASE_DENIED
    assert roots.open_count == 0


def test_write_edit_read_and_audit_do_not_emit_file_body(
    roots, execute_context, write_scope
) -> None:
    events = []
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        audit_sink=events.append,
    )

    written = gateway.dispatch(
        {"tool": "WriteFile", "path": "src/out.py", "content": "value = 1\n"}
    )
    assert isinstance(written, WriteFileOutput)
    edited = gateway.dispatch(
        {"tool": "EditFile", "path": "src/out.py", "old_text": "1", "new_text": "2"}
    )
    assert isinstance(edited, EditFileOutput)
    read = gateway.dispatch({"tool": "ReadFile", "path": "src/out.py"})
    assert isinstance(read, ReadFileOutput)
    assert "value = 2" in read.body
    assert all("value =" not in json.dumps(event.model_dump(mode="json")) for event in events)
    assert [event.point for event in events] == ["tool.call.pre", "tool.call.post"] * 3


def test_write_scope_rejects_existing_create_root_and_other_slice(
    roots, execute_context, write_scope
) -> None:
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)
    roots.workspace.write_atomic("generated/existing.py", b"old")

    rejected = gateway.dispatch(
        {"tool": "WriteFile", "path": "generated/existing.py", "content": "x"}
    )
    assert isinstance(rejected, ToolError)
    assert rejected.code is StableErrorCode.WRITE_SCOPE_VIOLATION

    other = gateway.with_other_write_scopes(
        [
            {"write_paths": ["generated/other.py"], "create_roots": []},
        ]
    )
    rejected_other = other.dispatch(
        {"tool": "WriteFile", "path": "generated/other.py", "content": "x"}
    )
    assert isinstance(rejected_other, ToolError)
    assert rejected_other.code is StableErrorCode.WRITE_SCOPE_VIOLATION


def test_successful_structured_write_can_be_forwarded_to_lifecycle_ledger(
    roots, execute_context, write_scope
) -> None:
    operations = []
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        operation_sink=operations.append,
    )

    result = gateway.dispatch({"tool": "WriteFile", "path": "src/out.py", "content": "x"})

    assert isinstance(result, WriteFileOutput)
    assert len(operations) == 1
    assert operations[0].path == "src/out.py"
    assert operations[0].bytes_written == 1


def test_read_phase_only_uses_snapshot(roots, execute_context, write_scope) -> None:
    roots.snapshot.write_atomic("src/a.py", b"a\n")
    plan = execute_context.model_copy(
        update={"phase": Phase.Plan, "session_kind": SessionKind.PlanAuxiliary}
    )
    gateway = ToolGateway(context=plan, roots=roots, write_scope=write_scope)
    assert isinstance(gateway.dispatch({"tool": "ReadFile", "path": "src/a.py"}), ReadFileOutput)
    denied = gateway.dispatch({"tool": "ReadFile", "path": "src/out.py"})
    assert isinstance(denied, ToolError)
    assert denied.code is StableErrorCode.READ_OUT_OF_SCOPE


def test_gateway_rejects_policy_digest_mismatch_before_ready(
    roots, execute_context, write_scope
) -> None:
    context = execute_context.model_copy(update={"phase_policy_sha256": "0" * 64})

    with pytest.raises(ValueError, match="policy digest"):
        ToolGateway(context=context, roots=roots, write_scope=write_scope)


def test_gateway_clones_reuse_the_frozen_policy_document(
    roots, execute_context, write_scope
) -> None:
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)
    clone = gateway.with_other_write_scopes([])

    assert gateway.policy_sha256 == load_resource("core://phase-tool-policy/v2").sha256
    assert gateway.policy_load_count == 1
    assert clone.policy_load_count == 0
