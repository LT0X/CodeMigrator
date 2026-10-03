from __future__ import annotations

import json
import time

import pytest

from codemigrator.workspace import (
    ExecExecution,
    ExecOutput,
    ExecToolBridge,
    QuickJSExecEngine,
    ShellExecution,
    ToolError,
    ToolGateway,
)


class Query:
    def query(self, request):
        return {"symbol": request.symbol, "found": request.symbol == "known"}


class RecordingBridge:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def call(self, raw_call):
        self.calls.append(dict(raw_call))
        return {"tool": raw_call["tool"], "ordinal": len(self.calls)}


def _run(engine: QuickJSExecEngine, script: str, bridge) -> ExecExecution:
    return engine.execute(script, bridge, timeout_secs=3)


def test_javascript_loop_condition_and_gateway_results_are_returned(
    roots, execute_context, write_scope
) -> None:
    events = []
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        query_port=Query(),
        audit_sink=events.append,
    )
    result = _run(
        QuickJSExecEngine(),
        """
        const results = [];
        for (const symbol of ["known", "missing"]) {
          const found = await tools.query_source_ast({kind: "FIND_SYMBOL", symbol});
          if (found.result.found) results.push(found.result.symbol);
        }
        return {found: results, count: results.length};
        """,
        ExecToolBridge(gateway),
    )

    assert result.error_message is None
    assert result.step_count == 2
    assert json.loads(result.result) == {"found": ["known"], "count": 1}
    assert [event.tool for event in events if event.point == "tool.call.pre"] == [
        "QuerySourceAst",
        "QuerySourceAst",
    ]


def test_promise_all_gateway_writes_are_dispatched_in_source_order(
    roots, execute_context, write_scope
) -> None:
    operations = []
    events = []
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        operation_sink=operations.append,
        audit_sink=events.append,
    )
    result = _run(
        QuickJSExecEngine(),
        """
        const written = await Promise.all([
          tools.write_file({path: "generated/first.py", content: "first"}),
          tools.write_file({path: "generated/second.py", content: "second"}),
          tools.write_file({path: "generated/third.py", content: "third"})
        ]);
        return written.map((item) => item.path);
        """,
        ExecToolBridge(gateway),
    )

    assert result.error_message is None
    assert result.step_count == 3
    assert json.loads(result.result) == [
        "generated/first.py",
        "generated/second.py",
        "generated/third.py",
    ]
    assert [operation.path for operation in operations] == [
        "generated/first.py",
        "generated/second.py",
        "generated/third.py",
    ]
    assert [event.tool for event in events if event.point == "tool.call.pre"] == [
        "WriteFile",
        "WriteFile",
        "WriteFile",
    ]
    assert (roots.workspace.absolute_path("generated/first.py")).read_text() == "first"
    assert (roots.workspace.absolute_path("generated/second.py")).read_text() == "second"
    assert (roots.workspace.absolute_path("generated/third.py")).read_text() == "third"


def test_gateway_exec_uses_quickjs_and_maps_script_errors(
    roots, execute_context, write_scope
) -> None:
    events = []
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        exec_engine=QuickJSExecEngine(),
        audit_sink=events.append,
    )
    success = gateway.dispatch(
        {
            "tool": "Exec",
            "script": (
                "const result = await tools.write_file({path: 'generated/gateway.py', "
                "content: 'ok'}); "
                "return result.path;"
            ),
        }
    )
    failure = gateway.dispatch({"tool": "Exec", "script": "const value = ;"})

    assert isinstance(success, ExecOutput)
    assert success.step_count == 1
    assert json.loads(success.result) == "generated/gateway.py"
    assert roots.workspace.absolute_path("generated/gateway.py").read_text() == "ok"
    assert isinstance(failure, ToolError)
    assert failure.code.value == "EXEC_SCRIPT_ERROR"
    assert failure.facts and failure.facts[0].get("line") == 1
    assert [event.tool for event in events if event.point == "tool.call.pre"] == [
        "Exec",
        "WriteFile",
        "Exec",
    ]


def test_tool_error_is_returned_as_a_tool_result() -> None:
    bridge = RecordingBridge()
    result = _run(
        QuickJSExecEngine(),
        "const read = await tools.read_file({path: " + json.dumps("src/a.py") + "}); return read;",
        bridge,
    )

    assert result.error_message is None
    assert result.step_count == 1
    assert bridge.calls == [{"tool": "ReadFile", "path": "src/a.py"}]
    assert json.loads(result.result) == {"tool": "ReadFile", "ordinal": 1}


def test_gateway_rejection_is_propagated_to_the_javascript_caller(
    roots, execute_context, write_scope
) -> None:
    gateway = ToolGateway(context=execute_context, roots=roots, write_scope=write_scope)
    result = _run(
        QuickJSExecEngine(),
        "return await tools.write_file({path: 'src/forbidden.py', content: 'bad'});",
        ExecToolBridge(gateway),
    )

    assert result.error_message is None
    assert result.step_count == 1
    assert json.loads(result.result)["code"] == "WRITE_SCOPE_VIOLATION"
    assert not roots.workspace.absolute_path("src/forbidden.py").exists()


