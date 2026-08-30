"""Ports for execution owners and the safe Exec bridge."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ConfigDict, Field

from codemigrator.core._base import CoreModel

from .models import ShellCall, ToolResult

if TYPE_CHECKING:
    from .gateway import ToolGateway


class ActionProtocolError(ValueError):
    """A model action cannot be parsed without guessing its intent."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ActionProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite_constant(value: str) -> None:
    raise ActionProtocolError(f"non-finite JSON number is not allowed: {value}")


def _parse_object(payload: str, line_offset: int = 0) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except (json.JSONDecodeError, ActionProtocolError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            message = f"line {line_offset + exc.lineno}: invalid action JSON"
        else:
            message = str(exc)
        raise ActionProtocolError(message) from exc
    if not isinstance(value, dict):
        raise ActionProtocolError("action payload must be one JSON object")
    return value


def parse_action_stream(text: str) -> tuple[dict[str, Any], ...]:
    """Parse marked multi-actions, or one strict JSON object as a fallback."""

    if not isinstance(text, str) or not text.strip():
        raise ActionProtocolError("action output must not be empty")
    if "[cm:action]" not in text:
        return (_parse_object(text),)
    lines = text.splitlines()
    actions: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        if lines[index].strip() != "[cm:action]":
            raise ActionProtocolError(f"line {index + 1}: expected [cm:action]")
        start_line = index + 1
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != "[cm:/action]":
            body.append(lines[index])
            index += 1
        if index == len(lines):
            raise ActionProtocolError(f"line {start_line}: missing [cm:/action]")
        actions.append(_parse_object("\n".join(body), start_line))
        index += 1
    if not actions:
        raise ActionProtocolError("action output contains no action segment")
    return tuple(actions)


class CasStore(Protocol):
    def read(self, digest: str) -> bytes | None: ...


class InMemoryCasStore:
    def __init__(self, values: Mapping[str, bytes] | None = None) -> None:
        self._values = dict(values or {})

    def read(self, digest: str) -> bytes | None:
        value = self._values.get(digest)
        return None if value is None else bytes(value)

    def put(self, digest: str, value: bytes) -> None:
        self._values[digest] = bytes(value)


class QuerySourceAstPort(Protocol):
    def query(self, request: Mapping[str, Any]) -> Any: ...


class ShellExecution(CoreModel):
    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False


class ShellRunner(Protocol):
    def run(self, call: ShellCall, workspace_root: str) -> ShellExecution: ...


class ExecExecution(CoreModel):
    model_config = ConfigDict(frozen=True)

    result: str = ""
    step_count: int = Field(ge=0)
    error_line: int | None = Field(default=None, ge=1)
    error_message: str | None = None
    timed_out: bool = False


class ExecEngine(Protocol):
    def execute(self, script: str, bridge: ExecToolBridge, timeout_secs: int) -> ExecExecution: ...


class CallbackExecEngine:
    """Deterministic test adapter; production binds a QuickJS implementation in infra."""

    def __init__(self, callback: Callable[[str, ExecToolBridge], ExecExecution]) -> None:
        self._callback = callback

    def execute(self, script: str, bridge: ExecToolBridge, timeout_secs: int) -> ExecExecution:
        return self._callback(script, bridge)


class ExecToolBridge:
    """The only capability exposed to an embedded script."""

    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway

    def call(self, raw_call: Mapping[str, object]) -> ToolResult:
        return self._gateway.dispatch(raw_call)


__all__ = [
    "ActionProtocolError",
    "CallbackExecEngine",
    "CasStore",
    "ExecEngine",
    "ExecExecution",
    "ExecToolBridge",
    "InMemoryCasStore",
    "QuerySourceAstPort",
    "ShellExecution",
    "ShellRunner",
    "parse_action_stream",
]
