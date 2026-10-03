"""Bounded, capability-limited JavaScript execution for the Exec tool."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

import quickjs  # type: ignore[import-untyped]

from .protocol import ExecExecution, ExecToolBridge

_TOOL_BOOTSTRAP = r"""
(() => {
  const maxToolCalls = __CM_MAX_TOOL_CALLS__;
  const queuedCalls = [];
  const pending = new Map();
  let nextId = 0;

  function invoke(tool, args) {
    if (args === null || typeof args !== "object" || Array.isArray(args)) {
      return Promise.reject(new TypeError("tool arguments must be an object"));
    }
    if (nextId >= maxToolCalls) {
      return Promise.reject(new Error("Exec tool-call limit exceeded"));
    }
    const id = nextId++;
    const payload = Object.assign({}, args, {tool});
    return new Promise((resolve, reject) => {
      pending.set(id, {resolve, reject});
      queuedCalls.push({id, call: payload});
    });
  }

  globalThis.tools = Object.freeze({
    read_file: (args) => invoke("ReadFile", args),
    write_file: (args) => invoke("WriteFile", args),
    edit_file: (args) => invoke("EditFile", args),
    query_source_ast: (request) => invoke("QuerySourceAst", {request}),
    shell: (args) => invoke("Shell", args)
  });

  // A single JSON protocol is used by the host to drain ordered calls and
  // resolve their promises. JavaScript never calls into Python directly.
  Object.defineProperty(globalThis, "__cm_bridge", {
    configurable: false,
    enumerable: false,
    writable: false,
    value: (raw) => {
      const message = JSON.parse(raw);
      if (message.op === "drain") return JSON.stringify(queuedCalls.splice(0));
      if (message.op !== "resolve" || !Array.isArray(message.responses)) {
        throw new TypeError("invalid Exec bridge message");
      }
      for (const response of message.responses) {
        const task = pending.get(response.id);
        if (!task) continue;
        pending.delete(response.id);
        if (response.ok) task.resolve(response.value);
        else task.reject(new Error(response.error));
      }
      return "null";
    }
  });
})();
globalThis.__cm_exec_state = {status: "pending", result: "null", error: null};
"""

_TOOL_SCRIPT_PREFIX = "globalThis.__cm_exec_task = (async function () {\n"
_TOOL_SCRIPT_SUFFIX = "\n})();"
_RESULT_HANDLER = r"""
function describeError(error) {
  const message = String(error);
  const stack = String(error && error.stack ? error.stack : "");
  return stack && stack !== message ? `${message}\n${stack}` : message;
}

