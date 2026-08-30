from __future__ import annotations

import hashlib

from codemigrator.core import StableErrorCode
from codemigrator.workspace import (
    CallbackExecEngine,
    ExecExecution,
    InMemoryCasStore,
    QuerySourceAstOutput,
    ShellExecution,
    ShellOutput,
    ToolError,
    ToolGateway,
)


class Query:
    def query(self, request):
        return {"echo": request.kind}


class Shell:
    def run(self, call, workspace_root):
        assert workspace_root.endswith("workspace")
        assert call.timeout_secs == 600
        return ShellExecution(exit_code=7, stdout="diagnostic", stderr="")


def test_query_shell_and_exec_are_ports_and_exec_calls_return_through_gateway(
    roots, execute_context, write_scope
) -> None:
    events = []

    def execute(script, bridge):
        result = bridge.call(
            {
                "tool": "QuerySourceAst",
                "request": {"kind": "FIND_SYMBOL", "symbol": "name"},
            }
        )
        assert isinstance(result, QuerySourceAstOutput)
        return ExecExecution(result='{"ok":true}', step_count=1)

    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        query_port=Query(),
        shell_runner=Shell(),
        exec_engine=CallbackExecEngine(execute),
        audit_sink=events.append,
    )
    query = gateway.dispatch(
        {
            "tool": "QuerySourceAst",
            "request": {"kind": "FIND_SYMBOL", "symbol": "name"},
        }
    )
    shell = gateway.dispatch({"tool": "Shell", "command": "pytest -q"})
    execution = gateway.dispatch({"tool": "Exec", "script": "tools.query_source_ast()"})

    assert isinstance(query, QuerySourceAstOutput)
    assert isinstance(shell, ShellOutput)
    assert shell.exit_code == 7
    assert execution.step_count == 1
    assert [event.point for event in events].count("tool.call.pre") == 4
    assert [event.point for event in events].count("tool.call.post") == 4
    exec_event = events[-1]
    assert exec_event.script == "tools.query_source_ast()"
    assert exec_event.script_sha256 == hashlib.sha256(b"tools.query_source_ast()").hexdigest()
    assert events[3].command_sha256 == hashlib.sha256(b"pytest -q").hexdigest()
    assert events[3].exit_code == 7
    exec_post = next(event for event in events if event.tool == "Exec" and event.step_count == 1)
    assert exec_post.step_count == 1


def test_query_port_error_codes_are_preserved(roots, execute_context, write_scope) -> None:
    class TimedQuery:
        def query(self, request):
            raise ValueError("QUERY_TIMEOUT: query exceeded timeout")

    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        query_port=TimedQuery(),
    )

    result = gateway.dispatch(
        {
            "tool": "QuerySourceAst",
            "request": {"kind": "SEARCH_CONTEXT", "query": "needle"},
        }
    )

    assert isinstance(result, ToolError)
    assert result.code is StableErrorCode.QUERY_TIMEOUT


def test_exec_timeout_and_shell_timeout_are_typed_failures(
    roots, execute_context, write_scope
) -> None:
    class TimedShell:
        def run(self, call, workspace_root):
            return ShellExecution(exit_code=-9, timed_out=True)

    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        shell_runner=TimedShell(),
        exec_engine=CallbackExecEngine(
            lambda script, bridge: ExecExecution(result="", step_count=0, timed_out=True)
        ),
    )
    shell = gateway.dispatch({"tool": "Shell", "command": "long"})
    execution = gateway.dispatch({"tool": "Exec", "script": "while (true) {}"})
    assert isinstance(shell, ToolError) and shell.code is StableErrorCode.SHELL_TIMEOUT
    assert isinstance(execution, ToolError) and execution.code is StableErrorCode.EXEC_TIMEOUT


def test_cas_read_is_run_scoped_and_line_bounded(roots, execute_context, write_scope) -> None:
    digest = "a" * 64
    roots = roots.__class__(
        snapshot=roots.snapshot,
        workspace=roots.workspace,
        contract_roots=roots.contract_roots,
        verified=roots.verified,
        cas=InMemoryCasStore({f"cas://{digest}": b"one\ntwo\nthree\n"}),
    )
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)
    output = gateway.dispatch(
        {"tool": "ReadFile", "cas": f"cas://{digest}", "range": {"start_line": 2, "end_line": 2}}
    )
    assert output.body.endswith("two")
    assert output.total_lines == 3