def test_exec_deadline_caps_an_embedded_shell_timeout(roots, execute_context, write_scope) -> None:
    class SlowShell:
        def __init__(self) -> None:
            self.timeouts: list[int] = []

        def run(self, call, workspace_root):
            assert workspace_root.endswith("workspace")
            self.timeouts.append(call.timeout_secs)
            time.sleep(call.timeout_secs + 0.05)
            return ShellExecution(exit_code=-9, timed_out=True)

    shell = SlowShell()
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        shell_runner=shell,
    )
    started = time.monotonic()
    result = QuickJSExecEngine(max_timeout_secs=3).execute(
        "return await tools.shell({command: 'long command', timeout_secs: 600});",
        ExecToolBridge(gateway),
        timeout_secs=10,
    )
    elapsed = time.monotonic() - started

    assert shell.timeouts and 1 <= shell.timeouts[0] <= 3
    assert elapsed < 3
    assert result.error_message is None
    assert json.loads(result.result)["code"] == "SHELL_TIMEOUT"


def test_exec_caps_the_default_embedded_shell_timeout(roots, execute_context, write_scope) -> None:
    class RecordingShell:
        def __init__(self) -> None:
            self.timeout: int | None = None

        def run(self, call, workspace_root):
            self.timeout = call.timeout_secs
            return ShellExecution(exit_code=0, stdout="ok")

    shell = RecordingShell()
    gateway = ToolGateway(
        context=execute_context,
        roots=roots,
        write_scope=write_scope,
        shell_runner=shell,
    )
    result = QuickJSExecEngine(max_timeout_secs=3).execute(
        "return await tools.shell({command: 'short command'});",
        ExecToolBridge(gateway),
        timeout_secs=3,
    )

    assert shell.timeout is not None and 1 <= shell.timeout <= 2
    assert result.error_message is None
    assert json.loads(result.result)["stdout"] == "ok"


def test_only_the_explicit_tool_bridge_is_exposed() -> None:
    result = _run(
        QuickJSExecEngine(),
        "return [typeof std, typeof os, typeof process, typeof require, "
        "typeof fetch, typeof Deno];",
        RecordingBridge(),
    )

    assert result.error_message is None
    assert json.loads(result.result) == ["undefined"] * 6


def test_syntax_error_is_reported_with_source_line() -> None:
    result = _run(QuickJSExecEngine(), "const ok = true;\nif ( {", RecordingBridge())

    assert result.error_message is not None
    assert result.error_line == 2
    assert result.timed_out is False


def test_script_failure_preserves_exception_message() -> None:
    result = _run(
        QuickJSExecEngine(),
        "throw new Error('expected failure');",
        RecordingBridge(),
    )

    assert result.error_message is not None
    assert "Error: expected failure" in result.error_message


def test_cpu_timeout_is_reported_and_does_not_poison_next_context() -> None:
    engine = QuickJSExecEngine(max_timeout_secs=1)
    timed = engine.execute("while (true) {}", RecordingBridge(), timeout_secs=3)

    assert timed.timed_out is True
    assert timed.error_message is None

    fresh = engine.execute("return typeof leaked;", RecordingBridge(), timeout_secs=2)
    assert fresh.error_message is None
    assert json.loads(fresh.result) == "undefined"


def test_memory_limit_is_reported_as_a_script_failure() -> None:
    engine = QuickJSExecEngine(max_memory_bytes=2 * 1024 * 1024)
    result = engine.execute(
        "const values = []; while (true) values.push(new Array(10000).fill('x'));",
        RecordingBridge(),
        timeout_secs=3,
    )

    assert result.error_message is not None
    assert result.timed_out is False
    assert result.step_count == 0


def test_stack_limit_is_reported_as_a_script_failure() -> None:
    engine = QuickJSExecEngine(max_stack_bytes=32 * 1024)
    result = engine.execute(
        "function recurse() { return recurse(); } return recurse();",
        RecordingBridge(),
        timeout_secs=3,
    )

    assert result.error_message is not None
    assert result.timed_out is False


def test_resource_configuration_cannot_raise_production_caps() -> None:
    assert QuickJSExecEngine().max_memory_bytes == 64 * 1024 * 1024
    assert QuickJSExecEngine().max_stack_bytes == 1024 * 1024
    assert QuickJSExecEngine().max_timeout_secs == 60
    assert QuickJSExecEngine().max_tool_calls == 200
    with pytest.raises(ValueError, match="max_memory_bytes"):
        QuickJSExecEngine(max_memory_bytes=QuickJSExecEngine.MAX_MEMORY_BYTES + 1)
    with pytest.raises(ValueError, match="max_stack_bytes"):
        QuickJSExecEngine(max_stack_bytes=QuickJSExecEngine.MAX_STACK_BYTES + 1)
    with pytest.raises(ValueError, match="max_timeout_secs"):
        QuickJSExecEngine(max_timeout_secs=QuickJSExecEngine.MAX_TIMEOUT_SECS + 1)
    with pytest.raises(ValueError, match="max_tool_calls"):
        QuickJSExecEngine(max_tool_calls=QuickJSExecEngine.MAX_TOOL_CALLS + 1)


def test_maximum_tool_steps_stop_dispatch_before_the_next_gateway_call() -> None:
    engine = QuickJSExecEngine(max_tool_calls=2)
    bridge = RecordingBridge()
    result = _run(
        engine,
        "for (let i = 0; i < 5; i++) await tools.read_file({path: `src/${i}.py`}); return 'done';",
        bridge,
    )

    assert result.error_message is not None
    assert result.step_count == 2
    assert len(bridge.calls) == 2


def test_each_execute_uses_a_fresh_context() -> None:
    engine = QuickJSExecEngine()
    first = engine.execute(
        "globalThis.privateValue = 7; return privateValue;", RecordingBridge(), 2
    )
    second = engine.execute("return typeof privateValue;", RecordingBridge(), 2)

    assert json.loads(first.result) == 7
    assert json.loads(second.result) == "undefined"