globalThis.__cm_exec_task.then(
  (value) => {
    try {
      const encoded = JSON.stringify(value === undefined ? null : value);
      if (encoded === undefined) throw new TypeError("Exec result is not JSON serializable");
      globalThis.__cm_exec_state.result = encoded;
      globalThis.__cm_exec_state.status = "done";
    } catch (error) {
      globalThis.__cm_exec_state.error = describeError(error);
      globalThis.__cm_exec_state.status = "error";
    }
  },
  (error) => {
    globalThis.__cm_exec_state.error = describeError(error);
    globalThis.__cm_exec_state.status = "error";
  }
);
"""

_INPUT_LINE_RE = re.compile(r"<input>:(\d+)")
_TIMEOUT_MARKERS = ("interrupted", "execution timed out", "time limit")


class QuickJSExecEngine:
    """Execute one script in a fresh QuickJS context with a single gateway bridge.

    JavaScript has no filesystem, network, or process APIs. Its only host
    capability is a set of fixed L1-L3 tool functions, each of which calls the
    JavaScript tool functions queue JSON requests and return pending Promises.
    The host pumps the queue synchronously through ``ExecToolBridge`` and
    resolves those Promises in order. Calls inside ``Promise.all`` therefore
    reach the gateway one at a time in source evaluation order.

    QuickJS cannot preempt synchronous Python gateway calls. Shell calls are
    narrowed to the remaining Exec deadline; other gateway ports retain their
    own timeout contracts.
    """

    MAX_MEMORY_BYTES = 64 * 1024 * 1024
    MAX_STACK_BYTES = 1024 * 1024
    MAX_TIMEOUT_SECS = 60
    MAX_TOOL_CALLS = 200

    def __init__(
        self,
        *,
        max_memory_bytes: int = MAX_MEMORY_BYTES,
        max_stack_bytes: int = MAX_STACK_BYTES,
        max_timeout_secs: int = MAX_TIMEOUT_SECS,
        max_tool_calls: int = MAX_TOOL_CALLS,
    ) -> None:
        self.max_memory_bytes = self._bounded_positive(
            "max_memory_bytes", max_memory_bytes, self.MAX_MEMORY_BYTES
        )
        self.max_stack_bytes = self._bounded_positive(
            "max_stack_bytes", max_stack_bytes, self.MAX_STACK_BYTES
        )
        self.max_timeout_secs = self._bounded_positive(
            "max_timeout_secs", max_timeout_secs, self.MAX_TIMEOUT_SECS
        )
        self.max_tool_calls = self._bounded_positive(
            "max_tool_calls", max_tool_calls, self.MAX_TOOL_CALLS
        )

    @staticmethod
    def _bounded_positive(name: str, value: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
            raise ValueError(f"{name} must be an integer in [1, {maximum}]")
        return value

    def execute(self, script: str, bridge: ExecToolBridge, timeout_secs: int) -> ExecExecution:
        """Run JavaScript and return its JSON result or a self-correctable failure."""

        if isinstance(timeout_secs, bool) or not isinstance(timeout_secs, int) or timeout_secs < 1:
            return ExecExecution(
                error_message="timeout_secs must be a positive integer", step_count=0
            )

        effective_timeout = min(timeout_secs, self.max_timeout_secs)
        step_count = 0
        context: quickjs.Context | None = None
        started_at = time.monotonic()
        deadline = started_at + effective_timeout

        class _TimeLimitExpired(Exception):
            pass

        def eval_js(source: str) -> Any:
            assert context is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TimeLimitExpired
            context.set_time_limit(min(remaining, float(self.MAX_TIMEOUT_SECS)))
            return context.eval(source)

        def call_bridge(message: Mapping[str, Any]) -> str:
            # JSON is encoded twice: the inner string is the bridge protocol
            # payload; the outer JSON string is a safe JavaScript literal.
            payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
            js_literal = json.dumps(payload, ensure_ascii=False)
            result = eval_js(f"__cm_bridge({js_literal})")
            if not isinstance(result, str):
                raise RuntimeError("Exec bridge returned a non-string response")
            return result

        try:
            context = quickjs.Context()
            context.set_memory_limit(self.max_memory_bytes)
            context.set_max_stack_size(self.max_stack_bytes)
            eval_js(_TOOL_BOOTSTRAP.replace("__CM_MAX_TOOL_CALLS__", str(self.max_tool_calls)))
            eval_js(_TOOL_SCRIPT_PREFIX + script + _TOOL_SCRIPT_SUFFIX)
            eval_js(_RESULT_HANDLER)

            while True:
                queued = json.loads(call_bridge({"op": "drain"}))
                if queued:
                    responses: list[dict[str, Any]] = []
                    for queued_call in queued:
                        if step_count >= self.max_tool_calls:
                            responses.append(
                                {
                                    "id": queued_call["id"],
                                    "ok": False,
                                    "error": "Exec tool-call limit exceeded",
                                }
                            )
                            continue
                        step_count += 1
                        try:
                            raw_call = queued_call["call"]
                            if not isinstance(raw_call, Mapping):
                                raise ValueError("invalid tool call payload")
                            gateway_call: Mapping[str, Any] = raw_call
                            if raw_call.get("tool") == "Shell":
                                remaining = deadline - time.monotonic()
                                # Leave one second for process-group teardown,
                                # JS promise settlement, and the final result.
                                available_shell_secs = int(remaining) - 1
                                if available_shell_secs < 1:
                                    return ExecExecution(step_count=step_count - 1, timed_out=True)
                                requested_timeout = raw_call.get("timeout_secs")
                                if requested_timeout is None:
                                    requested_timeout = 600
                                if (
                                    isinstance(requested_timeout, int)
                                    and not isinstance(requested_timeout, bool)
                                    and 1 <= requested_timeout <= 600
                                ):
                                    shell_timeout = min(requested_timeout, available_shell_secs)
                                    if shell_timeout < 1:
                                        return ExecExecution(
                                            step_count=step_count - 1, timed_out=True
                                        )
                                    if shell_timeout != requested_timeout:
                                        bounded_call = dict(raw_call)
                                        bounded_call["timeout_secs"] = shell_timeout
                                        gateway_call = bounded_call
                            # Gateway-owned Python work is synchronous and cannot
                            # be interrupted by QuickJS's JavaScript time limit.
                            result = bridge.call(gateway_call)
                            value = (
                                result.model_dump(mode="json")
                                if hasattr(result, "model_dump")
                                else result
                            )
                            json.dumps(
                                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                            )
                        except Exception:
                            responses.append(
                                {
                                    "id": queued_call["id"],
                                    "ok": False,
                                    "error": "tool bridge call failed",
                                }
                            )
                        else:
                            responses.append({"id": queued_call["id"], "ok": True, "value": value})
                    call_bridge({"op": "resolve", "responses": responses})
                    continue

                state = json.loads(eval_js("JSON.stringify(__cm_exec_state)"))
                if state["status"] != "pending":
                    break
                assert context is not None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ExecExecution(step_count=step_count, timed_out=True)
                context.set_time_limit(min(remaining, float(self.MAX_TIMEOUT_SECS)))
                if not context.execute_pending_job():
                    # A script can await a Promise that will never settle. Keep
                    # the same wall-clock bound even when QuickJS has no job to run.
                    time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))

            if state["status"] == "error":
                message = str(state["error"] or "JavaScript execution failed")
                if self._is_timeout(message):
                    return ExecExecution(step_count=step_count, timed_out=True)
                return ExecExecution(
                    step_count=step_count,
                    error_line=self._source_line(message, script),
                    error_message=message[:4096],
                )

            return ExecExecution(step_count=step_count, result=str(state["result"]))
        except _TimeLimitExpired:
            return ExecExecution(step_count=step_count, timed_out=True)
        except quickjs.JSException as exc:
            message = str(exc)
            if self._is_timeout(message):
                return ExecExecution(step_count=step_count, timed_out=True)
            return ExecExecution(
                step_count=step_count,
                error_line=self._source_line(message, script),
                error_message=message[:4096],
            )
        except Exception as exc:
            # Keep engine failures inside the existing ExecExecution contract;
            # ToolGateway owns the stable public error mapping and digest facts.
            message = str(exc) or type(exc).__name__
            return ExecExecution(
                step_count=step_count,
                error_line=self._source_line(message, script),
                error_message=message[:4096],
            )
        finally:
            # A context is never cached: globals, closures, and pending jobs from
            # one invocation cannot leak into a later Exec session.
            context = None

    @staticmethod
    def _is_timeout(message: str) -> bool:
        lowered = message.lower()
        return any(marker in lowered for marker in _TIMEOUT_MARKERS)

    @staticmethod
    def _source_line(message: str, script: str) -> int | None:
        match = _INPUT_LINE_RE.search(message)
        if match is None:
            return None
        line = int(match.group(1)) - 1  # subtract the single wrapper prefix line
        return min(max(1, line), max(1, len(script.splitlines())))


__all__ = ["QuickJSExecEngine"]
