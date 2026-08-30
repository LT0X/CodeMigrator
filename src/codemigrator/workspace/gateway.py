"""ToolGateway: the single admission and execution boundary for model tools."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, cast

from pydantic import TypeAdapter, ValidationError

from codemigrator.core import (
    Phase,
    RepoRelativePath,
    ResourceDocument,
    SessionKind,
    StableErrorCode,
    WriteScope,
    WriteScopeOut,
    canonical_json_bytes,
    load_resource,
)

from .models import (
    AuditEvent,
    EditFileCall,
    EditFileOutput,
    ExecCall,
    ExecOutput,
    GatewayContext,
    QuerySourceAstCall,
    QuerySourceAstOutput,
    ReadFileCall,
    ReadFileOutput,
    ShellCall,
    ShellOutput,
    ToolCall,
    ToolError,
    ToolResult,
    WorkspaceFileOperation,
    WriteFileCall,
    WriteFileOutput,
)
from .paths import PathNotFound, PathSecurityError, SecureRoot, sha256_bytes
from .protocol import (
    CasStore,
    ExecEngine,
    ExecExecution,
    QuerySourceAstPort,
    ShellRunner,
)


@lru_cache(maxsize=1)
def _load_phase_policy() -> ResourceDocument:
    """Load one immutable policy snapshot for all gateways in this process."""

    return load_resource("core://phase-tool-policy/v2")


@dataclass(frozen=True)
class GatewayRoots:
    snapshot: SecureRoot
    workspace: SecureRoot
    contract_roots: tuple[SecureRoot, ...] = ()
    verified: SecureRoot | None = None
    cas: CasStore | None = None

    @property
    def open_count(self) -> int:
        return sum(
            root.open_count
            for root in (self.snapshot, self.workspace, *self.contract_roots)
            if root is not None
        ) + (self.verified.open_count if self.verified is not None else 0)


class GatewayError(RuntimeError):
    """Internal exception used to return one structured tool error."""

    def __init__(self, error: ToolError) -> None:
        super().__init__(error.message)
        self.error = error


class ToolGateway:
    """Apply schema, authorization, path, scope, and execution gates in order."""

    MAX_READ_BYTES = 64 * 1024**2
    MAX_RESULT_BYTES = 256 * 1024
    MAX_READ_CALLS = 2_000
    MAX_SHELL_TIMEOUT = 600
    MAX_EXEC_TIMEOUT = 60

    def __init__(
        self,
        *,
        context: GatewayContext,
        roots: GatewayRoots,
        write_scope: WriteScope | None = None,
        other_write_scopes: Sequence[WriteScope] = (),
        query_port: QuerySourceAstPort | None = None,
        shell_runner: ShellRunner | None = None,
        exec_engine: ExecEngine | None = None,
        audit_sink: Callable[[AuditEvent], None] | None = None,
        operation_sink: Callable[[WorkspaceFileOperation], None] | None = None,
        _policy_document: ResourceDocument | None = None,
    ) -> None:
        self.context = context
        self.roots = roots
        self.write_scope = write_scope
        self.other_write_scopes = tuple(other_write_scopes)
        self.query_port = query_port
        self.shell_runner = shell_runner
        self.exec_engine = exec_engine
        self.audit_sink = audit_sink
        self.operation_sink = operation_sink
        self._policy_document = _policy_document or _load_phase_policy()
        if self.context.phase_policy_sha256 != self._policy_document.sha256:
            raise ValueError("phase policy digest does not match the frozen run context")
        self._policy = {
            phase: frozenset(tools) for phase, tools in self._policy_document.payload.items()
        }
        self._adapter: TypeAdapter[ToolCall] = TypeAdapter(ToolCall)
        self._read_calls = 0
        self._shell_calls = 0
        self._write_bytes = 0
        self.operations: list[WorkspaceFileOperation] = []
        self._policy_load_count = 0 if _policy_document is not None else 1

    @property
    def policy_sha256(self) -> str:
        return self._policy_document.sha256

    @property
    def policy_load_count(self) -> int:
        return self._policy_load_count

    def with_other_write_scopes(self, scopes: Sequence[Mapping[str, object]]) -> ToolGateway:
        parsed: list[WriteScope] = []
        for scope in scopes:
            write_paths = cast(Sequence[str], scope.get("write_paths", []))
            create_roots = cast(Sequence[str], scope.get("create_roots", []))
            parsed.append(
                WriteScope(
                    out=WriteScopeOut(
                        write_paths=list(cast(Sequence[RepoRelativePath], write_paths)),
                        create_roots=list(cast(Sequence[RepoRelativePath], create_roots)),
                    )
                )
            )
        return ToolGateway(
            context=self.context,
            roots=self.roots,
            write_scope=self.write_scope,
            other_write_scopes=parsed,
            query_port=self.query_port,
            shell_runner=self.shell_runner,
            exec_engine=self.exec_engine,
            audit_sink=self.audit_sink,
            operation_sink=self.operation_sink,
            _policy_document=self._policy_document,
        )

    def dispatch(self, raw_call: object) -> ToolResult:
        tool_name = raw_call.get("tool") if isinstance(raw_call, Mapping) else None
        parameter_sha256 = self._parameter_sha256(raw_call)
        started = time.monotonic()
        parsed: ToolCall | None = None
        if not isinstance(tool_name, str) or tool_name not in {
            "ReadFile",
            "WriteFile",
            "EditFile",
            "QuerySourceAst",
            "Shell",
            "Exec",
        }:
            result: ToolResult = self._error(
                StableErrorCode.TOOL_NOT_FOUND,
                "tool is not in the closed six-tool registry",
                retryable=False,
            )
            self._emit_pre(tool_name, parameter_sha256)
            self._emit_post(tool_name, parameter_sha256, result, started, call=None)
            return result
        self._emit_pre(tool_name, parameter_sha256)
        try:
            parsed = self._adapter.validate_python(raw_call)
        except ValidationError:
            result = self._error(
                StableErrorCode.TOOL_SCHEMA_INVALID,
                "tool input does not satisfy the closed schema",
            )
            self._emit_post(tool_name, parameter_sha256, result, started, call=None)
            return result

        if not self._is_allowed(tool_name):
            result = self._error(
                StableErrorCode.TOOL_PHASE_DENIED,
                "the current phase or session cannot use this tool",
                retryable=False,
            )
            self._emit_post(tool_name, parameter_sha256, result, started, call=parsed)
            return result

        try:
            result = self._execute(parsed)
        except GatewayError as exc:
            result = exc.error
        except (OSError, UnicodeError, ValueError) as exc:
            result = self._error(
                StableErrorCode.CHECKPOINT_WRITE_FAILED,
                "tool execution failed without exposing host details",
                facts=({"exception": type(exc).__name__},),
            )
        self._emit_post(tool_name, parameter_sha256, result, started, call=parsed)
        return result

    def _execute(self, call: ToolCall) -> ToolResult:
        if isinstance(call, ReadFileCall):
            return self._read(call)
        if isinstance(call, WriteFileCall):
            return self._write(call)
        if isinstance(call, EditFileCall):
            return self._edit(call)
        if isinstance(call, QuerySourceAstCall):
            return self._query(call)
        if isinstance(call, ShellCall):
            return self._shell(call)
        return self._exec(call)

    def _is_allowed(self, tool_name: str) -> bool:
        phase = self.context.phase.value
        allowed = self._policy.get(phase, frozenset())
        if tool_name not in allowed:
            return False
        if self.context.session_kind is SessionKind.ExploreCoordinator:
            return tool_name in {"ReadFile", "QuerySourceAst", "Exec"}
        if self.context.session_kind is SessionKind.ExecuteSupervisor:
            return tool_name in {"ReadFile", "QuerySourceAst"}
        return True

    def _read(self, call: ReadFileCall) -> ToolResult:
        if self._read_calls >= self.MAX_READ_CALLS:
            return self._error(
                StableErrorCode.READ_LIMIT_EXCEEDED,
                "read call quota has been exhausted",
                facts=({"limit": self.MAX_READ_CALLS},),
            )
        self._read_calls += 1
        if call.cas is not None:
            if self.roots.cas is None:
                return self._error(
                    StableErrorCode.READ_OUT_OF_SCOPE,
                    "CAS is not bound to this run",
                )
            data = self.roots.cas.read(call.cas)
            if data is None:
                return self._error(
                    StableErrorCode.READ_OUT_OF_SCOPE,
                    "CAS object is not owned by this run",
                )
            path = call.cas
        else:
            assert call.path is not None
            data = self._read_from_bound_roots(call.path)
            if data is None:
                return self._error(
                    StableErrorCode.READ_OUT_OF_SCOPE,
                    "path is outside the current read roots",
                    facts=({"root_count": len(self._read_roots())},),
                )
            path = call.path
        if len(data) > self.MAX_READ_BYTES:
            return self._error(
                StableErrorCode.READ_LIMIT_EXCEEDED,
                "file exceeds the 64 MiB read limit",
                facts=({"bytes": len(data), "limit": self.MAX_READ_BYTES},),
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(StableErrorCode.READ_OUT_OF_SCOPE, "file is not UTF-8 text")
        return self._format_read(path, text, call)

    def _read_from_bound_roots(self, path: str) -> bytes | None:
        for root in self._read_roots():
            try:
                return root.read_bytes(path, max_bytes=self.MAX_READ_BYTES)
            except PathSecurityError as exc:
                raise GatewayError(
                    self._error(
                        StableErrorCode.PATH_DENIED,
                        "path failed the no-follow root safety gate",
                        facts=({"root": root.name},),
                    )
                ) from exc
            except PathNotFound:
                continue
            except ValueError as exc:
                raise GatewayError(
                    self._error(
                        StableErrorCode.READ_LIMIT_EXCEEDED,
                        "file exceeds the 64 MiB read limit",
                    )
                ) from exc
        return None

    def _read_roots(self) -> tuple[SecureRoot, ...]:
        if self.context.phase is Phase.Plan:
            return (self.roots.snapshot,)
        roots = (self.roots.workspace, *self.roots.contract_roots, self.roots.snapshot)
        if (
            self.context.session_kind is SessionKind.RepairSession
            and self.roots.verified is not None
        ):
            roots += (self.roots.verified,)
        return roots

    def _format_read(self, path: str, text: str, call: ReadFileCall) -> ReadFileOutput:
        lines = text.splitlines()
        total_lines = len(lines)
        start = call.range.start_line if call.range is not None else 1
        end = call.range.end_line if call.range is not None else total_lines
        selected = lines[max(0, start - 1) : end]
        width = len(str(max(end, total_lines, 1)))
        output_lines: list[str] = []
        truncated = False
        size = 0
        for offset, line in enumerate(selected, start=max(1, start)):
            rendered = f"{offset:>{width}}\t{line}"
            encoded_size = len(rendered.encode("utf-8")) + (1 if output_lines else 0)
            if size + encoded_size > self.MAX_RESULT_BYTES:
                truncated = True
                break
            output_lines.append(rendered)
            size += encoded_size
        return ReadFileOutput(
            tool="ReadFile",
            path=path,
            body="\n".join(output_lines),
            total_lines=total_lines,
            truncated=truncated,
        )

    def _write(self, call: WriteFileCall) -> ToolResult:
        workspace = self.roots.workspace
        try:
            workspace.validate(call.path)
            existed = workspace.exists(call.path)
        except PathSecurityError as exc:
            raise GatewayError(
                self._error(StableErrorCode.PATH_DENIED, "path failed the safety gate")
            ) from exc
        except PathNotFound:
            existed = False
        if self.write_scope is None or not self._write_allowed(call.path, existed):
            return self._error(
                StableErrorCode.WRITE_SCOPE_VIOLATION,
                "path is outside the frozen write scope",
                facts=({"scope": "frozen"},),
            )
        data = call.content.encode("utf-8")
        if self._write_bytes + len(data) > self.MAX_READ_BYTES:
            return self._error(
                StableErrorCode.WRITE_LIMIT_EXCEEDED,
                "write quota has been exhausted",
            )
        workspace.write_atomic(call.path, data)
        self._write_bytes += len(data)
        output = WriteFileOutput(
            tool="WriteFile",
            path=call.path,
            bytes_written=len(data),
            disposition="OVERWRITTEN" if existed else "CREATED",
        )
        self._record_operation(call.tool, call.path, len(data), output.disposition)
        return output

    def _edit(self, call: EditFileCall) -> ToolResult:
        workspace = self.roots.workspace
        try:
            workspace.validate(call.path)
            if not workspace.exists(call.path):
                return self._error(
                    StableErrorCode.EDIT_TARGET_NOT_FOUND,
                    "edit target does not exist",
                )
            data = workspace.read_bytes(call.path, max_bytes=self.MAX_READ_BYTES)
        except PathSecurityError as exc:
            raise GatewayError(
                self._error(StableErrorCode.PATH_DENIED, "path failed the safety gate")
            ) from exc
        except PathNotFound:
            return self._error(StableErrorCode.EDIT_TARGET_NOT_FOUND, "edit target does not exist")
        if self.write_scope is None or not self._write_allowed(call.path, True):
            return self._error(
                StableErrorCode.WRITE_SCOPE_VIOLATION,
                "path is outside the frozen write scope",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return self._error(
                StableErrorCode.EDIT_TARGET_NOT_FOUND, "edit target is not UTF-8 text"
            )
        positions: list[int] = []
        cursor = 0
        while True:
            cursor = text.find(call.old_text, cursor)
            if cursor < 0:
                break
            positions.append(cursor)
            cursor += len(call.old_text)
        if not positions:
            return self._error(
                StableErrorCode.EDIT_TARGET_NOT_FOUND,
                "edit target was not found",
                facts=({"line_count": len(text.splitlines())},),
            )
        if call.occur is None and len(positions) > 1:
            return self._error(
                StableErrorCode.EDIT_AMBIGUOUS,
                "edit target matched more than once",
                facts=tuple({"line": text.count("\n", 0, position) + 1} for position in positions),
            )
        index = (call.occur - 1) if call.occur is not None else 0
        if index >= len(positions):
            return self._error(
                StableErrorCode.EDIT_TARGET_NOT_FOUND,
                "requested occurrence was not found",
                facts=({"match_count": len(positions)},),
            )
        position = positions[index]
        updated = text[:position] + call.new_text + text[position + len(call.old_text) :]
        updated_bytes = updated.encode("utf-8")
        if self._write_bytes + len(updated_bytes) > self.MAX_READ_BYTES:
            return self._error(
                StableErrorCode.WRITE_LIMIT_EXCEEDED, "write quota has been exhausted"
            )
        workspace.write_atomic(call.path, updated_bytes)
        self._write_bytes += len(updated_bytes)
        output = EditFileOutput(
            tool="EditFile",
            path=call.path,
            replaced_line=text.count("\n", 0, position) + 1,
            bytes_before=len(data),
            bytes_after=len(updated_bytes),
        )
        self._record_operation(call.tool, call.path, len(updated_bytes), "OVERWRITTEN")
        return output

    def _write_allowed(self, path: str, existed: bool) -> bool:
        assert self.write_scope is not None
        write_paths = {str(item) for item in self.write_scope.out.write_paths}
        if path in write_paths:
            return True
        if existed:
            return False
        roots = {str(item).rstrip("/") for item in self.write_scope.out.create_roots}
        if not any(path == root or path.startswith(root + "/") for root in roots):
            return False
        return not any(
            self._inside_scope(path, other.out.write_paths, other.out.create_roots)
            for other in self.other_write_scopes
        )

    @staticmethod
    def _inside_scope(
        path: str, write_paths: Sequence[object], create_roots: Sequence[object]
    ) -> bool:
        return path in {str(item) for item in write_paths} or any(
            path == str(root).rstrip("/") or path.startswith(str(root).rstrip("/") + "/")
            for root in create_roots
        )

    def _query(self, call: QuerySourceAstCall) -> ToolResult:
        if self.query_port is None:
            return self._error(
                StableErrorCode.DEPENDENCY_UNAVAILABLE, "PSF query port is not bound"
            )
        try:
            return QuerySourceAstOutput(
                tool="QuerySourceAst", result=self.query_port.query(call.request)
            )
        except ValueError as exc:
            code = self._query_error_code(str(exc))
            return self._error(
                code,
                "source navigation rejected the request",
            )
        except Exception as exc:  # noqa: BLE001 - port errors become a typed tool result
            return self._error(
                StableErrorCode.ANALYSIS_INFRA_ERROR,
                "source navigation failed without exposing host details",
                facts=({"exception": type(exc).__name__},),
            )

    @staticmethod
    def _query_error_code(message: str) -> StableErrorCode:
        for code in (
            StableErrorCode.PATH_OUTSIDE_SNAPSHOT,
            StableErrorCode.QUERY_TIMEOUT,
            StableErrorCode.TEXT_FALLBACK_UNSUPPORTED,
        ):
            if message == code.value or message.startswith(f"{code.value}:"):
                return code
        return StableErrorCode.ANALYSIS_INFRA_ERROR

    def _shell(self, call: ShellCall) -> ToolResult:
        if self.shell_runner is None:
            return self._error(
                StableErrorCode.DEPENDENCY_UNAVAILABLE, "sandbox Shell runner is not bound"
            )
        if self._shell_calls >= self.MAX_READ_CALLS:
            return self._error(
                StableErrorCode.SHELL_LIMIT_EXCEEDED, "Shell call quota has been exhausted"
            )
        self._shell_calls += 1
        if call.workdir is not None:
            try:
                self.roots.workspace.validate(call.workdir)
            except PathSecurityError as exc:
                raise GatewayError(
                    self._error(StableErrorCode.PATH_DENIED, "workdir failed the safety gate")
                ) from exc
        effective_call = (
            call
            if call.timeout_secs is not None
            else call.model_copy(update={"timeout_secs": self.MAX_SHELL_TIMEOUT})
        )
        execution = self.shell_runner.run(
            effective_call, str(self.roots.workspace.absolute_path(call.workdir))
        )
        if execution.timed_out:
            return self._error(
                StableErrorCode.SHELL_TIMEOUT,
                "Shell command exceeded its bounded wait",
                facts=({"timeout_secs": effective_call.timeout_secs},),
            )
        return ShellOutput(
            tool="Shell",
            exit_code=execution.exit_code,
            stdout=execution.stdout,
            stderr=execution.stderr,
            truncated=execution.truncated,
        )

    def _exec(self, call: ExecCall) -> ToolResult:
        if self.exec_engine is None:
            return self._error(
                StableErrorCode.EXEC_SCRIPT_ERROR,
                "embedded Exec engine is not bound",
                facts=({"engine": "unavailable"},),
            )
        from .protocol import ExecToolBridge

        execution: ExecExecution = self.exec_engine.execute(
            call.script, ExecToolBridge(self), call.timeout_secs or self.MAX_EXEC_TIMEOUT
        )
        if execution.timed_out:
            return self._error(
                StableErrorCode.EXEC_TIMEOUT,
                "Exec script exceeded its bounded wait",
                facts=({"timeout_secs": call.timeout_secs or self.MAX_EXEC_TIMEOUT},),
            )
        if execution.error_message is not None:
            fact: dict[str, Any] = {
                "message_sha256": sha256_bytes(execution.error_message.encode("utf-8"))
            }
            if execution.error_line is not None:
                fact["line"] = execution.error_line
            return self._error(
                StableErrorCode.EXEC_SCRIPT_ERROR, "Exec script failed", facts=(fact,)
            )
        return ExecOutput(tool="Exec", result=execution.result, step_count=execution.step_count)

    def _record_operation(self, tool: str, path: str, bytes_written: int, disposition: str) -> None:
        if self.context.slice_id is None or self.context.generation is None:
            return
        operation = WorkspaceFileOperation(
            run_id=self.context.run_id,
            slice_id=self.context.slice_id,
            generation=self.context.generation,
            tool=tool,  # type: ignore[arg-type]
            path=path,
            bytes_written=bytes_written,
            disposition=disposition,  # type: ignore[arg-type]
        )
        self.operations.append(operation)
        if self.operation_sink is not None:
            self.operation_sink(operation)

    def _emit_pre(self, tool: str | None, parameter_sha256: str) -> None:
        self._emit(
            AuditEvent(
                point="tool.call.pre",
                run_id=self.context.run_id,
                slice_id=self.context.slice_id,
                generation=self.context.generation,
                phase=self.context.phase,
                tool=tool,
                parameter_sha256=parameter_sha256,
                outcome="RECEIVED",
                path_kind="repo-relative"
                if tool in {"ReadFile", "WriteFile", "EditFile"}
                else None,
            )
        )

    def _emit_post(
        self,
        tool: str | None,
        parameter_sha256: str,
        result: ToolResult,
        started: float,
        *,
        call: ToolCall | None,
    ) -> None:
        error_code = result.code if isinstance(result, ToolError) else None
        path = getattr(call, "path", None)
        command = getattr(call, "command", None)
        script = getattr(call, "script", None)
        if isinstance(result, WriteFileOutput):
            bytes_written = result.bytes_written
        elif isinstance(result, EditFileOutput):
            bytes_written = result.bytes_after
        else:
            bytes_written = None
        exit_code = result.exit_code if isinstance(result, ShellOutput) else None
        step_count = result.step_count if isinstance(result, ExecOutput) else None
        stdout = result.stdout if isinstance(result, ShellOutput) else None
        stderr = result.stderr if isinstance(result, ShellOutput) else None
        self._emit(
            AuditEvent(
                point="tool.call.post",
                run_id=self.context.run_id,
                slice_id=self.context.slice_id,
                generation=self.context.generation,
                phase=self.context.phase,
                tool=tool,
                parameter_sha256=parameter_sha256,
                outcome="REJECTED" if error_code is not None else "SUCCEEDED",
                error_code=error_code,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                path_sha256=(sha256_bytes(path.encode("utf-8")) if isinstance(path, str) else None),
                command_sha256=(
                    sha256_bytes(command.encode("utf-8")) if isinstance(command, str) else None
                ),
                script=script if isinstance(script, str) else None,
                script_sha256=(
                    sha256_bytes(script.encode("utf-8")) if isinstance(script, str) else None
                ),
                step_count=step_count,
                exit_code=exit_code,
                bytes_written=bytes_written,
                stdout_sha256=(
                    sha256_bytes(stdout.encode("utf-8")) if isinstance(stdout, str) else None
                ),
                stderr_sha256=(
                    sha256_bytes(stderr.encode("utf-8")) if isinstance(stderr, str) else None
                ),
                stdout_bytes=(len(stdout.encode("utf-8")) if isinstance(stdout, str) else None),
                stderr_bytes=(len(stderr.encode("utf-8")) if isinstance(stderr, str) else None),
                output_truncated=(result.truncated if isinstance(result, ShellOutput) else None),
            )
        )

    def _emit(self, event: AuditEvent) -> None:
        if self.audit_sink is not None:
            self.audit_sink(event)

    @staticmethod
    def _error(
        code: StableErrorCode,
        message: str,
        *,
        retryable: bool = True,
        facts: tuple[dict[str, Any], ...] = (),
    ) -> ToolError:
        return ToolError(code=code, message=message, retryable_in_phase=retryable, facts=facts)

    @staticmethod
    def _parameter_sha256(raw_call: object) -> str:
        if isinstance(raw_call, Mapping):
            redacted: dict[str, object] = {}
            for key, value in raw_call.items():
                if key in {"content", "old_text", "new_text"} and isinstance(value, str):
                    redacted[key] = {
                        "bytes": len(value.encode("utf-8")),
                        "sha256": sha256_bytes(value.encode("utf-8")),
                    }
                elif key in {"command", "script"} and isinstance(value, str):
                    redacted[key] = {
                        "bytes": len(value.encode("utf-8")),
                        "sha256": sha256_bytes(value.encode("utf-8")),
                    }
                elif key in {"path", "workdir", "cas"} and isinstance(value, str):
                    redacted[key] = {
                        "kind": "path-or-cas",
                        "sha256": sha256_bytes(value.encode("utf-8")),
                    }
                else:
                    redacted[key] = value
            try:
                return hashlib.sha256(canonical_json_bytes(redacted)).hexdigest()
            except (TypeError, ValueError):
                pass
        return sha256_bytes(repr(raw_call).encode("utf-8"))


__all__ = ["GatewayError", "GatewayRoots", "ToolGateway"]
