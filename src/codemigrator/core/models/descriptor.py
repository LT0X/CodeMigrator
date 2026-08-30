"""Declarative source/target toolchain contract models."""

from __future__ import annotations

from typing import Annotated

import semver
from pydantic import WithJsonSchema, field_serializer, field_validator

from .._base import CoreModel
from ..enums import CheckAction, ModuleBoundaryStrategy
from ..ids import CheckId, LanguageId, ProjectModuleId, RepoRelativePath, Sha256
from ..paths import _validate_repo_relative_path, normalize_repo_relative_paths

_SEMVER_PATTERN = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_DescriptorVersion = Annotated[
    semver.Version,
    WithJsonSchema({"type": "string", "pattern": _SEMVER_PATTERN}, mode="validation"),
    WithJsonSchema({"type": "string", "pattern": _SEMVER_PATTERN}, mode="serialization"),
]


class TreeSitterGrammarRef(CoreModel):
    grammar_id: str
    grammar_sha256: Sha256


class ManifestParserRef(CoreModel):
    manifest_kind: str
    parser_id: str


class CheckCommandTemplate(CoreModel):
    action: CheckAction
    program: str
    argv: list[str]
    timeout_secs: int


class SourceToolchain(CoreModel):
    language_id: LanguageId
    extensions: list[str]
    parser: TreeSitterGrammarRef
    manifest_parsers: list[ManifestParserRef]
    module_boundary_strategy: ModuleBoundaryStrategy
    runtime_image_digest: Sha256 | None = None


class TargetToolchain(CoreModel):
    language_id: LanguageId
    package_manager: str
    scaffold: list[CheckCommandTemplate]
    build: list[CheckCommandTemplate]
    test: list[CheckCommandTemplate]
    lint: list[CheckCommandTemplate]
    typecheck: list[CheckCommandTemplate]
    toolchain_image_digest: str
    build_excludes: list[RepoRelativePath]

    @field_validator("build_excludes", mode="before")
    @classmethod
    def build_excludes_are_safe(cls, value: object) -> list[str]:
        try:
            return normalize_repo_relative_paths(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


class ToolchainDescriptor(CoreModel):
    descriptor_version: _DescriptorVersion
    descriptor_sha256: Sha256
    source: SourceToolchain
    target: TargetToolchain

    @field_validator("descriptor_version", mode="before")
    @classmethod
    def parse_descriptor_version(cls, value: object) -> semver.Version:
        if isinstance(value, semver.Version):
            return value
        if isinstance(value, str):
            try:
                return semver.Version.parse(value)
            except ValueError as exc:
                raise ValueError("descriptor_version must be valid semver") from exc
        raise ValueError("descriptor_version must be a semver string")

    @field_serializer("descriptor_version")
    def serialize_descriptor_version(self, value: semver.Version) -> str:
        return str(value)


class RequiredCheck(CoreModel):
    id: CheckId
    action: CheckAction
    template_sha256: Sha256


class ContractArtifact(CoreModel):
    module_id: ProjectModuleId
    target_module_path: RepoRelativePath
    public_signatures: list[str]
    types_hash: Sha256

    @field_validator("target_module_path", mode="before")
    @classmethod
    def target_module_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)
