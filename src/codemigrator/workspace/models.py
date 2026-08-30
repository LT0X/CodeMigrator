"""Closed contracts owned by the candidate-workspace execution layer."""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from codemigrator.analysis import SourceAstQuery
from codemigrator.core import Phase, SessionKind, StableErrorCode
from codemigrator.core._base import CoreModel
from codemigrator.core.ids import RunId, Sha256, SliceId


class LineRange(CoreModel):
    model_config = ConfigDict(frozen=True)

    start_line: StrictInt = Field(ge=1)
    end_line: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def range_is_ordered(self) -> LineRange:
        if self.end_line < self.start_line:
            raise ValueError("line range end must not precede start")
        return self


class ReadFileCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["ReadFile"]
    path: str | None = None
    cas: str | None = None
    range: LineRange | None = None

    @field_validator("cas")
    @classmethod
    def cas_is_sha256_uri(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"cas://[0-9a-fA-F]{64}", value) is None:
            raise ValueError("cas must be a cas:// SHA-256 URI")
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def exactly_one_source(self) -> ReadFileCall:
        if (self.path is None) == (self.cas is None):
            raise ValueError("ReadFile requires exactly one of path or cas")
        return self


class WriteFileCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["WriteFile"]
    path: str
    content: str = Field(max_length=64 * 1024**2)

    @field_validator("content")
    @classmethod
    def content_is_within_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 64 * 1024**2:
            raise ValueError("content exceeds 64 MiB UTF-8 bytes")
        return value


class EditFileCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["EditFile"]
    path: str
    old_text: str = Field(min_length=1, max_length=1024**2)
    new_text: str = Field(max_length=1024**2)
    occur: StrictInt | None = Field(default=None, ge=1)

    @field_validator("old_text", "new_text")
    @classmethod
    def edit_text_is_within_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024**2:
            raise ValueError("edit text exceeds 1 MiB UTF-8 bytes")
        return value


class QuerySourceAstCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["QuerySourceAst"]
    request: SourceAstQuery


class ShellCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["Shell"]
    command: str = Field(min_length=1, max_length=1024**2)
    workdir: str | None = None
    timeout_secs: StrictInt | None = Field(default=None, gt=0, le=600)

    @field_validator("command")
    @classmethod
    def command_is_within_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024**2:
            raise ValueError("command exceeds 1 MiB UTF-8 bytes")
        return value


class ExecCall(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["Exec"]
    script: str = Field(min_length=1, max_length=1024**2)
    timeout_secs: StrictInt | None = Field(default=None, gt=0, le=60)

    @field_validator("script")
    @classmethod
    def script_is_within_byte_limit(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 1024**2:
            raise ValueError("script exceeds 1 MiB UTF-8 bytes")
        return value


ToolCall: TypeAlias = Annotated[
    ReadFileCall | WriteFileCall | EditFileCall | QuerySourceAstCall | ShellCall | ExecCall,
    Field(discriminator="tool"),
]


class ReadFileOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["ReadFile"]
    path: str
    body: str
    total_lines: int = Field(ge=0)
    truncated: bool = False


class WriteFileOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["WriteFile"]
    path: str
    bytes_written: int = Field(ge=0)
    disposition: Literal["CREATED", "OVERWRITTEN"]


class EditFileOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["EditFile"]
    path: str
    replaced_line: int = Field(ge=1)
    bytes_before: int = Field(ge=0)
    bytes_after: int = Field(ge=0)


class QuerySourceAstOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["QuerySourceAst"]
    result: Any


class ShellOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["Shell"]
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False


class ExecOutput(CoreModel):
    model_config = ConfigDict(frozen=True)

    tool: Literal["Exec"]
    result: str
    step_count: int = Field(ge=0)


class ToolError(CoreModel):
    model_config = ConfigDict(frozen=True)

    code: StableErrorCode
    message: str = Field(min_length=1)
    retryable_in_phase: bool
    facts: tuple[dict[str, Any], ...] = ()


ToolOutput: TypeAlias = (
    ReadFileOutput
    | WriteFileOutput
    | EditFileOutput
    | QuerySourceAstOutput
    | ShellOutput
    | ExecOutput
)
ToolResult: TypeAlias = ToolOutput | ToolError


class AuditEvent(CoreModel):
    """A redacted tool/checkpoint event suitable for run_events projection."""

    model_config = ConfigDict(frozen=True)

    point: Literal["tool.call.pre", "tool.call.post", "checkpoint.pre"]
    run_id: RunId | None
    slice_id: SliceId | None
    generation: int | None
    phase: Phase | None
    tool: str | None
    parameter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: str
    error_code: StableErrorCode | None = None
    duration_ms: int = Field(default=0, ge=0)
    path_kind: str | None = None
    path_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    script: str | None = None
    script_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    step_count: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    bytes_written: int | None = Field(default=None, ge=0)
    stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stdout_bytes: int | None = Field(default=None, ge=0)
    stderr_bytes: int | None = Field(default=None, ge=0)
    output_truncated: bool | None = None
    file_count: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    scope_check_passed: bool | None = None
    changed_paths: tuple[str, ...] = ()


class WorkspaceFileOperation(CoreModel):
    model_config = ConfigDict(frozen=True)

    run_id: RunId
    slice_id: SliceId
    generation: int = Field(ge=0, le=2)
    tool: Literal["WriteFile", "EditFile"]
    path: str
    bytes_written: int = Field(ge=0)
    disposition: Literal["CREATED", "OVERWRITTEN"]


class WorkspaceState(str, Enum):
    Provisioned = "PROVISIONED"
    Iterating = "ITERATING"
    Checkpointing = "CHECKPOINTING"
    Frozen = "FROZEN"
    Discarded = "DISCARDED"


class GatewayContext(CoreModel):
    model_config = ConfigDict(frozen=True)

    run_id: RunId
    phase_policy_sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    phase: Phase
    session_kind: SessionKind
    slice_id: SliceId | None = None
    generation: int | None = Field(default=None, ge=0, le=2)


class WorkspaceHandle(CoreModel):
    model_config = ConfigDict(frozen=True)

    run_id: RunId
    slice_id: SliceId
    generation: int = Field(ge=0, le=2)
    path: str
    state: WorkspaceState
    base_verified_oid: str


__all__ = [
    "AuditEvent",
    "EditFileCall",
    "EditFileOutput",
    "ExecCall",
    "ExecOutput",
    "GatewayContext",
    "LineRange",
    "QuerySourceAstCall",
    "QuerySourceAstOutput",
    "ReadFileCall",
    "ReadFileOutput",
    "ShellCall",
    "ShellOutput",
    "ToolCall",
    "ToolError",
    "ToolOutput",
    "ToolResult",
    "WorkspaceFileOperation",
    "WorkspaceHandle",
    "WorkspaceState",
    "WriteFileCall",
    "WriteFileOutput",
]
