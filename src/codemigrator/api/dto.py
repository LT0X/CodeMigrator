"""Closed HTTP projection models owned by the API boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from codemigrator.core import (
    CheckAction,
    DeliveryChannelStatus,
    RunStatus,
    SliceAttemptStatus,
    SliceKind,
)


class ApiModel(BaseModel):
    """Reject undocumented fields at every external API boundary."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())


class DescriptorLockView(ApiModel):
    descriptor_version: str
    source_descriptor_sha256: str
    target_descriptor_sha256: str
    toolchain_image_digest: str


class RequiredCheckView(ApiModel):
    action: CheckAction
    template_sha256: str


class SpecView(ApiModel):
    spec_id: UUID
    canonical_sha256: str
    source_language_id: str
    target_language_id: str
    descriptor_lock: DescriptorLockView
    required_checks: list[RequiredCheckView]


class SliceView(ApiModel):
    slice_id: UUID
    kind: SliceKind
    status: SliceAttemptStatus
    generation: int
    write_scope: dict[str, list[str]]
    integration_rank: int


class MigrationView(ApiModel):
    run_id: UUID
    status: RunStatus
    version: int
    verification_outcome: object | None = None
    report_delivery_status: DeliveryChannelStatus = DeliveryChannelStatus.Pending
    code_delivery_status: DeliveryChannelStatus = DeliveryChannelStatus.Pending


class WorkspaceView(ApiModel):
    run_id: UUID
    slices: list[SliceView] = Field(default_factory=list)
    integration_queue: list[dict[str, object]] = Field(default_factory=list)
    latest_sequence: int = 0


class ReportView(ApiModel):
    run_id: UUID
    status: str
    report_ref: str | None = None


class EvidenceView(ApiModel):
    run_id: UUID
    receipt_id: UUID
    status: str
    artifact_refs: list[str] = Field(default_factory=list)


class DescriptorView(ApiModel):
    source_language_id: str
    target_language_id: str
    descriptor_version: str
    source_descriptor_sha256: str
    target_descriptor_sha256: str
    toolchain_image_digest: str
    checks: list[RequiredCheckView] = Field(default_factory=list)


class HealthView(ApiModel):
    app: str
    postgres: str
    sandbox: str
    optional_profiles: dict[str, str] = Field(default_factory=dict)


class ProjectView(ApiModel):
    project_id: UUID
    snapshot_id: UUID | None = None
    status: str = "READY"


class SessionView(ApiModel):
    session_id: UUID
    status: str
    revision: int = 0


class ChangesView(ApiModel):
    run_id: UUID
    changes: list[dict[str, object]] = Field(default_factory=list)


class OutputView(ApiModel):
    run_id: UUID
    status: str
    files: list[str] = Field(default_factory=list)


class SkillView(ApiModel):
    skill_id: str
    version: str
    summary: str


class SessionCreateRequest(ApiModel):
    kind: str
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def kind_is_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session kind must not be empty")
        return value


class SessionMessageRequest(ApiModel):
    message: str
    revision: int | None = None

    @field_validator("message")
    @classmethod
    def message_is_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be empty")
        return value


class SessionAnswerRequest(ApiModel):
    question_id: UUID
    answer: object
    revision: int


class SessionConfirmRequest(ApiModel):
    revision: int


class CorrectionConfirmRequest(ApiModel):
    preview_hash: str

    @field_validator("preview_hash")
    @classmethod
    def preview_hash_is_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("preview_hash must not be empty")
        return value


class MigrationEvent(ApiModel):
    schema_name: Literal["migration.event"] = Field("migration.event", alias="schema")
    version: Literal[1] = 1
    type: str
    data: dict[str, object]
    sequence: int
    timestamp_utc: datetime

    @field_validator("type")
    @classmethod
    def event_type_is_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event type must not be empty")
        return value

    @field_validator("sequence")
    @classmethod
    def sequence_is_positive(cls, value: int) -> int:
        if type(value) is not int or value < 1:
            raise ValueError("event sequence must be a positive integer")
        return value

    @field_validator("timestamp_utc")
    @classmethod
    def timestamp_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def event_data_is_redacted(self) -> MigrationEvent:
        _assert_redacted(self.data)
        return self

    @property
    def sse_id(self) -> str:
        return str(self.sequence)

    @property
    def schema(self) -> str:  # type: ignore[override]
        return self.schema_name

    @classmethod
    def from_record(
        cls,
        record: object,
        *,
        data: dict[str, object] | None = None,
    ) -> MigrationEvent:
        from .deps import EventRecord

        if not isinstance(record, EventRecord):
            raise TypeError("record must use EventRecord")
        return cls(
            schema="migration.event",
            type=record.event_type,
            data=record.data if data is None else data,
            sequence=record.sequence,
            timestamp_utc=record.timestamp_utc,
        )


_REDACTION_KEYS = frozenset(
    {"authorization", "cookie", "password", "secret", "token", "credential", "source"}
)


def _assert_redacted(value: object) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in _REDACTION_KEYS:
                raise ValueError("event data must be redacted")
            _assert_redacted(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_redacted(nested)


__all__ = [
    "ApiModel",
    "ChangesView",
    "CorrectionConfirmRequest",
    "DescriptorLockView",
    "DescriptorView",
    "EvidenceView",
    "HealthView",
    "MigrationEvent",
    "MigrationView",
    "OutputView",
    "ProjectView",
    "ReportView",
    "RequiredCheckView",
    "SessionAnswerRequest",
    "SessionConfirmRequest",
    "SessionCreateRequest",
    "SessionMessageRequest",
    "SessionView",
    "SkillView",
    "SliceView",
    "SpecView",
    "WorkspaceView",
]
