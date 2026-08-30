"""Descriptor-owned extraction rules; no language-specific branches live here."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from codemigrator.core import ArtifactKind, ModuleBoundaryStrategy

from .canonical import canonical_bytes
from .models import AnalysisCapability, EdgeConfidence, UnknownReason


@dataclass(frozen=True)
class TextRule:
    pattern: str
    kind: str

    def __post_init__(self) -> None:
        re.compile(self.pattern)


@dataclass(frozen=True)
class ImportRule:
    pattern: str
    target_group: str = "target"
    confidence: EdgeConfidence = EdgeConfidence.Static
    reason: UnknownReason | None = None
    symbol_group: str | None = None
    local_symbol_group: str | None = None

    def __post_init__(self) -> None:
        compiled = re.compile(self.pattern)
        if self.symbol_group is not None and self.symbol_group not in compiled.groupindex:
            raise ValueError(f"import rule symbol group is missing: {self.symbol_group}")
        if (
            self.local_symbol_group is not None
            and self.local_symbol_group not in compiled.groupindex
        ):
            raise ValueError(
                f"import rule local symbol group is missing: {self.local_symbol_group}"
            )
        confidence = getattr(self.confidence, "value", self.confidence)
        reason = getattr(self.reason, "value", self.reason)
        if confidence == EdgeConfidence.Unknown.value and reason is None:
            raise ValueError("unknown import rule requires a reason")


@dataclass(frozen=True)
class ManifestRule:
    pattern: str
    manifest_kind: str


@dataclass(frozen=True)
class ArtifactRule:
    pattern: str
    artifact_kind: ArtifactKind
    source_pattern: str | None = None
    mapping: str | None = None


def descriptor_pattern_matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


@dataclass(frozen=True)
class SourceAnalysisDescriptor:
    language_id: str
    extensions: tuple[str, ...]
    module_boundary_strategy: ModuleBoundaryStrategy
    test_patterns: tuple[str, ...] = ()
    import_rules: tuple[ImportRule, ...] = ()
    export_rules: tuple[TextRule, ...] = ()
    test_function_rules: tuple[TextRule, ...] = ()
    assertion_rules: tuple[TextRule, ...] = ()
    manifest_rules: tuple[ManifestRule, ...] = ()
    artifact_rules: tuple[ArtifactRule, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    external_packages: tuple[str, ...] = ()
    grammar_id: str | None = "generic"
    grammar_sha256: str | None = "0" * 64
    text_fallback: bool = False
    max_file_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.language_id or self.language_id != self.language_id.lower():
            raise ValueError("descriptor language_id must be lowercase")
        if not self.extensions or any(
            not extension.startswith(".") for extension in self.extensions
        ):
            raise ValueError("descriptor must declare file extensions")
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if not self.text_fallback and (not self.grammar_id or not self.grammar_sha256):
            raise ValueError("full analysis requires a grammar identity")

    @property
    def capability(self) -> AnalysisCapability:
        return AnalysisCapability.TextFallback if self.text_fallback else AnalysisCapability.Full

    @property
    def descriptor_sha256(self) -> str:
        def enum_value(value: object) -> object:
            return getattr(value, "value", value)

        payload = {
            "language_id": self.language_id,
            "extensions": sorted(self.extensions),
            "module_boundary_strategy": getattr(
                self.module_boundary_strategy, "value", self.module_boundary_strategy
            ),
            "test_patterns": sorted(self.test_patterns),
            "import_rules": [
                {
                    "pattern": rule.pattern,
                    "target_group": rule.target_group,
                    "confidence": enum_value(rule.confidence),
                    "reason": enum_value(rule.reason),
                    "symbol_group": rule.symbol_group,
                    "local_symbol_group": rule.local_symbol_group,
                }
                for rule in self.import_rules
            ],
            "export_rules": [
                {"pattern": rule.pattern, "kind": rule.kind} for rule in self.export_rules
            ],
            "test_function_rules": [
                {"pattern": rule.pattern, "kind": rule.kind} for rule in self.test_function_rules
            ],
            "assertion_rules": [
                {"pattern": rule.pattern, "kind": rule.kind} for rule in self.assertion_rules
            ],
            "manifest_rules": [
                {"pattern": rule.pattern, "manifest_kind": rule.manifest_kind}
                for rule in self.manifest_rules
            ],
            "artifact_rules": [
                {
                    "pattern": rule.pattern,
                    "artifact_kind": enum_value(rule.artifact_kind),
                    "source_pattern": rule.source_pattern,
                    "mapping": rule.mapping,
                }
                for rule in self.artifact_rules
            ],
            "aliases": dict(sorted(self.aliases.items())),
            "external_packages": sorted(self.external_packages),
            "grammar_id": self.grammar_id,
            "grammar_sha256": self.grammar_sha256,
            "text_fallback": self.text_fallback,
            "max_file_bytes": self.max_file_bytes,
        }
        return hashlib.sha256(canonical_bytes(payload)).hexdigest()

    def is_source_file(self, path: str) -> bool:
        return any(path.endswith(extension) for extension in self.extensions)

    def is_test_file(self, path: str) -> bool:
        return any(descriptor_pattern_matches(pattern, path) for pattern in self.test_patterns)

    def manifest_kind(self, path: str) -> str | None:
        for rule in self.manifest_rules:
            if descriptor_pattern_matches(rule.pattern, path):
                return rule.manifest_kind
        return None


__all__ = [
    "ArtifactRule",
    "ImportRule",
    "ManifestRule",
    "SourceAnalysisDescriptor",
    "TextRule",
    "descriptor_pattern_matches",
]
