"""Closed public models for the Migration Spec v3 contract."""

from __future__ import annotations

import re
from typing import Annotated

import semver
from pydantic import Field, field_validator, model_validator

from .._base import CoreModel
from ..enums import CheckAction


def _normalize_sha256(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("SHA-256 digest must be a string")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdefABCDEF" for character in digest):
        raise ValueError("SHA-256 digest must contain 64 hexadecimal characters")
    return digest.lower()


_Digest = Annotated[str, Field(min_length=64, max_length=64)]


class DescriptorLock(CoreModel):
    """The exact resource versions a Spec is allowed to consume."""

    descriptor_version: str
    source_descriptor_sha256: _Digest
    target_descriptor_sha256: _Digest
    toolchain_image_digest: _Digest

    @field_validator("descriptor_version")
    @classmethod
    def descriptor_version_is_semver(cls, value: str) -> str:
        try:
            semver.Version.parse(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("descriptor_version must be valid semver") from exc
        return value

    @field_validator(
        "source_descriptor_sha256",
        "target_descriptor_sha256",
        "toolchain_image_digest",
        mode="before",
    )
    @classmethod
    def digest_is_normalized(cls, value: object) -> str:
        return _normalize_sha256(value)


class SpecScope(CoreModel):
    """The finite repository-relative path language used by Spec v3."""

    include: list[str]
    exclude: list[str] = Field(default_factory=list)

    @field_validator("include", "exclude")
    @classmethod
    def paths_are_strings(cls, value: list[str]) -> list[str]:
        if any(not isinstance(path, str) for path in value):
            raise ValueError("scope patterns must be strings")
        return value

    @field_validator("include")
    @classmethod
    def include_is_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("scope include must contain at least one pattern")
        return value

    @field_validator("include", "exclude")
    @classmethod
    def paths_are_finite_patterns(cls, value: list[str]) -> list[str]:
        from ..scope import validate_scope_pattern

        for path in value:
            validate_scope_pattern(path)
        return value

    def includes(self, path: str) -> bool:
        from ..scope import scope_includes_path

        return scope_includes_path(self, path)


class RequiredCheckSelection(CoreModel):
    """A reference to a target descriptor command template."""

    action: CheckAction
    template_sha256: _Digest

    @field_validator("template_sha256", mode="before")
    @classmethod
    def digest_is_normalized(cls, value: object) -> str:
        return _normalize_sha256(value)


class Decomposition(CoreModel):
    """Planner hints; they never carry commands or write scope."""

    module_granularity: str | None = None
    max_parallelism: int | None = None
    test_grouping: str | None = None

    @field_validator("module_granularity", "test_grouping")
    @classmethod
    def text_hints_are_non_empty(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("decomposition text hints must not be empty")
        return value

    @field_validator("max_parallelism")
    @classmethod
    def parallelism_is_positive(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("max_parallelism must be a positive integer")
        return value


class MigrationSpec(CoreModel):
    """The typed business document accepted by the four Spec gates."""

    schema_name: str = Field(alias="schema")
    version: int
    name: str
    description: str | None = None
    source_language_id: str
    target_language_id: str
    descriptor_lock: DescriptorLock
    scope: SpecScope
    required_checks: list[RequiredCheckSelection]
    decomposition: Decomposition | None = None

    @property
    def schema(self) -> str:  # type: ignore[override]
        """Expose the wire-level field without shadowing BaseModel.schema."""

        return self.schema_name

    @field_validator("schema_name")
    @classmethod
    def schema_name_is_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("schema must not be empty")
        return value

    @field_validator("name")
    @classmethod
    def name_has_valid_size(cls, value: str) -> str:
        size = len(value.encode("utf-8"))
        if not 1 <= size <= 128:
            raise ValueError("name must be between 1 and 128 UTF-8 bytes")
        return value

    @field_validator("description")
    @classmethod
    def description_has_valid_size(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 1024:
            raise ValueError("description must not exceed 1024 UTF-8 bytes")
        return value

    @field_validator("source_language_id", "target_language_id")
    @classmethod
    def language_id_is_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", value):
            raise ValueError("language id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def source_and_target_are_distinct(self) -> MigrationSpec:
        if self.source_language_id == self.target_language_id:
            raise ValueError("source and target languages must differ")
        from ..scope import excludes_are_contained

        if not excludes_are_contained(self.scope.include, self.scope.exclude):
            raise ValueError("scope excludes must be contained by scope includes")
        return self


__all__ = [
    "Decomposition",
    "DescriptorLock",
    "MigrationSpec",
    "RequiredCheckSelection",
    "SpecScope",
]
