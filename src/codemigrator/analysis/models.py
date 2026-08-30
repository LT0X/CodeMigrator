"""Closed, deterministic source-analysis facts and query contracts."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from codemigrator.core import ArtifactKind, ProjectModuleId, RepoRelativePath, StableErrorCode
from codemigrator.core._base import CoreModel
from codemigrator.core.paths import _validate_repo_relative_path, canonical_json_bytes


class _FrozenModel(CoreModel):
    model_config = ConfigDict(frozen=True)


class ModuleRole(str, Enum):
    Source = "SOURCE"
    Test = "TEST"


class ModuleBoundary(str, Enum):
    Manifest = "MANIFEST"
    Directory = "DIRECTORY"
    File = "FILE"


class AnalysisCapability(str, Enum):
    Full = "FULL"
    TextFallback = "TEXT_FALLBACK"


class SymbolKind(str, Enum):
    Function = "FUNCTION"
    Class = "CLASS"
    Type = "TYPE"
    Interface = "INTERFACE"
    Constant = "CONSTANT"


class EdgeConfidence(str, Enum):
    Static = "STATIC"
    Unknown = "UNKNOWN"


class UnknownReason(str, Enum):
    DynamicImport = "DYNAMIC_IMPORT"
    Reflection = "REFLECTION"
    UnresolvedPath = "UNRESOLVED_PATH"


class CoverageDerivation(str, Enum):
    ImportGraph = "IMPORT_GRAPH"
    DirectoryConvention = "DIRECTORY_CONVENTION"
    Uncovered = "UNCOVERED"


class ModuleCoverage(str, Enum):
    Covered = "COVERED"
    EmptyTestSuite = "EMPTY_TEST_SUITE"
    Undetermined = "UNDETERMINED"


class SourcePosition(_FrozenModel):
    line: Annotated[StrictInt, Field(ge=1)]
    column: Annotated[StrictInt, Field(ge=0)]


class SourceRange(_FrozenModel):
    file_path: RepoRelativePath
    start: SourcePosition
    end: SourcePosition

    @field_validator("file_path", mode="before")
    @classmethod
    def file_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)

    @model_validator(mode="after")
    def end_is_not_before_start(self) -> SourceRange:
        if (self.end.line, self.end.column) < (self.start.line, self.start.column):
            raise ValueError("source range end must not precede start")
        return self


class ModuleTarget(_FrozenModel):
    kind: Literal["MODULE"] = "MODULE"
    module_id: ProjectModuleId


class ExternalTarget(_FrozenModel):
    kind: Literal["EXTERNAL"] = "EXTERNAL"
    package: str = Field(min_length=1)


ImportTarget = Annotated[ModuleTarget | ExternalTarget, Field(discriminator="kind")]


class ExportSummary(_FrozenModel):
    symbol: str = Field(min_length=1)
    kind: SymbolKind
    signature_text: str = Field(max_length=4096)


class ModuleFact(_FrozenModel):
    module_id: ProjectModuleId
    file_paths: list[RepoRelativePath]
    role: ModuleRole
    boundary: ModuleBoundary
    exported_symbols: list[ExportSummary]
    capability: AnalysisCapability
    degraded_files: list[RepoRelativePath]


class ImportEdge(_FrozenModel):
    from_module: ProjectModuleId
    to: ImportTarget | None
    confidence: EdgeConfidence
    reason: UnknownReason | None = None
    evidence: SourceRange
    imported_symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def confidence_matches_resolution(self) -> ImportEdge:
        if self.confidence is EdgeConfidence.Static and self.to is None:
            raise ValueError("static import edges require a target")
        if self.confidence is EdgeConfidence.Unknown and self.reason is None:
            raise ValueError("unknown import edges require a reason")
        return self


class CoverageEntry(_FrozenModel):
    test_file: RepoRelativePath
    tested_modules: list[ProjectModuleId]
    derivation: CoverageDerivation


class ModuleCoverageStatus(_FrozenModel):
    module: ProjectModuleId
    status: ModuleCoverage


class TestConservationBaseline(_FrozenModel):
    module: ProjectModuleId
    source_tests: Annotated[StrictInt, Field(ge=0)]
    source_assertions: Annotated[StrictInt, Field(ge=0)]
    source_loc: Annotated[StrictInt, Field(ge=0)]


class DependencyEntry(_FrozenModel):
    name: str = Field(min_length=1)
    version: str = ""


class ScriptEntry(_FrozenModel):
    name: str = Field(min_length=1)
    command_summary: str = Field(min_length=1, max_length=4096)


class ManifestSummary(_FrozenModel):
    manifest_path: RepoRelativePath
    manifest_kind: str = Field(min_length=1)
    dependencies: list[DependencyEntry]
    scripts: list[ScriptEntry]
    entry_points: list[str]

    @field_validator("manifest_path", mode="before")
    @classmethod
    def manifest_path_is_safe(cls, value: object) -> str:
        return _validate_repo_relative_path(value)


class ArtifactFact(_FrozenModel):
    path: RepoRelativePath
    artifact_kind: ArtifactKind
    source_path: RepoRelativePath | None = None

    @field_validator("path", "source_path", mode="before")
    @classmethod
    def artifact_paths_are_safe(cls, value: object) -> object:
        return None if value is None else _validate_repo_relative_path(value)


class SymbolBinding(_FrozenModel):
    symbol: str = Field(min_length=1)
    kind: SymbolKind
    definition: SourceRange
    signature_text: str = Field(max_length=4096)
    ambiguous: bool = False


class ReferenceSite(_FrozenModel):
    symbol: str = Field(min_length=1)
    site: SourceRange
    binding: SourceRange | None = None
    ambiguous: bool = False


class SymbolCoverageEdge(_FrozenModel):
    test_site: SourceRange
    symbol: SourceRange
    ambiguous: bool = False


class RelationEdge(_FrozenModel):
    kind: Literal["IMPORT", "COVERAGE", "CONTAINS"]
    from_module: ProjectModuleId | None = None
    to_module: ProjectModuleId
    file_path: RepoRelativePath | None = None
    evidence: SourceRange | None = None

    @field_validator("file_path", mode="before")
    @classmethod
    def relation_path_is_safe(cls, value: object) -> object:
        return None if value is None else _validate_repo_relative_path(value)

    @model_validator(mode="after")
    def relation_shape_is_valid(self) -> RelationEdge:
        if self.kind == "CONTAINS":
            if self.from_module is not None or self.file_path is None:
                raise ValueError("contains edges require a file path and no source module")
        elif self.from_module is None or self.file_path is not None:
            raise ValueError("module relation edges require two modules and no file path")
        return self


class AnalysisError(_FrozenModel):
    code: StableErrorCode
    message: str = Field(min_length=1)
    path: RepoRelativePath | None = None

    @field_validator("path", mode="before")
    @classmethod
    def error_path_is_safe(cls, value: object) -> object:
        return None if value is None else _validate_repo_relative_path(value)


class AnalysisResult(_FrozenModel):
    snapshot_oid: str = Field(min_length=1)
    descriptor_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    capability: AnalysisCapability
    modules: list[ModuleFact]
    imports: list[ImportEdge]
    coverage: list[CoverageEntry]
    coverage_status: list[ModuleCoverageStatus]
    conservation: list[TestConservationBaseline]
    manifests: list[ManifestSummary]
    artifacts: list[ArtifactFact]
    symbol_bindings: list[SymbolBinding]
    reference_sites: list[ReferenceSite]
    symbol_coverage: list[SymbolCoverageEdge]
    relation_edges: list[RelationEdge] = Field(default_factory=list)
    skipped_files: list[RepoRelativePath] = Field(default_factory=list)
    errors: list[AnalysisError] = Field(default_factory=list)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


__all__ = [
    "AnalysisCapability",
    "AnalysisError",
    "AnalysisResult",
    "ArtifactFact",
    "ArtifactKind",
    "CoverageDerivation",
    "CoverageEntry",
    "DependencyEntry",
    "EdgeConfidence",
    "ExportSummary",
    "ExternalTarget",
    "ImportEdge",
    "ImportTarget",
    "ManifestSummary",
    "ModuleBoundary",
    "ModuleCoverage",
    "ModuleCoverageStatus",
    "ModuleFact",
    "ModuleRole",
    "ModuleTarget",
    "ReferenceSite",
    "RelationEdge",
    "ScriptEntry",
    "SourcePosition",
    "SourceRange",
    "SymbolBinding",
    "SymbolCoverageEdge",
    "SymbolKind",
    "TestConservationBaseline",
    "UnknownReason",
]
