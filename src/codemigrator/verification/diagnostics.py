"""Trusted diagnostic parsing and conservative P-09 attribution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from codemigrator.core import (
    AttributionReliability,
    CheckAction,
    DiagnosticMapping,
    DiagnosticSeverity,
    FileLine,
    RepairEvidence,
    RepoRelativePath,
    Sha256,
    SliceId,
    TestIdentity,
    Unknown,
    normalize_repo_relative_paths,
)

_FILE_LINE = re.compile(r"^(?P<path>[^:\s][^:]*):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$")
_PYTEST_FAILURE = re.compile(r"^FAILED\s+(?P<test>\S+?)(?:\s+-\s+(?P<message>.*))?$", re.IGNORECASE)
_CODE_SUFFIX = re.compile(r"\[([A-Za-z0-9_-]+)\]\s*$")
_CODE_PREFIX = re.compile(r"^([A-Z][0-9]{3,5})\b")
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s:]+")

@dataclass(frozen=True)
class ParsedDiagnostics:
    """Parser output including whether every meaningful line was understood."""

    mappings: tuple[DiagnosticMapping, ...]
    complete: bool
    unparsed_lines: tuple[str, ...] = ()


Parser = Callable[[str, str], ParsedDiagnostics | list[DiagnosticMapping]]


def _as_slice_id(value: object) -> SliceId:
    return SliceId(cast(UUID, value))


def normalize_diagnostic_message(message: str) -> str:
    """Remove unstable paths/timestamps and collapse whitespace."""

    normalized = _TIMESTAMP.sub("<timestamp>", message)
    normalized = _ABSOLUTE_PATH.sub("<path>", normalized)
    return " ".join(normalized.split())


def _mapping(
    severity: DiagnosticSeverity,
    target: FileLine | TestIdentity | Unknown,
    code: str,
    message: str,
) -> DiagnosticMapping:
    normalized = normalize_diagnostic_message(message)
    return DiagnosticMapping(
        severity=severity,
        target=target,
        code=code,
        message_hash=Sha256(hashlib.sha256(normalized.encode("utf-8")).hexdigest()),
    )


def _severity_and_code(message: str, program: str) -> tuple[DiagnosticSeverity, str]:
    lower = message.lower()
    severity = DiagnosticSeverity.Warning if "warning" in lower else DiagnosticSeverity.Error
    suffix = _CODE_SUFFIX.search(message)
    if suffix:
        return severity, suffix.group(1)
    prefix = _CODE_PREFIX.match(message)
    if prefix:
        return severity, prefix.group(1)
    return severity, f"{program.upper().replace('-', '_')}_DIAGNOSTIC"


def _safe_file(path: str) -> RepoRelativePath | None:
    try:
        return RepoRelativePath(normalize_repo_relative_paths([path])[0])
    except (TypeError, ValueError):
        return None


def _parse_file_lines(text: str, program: str) -> ParsedDiagnostics:
    mappings: list[DiagnosticMapping] = []
    unparsed: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _FILE_LINE.match(line.strip())
        if match is None:
            unparsed.append(stripped)
            continue
        path = _safe_file(match.group("path"))
        if path is None:
            mappings.append(
                _mapping(
                    DiagnosticSeverity.Error, Unknown(kind="UNKNOWN"), "UNKNOWN_DIAGNOSTIC", line
                )
            )
            continue
        message = match.group("message")
        severity, code = _severity_and_code(message, program)
        mappings.append(
            _mapping(
                severity,
                FileLine(kind="FILE_LINE", file_path=path, line=int(match.group("line"))),
                code,
                message,
            )
        )
    return ParsedDiagnostics(tuple(mappings), not unparsed, tuple(unparsed))


def _parse_pytest(text: str, program: str) -> ParsedDiagnostics:
    del program
    mappings: list[DiagnosticMapping] = []
    unparsed: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _PYTEST_FAILURE.match(stripped)
        if match is None:
            unparsed.append(stripped)
            continue
        message = match.group("message") or "pytest test failed"
        mappings.append(
            _mapping(
                DiagnosticSeverity.Error,
                TestIdentity(kind="TEST_IDENTITY", test_name=match.group("test")),
                "PYTEST_FAILURE",
                message,
            )
        )
    return ParsedDiagnostics(tuple(mappings), not unparsed, tuple(unparsed))


def _unknown(text: str) -> list[DiagnosticMapping]:
    meaningful = [line.strip() for line in text.splitlines() if line.strip()]
    if not meaningful:
        return []
    return [
        _mapping(
            DiagnosticSeverity.Error,
            Unknown(kind="UNKNOWN"),
            "UNKNOWN_DIAGNOSTIC",
            "\n".join(meaningful),
        )
    ]


class DiagnosticParserRegistry:
    """Closed-by-default parser registry keyed by action and executable."""

    def __init__(self) -> None:
        self._parsers: dict[tuple[CheckAction, str], Parser] = {}

    @classmethod
    def with_builtins(cls) -> DiagnosticParserRegistry:
        registry = cls()
        registry.register(CheckAction.Test, "pytest", _parse_pytest)
        for action, program in (
            (CheckAction.Compile, "go"),
            (CheckAction.Lint, "ruff"),
            (CheckAction.TypeCheck, "mypy"),
            (CheckAction.Lint, "go"),
            (CheckAction.Lint, "go vet"),
        ):
            registry.register(action, program, _parse_file_lines)
        return registry

    def register(self, action: CheckAction, program: str, parser: Parser) -> None:
        key = (action, program)
        if key in self._parsers:
            raise ValueError(f"diagnostic parser already registered: {action}/{program}")
        self._parsers[key] = parser

    def parse(self, action: CheckAction, program: str, text: str) -> list[DiagnosticMapping]:
        parser = self._parsers.get((action, program))
        if parser is None:
            return _unknown(text)
        parsed = parser(text, program)
        if isinstance(parsed, ParsedDiagnostics):
            mappings = list(parsed.mappings)
            if parsed.unparsed_lines or not parsed.complete:
                mappings.append(
                    _mapping(
                        DiagnosticSeverity.Error,
                        Unknown(kind="UNKNOWN"),
                        "UNKNOWN_DIAGNOSTIC",
                        "\n".join(parsed.unparsed_lines) or text,
                    )
                )
            return mappings or _unknown(text)
        return parsed or _unknown(text)


@dataclass(frozen=True)
class AttributionContext:
    """Frozen lookup facts supplied by planning and analysis ports."""

    write_scopes: Mapping[object, Iterable[str]]
    test_to_symbols: Mapping[str, Iterable[str]] | None = None
    symbol_to_slices: Mapping[str, Iterable[object]] | None = None
    failure_symbol_by_test: Mapping[str, str] | None = None
    test_to_slices: Mapping[str, Iterable[object]] | None = None
    test_file_to_slices: Mapping[str, Iterable[object]] | None = None
    dependency_file_to_slices: Mapping[str, Iterable[object]] | None = None
    interface_definition_slices: tuple[object, ...] = ()
    call_site_slices: tuple[object, ...] = ()
    interface_definition_files: Mapping[str, Iterable[object]] | None = None
    call_site_files: Mapping[str, Iterable[object]] | None = None
    coupling_evidence_complete: bool = False
    cross_generation_recurrence: bool = False


@dataclass(frozen=True)
class AttributionResult:
    """Mechanical evidence; it is not a Supervisor decision."""

    candidate_slice_set: tuple[object, ...]
    reliability: AttributionReliability
    strong_coupling: bool = False
    cross_generation_recurrence: bool = False
    unknown_error_count: int = 0
    coupling_evidence_complete: bool = False
    reason: str = ""

    def to_repair_evidence(self) -> RepairEvidence:
        return RepairEvidence(
            candidate_slice_set=[_as_slice_id(item) for item in self.candidate_slice_set],
            reliability=self.reliability,
            strong_coupling=self.strong_coupling,
            cross_generation_recurrence=self.cross_generation_recurrence,
            conservation_signal_summary={
                "error_unknown_count": self.unknown_error_count,
                "coupling_evidence_complete": self.coupling_evidence_complete,
            },
        )


def _scope_paths(scope: object) -> set[str]:
    paths = getattr(getattr(scope, "out", None), "write_paths", scope)
    return {str(path) for path in cast(Iterable[object], paths)}


def _ordered(values: Iterable[object]) -> tuple[object, ...]:
    return tuple(sorted(set(values), key=lambda value: str(value)))


def _file_candidates(path: str, context: AttributionContext) -> set[object]:
    return {
        slice_id for slice_id, scope in context.write_scopes.items() if path in _scope_paths(scope)
    }


def _test_file(test_name: str) -> str:
    return test_name.split("::", 1)[0]


def _mapped_slices(
    diagnostics: Iterable[DiagnosticMapping], mapping: Mapping[str, Iterable[object]] | None
) -> set[object]:
    if not mapping:
        return set()
    return {
        slice_id
        for diagnostic in diagnostics
        if isinstance(diagnostic.target, FileLine)
        for slice_id in mapping.get(diagnostic.target.file_path, ())
    }


def attribute_diagnostics(
    diagnostics: Iterable[DiagnosticMapping],
    context: AttributionContext,
    *,
    action: CheckAction | None = None,
) -> AttributionResult:
    """Attribute Error diagnostics with symbol-first, no-guess semantics."""

    errors = [
        diagnostic for diagnostic in diagnostics if diagnostic.severity is DiagnosticSeverity.Error
    ]
    if not errors:
        return AttributionResult((), AttributionReliability.Uncertain, reason="no_error_diagnostic")

    unknown_error_count = sum(
        isinstance(diagnostic.target, Unknown) for diagnostic in errors
    )
    dynamic = action is CheckAction.Test or any(
        isinstance(diagnostic.target, TestIdentity) for diagnostic in errors
    )
    candidates: set[object] = set()
    symbol_candidates: set[object] = set()
    if dynamic and context.test_to_symbols and context.symbol_to_slices:
        for diagnostic in errors:
            if not isinstance(diagnostic.target, TestIdentity):
                continue
            symbols: Iterable[str]
            if (
                context.failure_symbol_by_test
                and diagnostic.target.test_name in context.failure_symbol_by_test
            ):
                symbols = (context.failure_symbol_by_test[diagnostic.target.test_name],)
            else:
                symbols = context.test_to_symbols.get(diagnostic.target.test_name, ())
            for symbol in symbols:
                symbol_candidates.update(context.symbol_to_slices.get(symbol, ()))
        candidates.update(symbol_candidates)

    if dynamic and not symbol_candidates:
        for diagnostic in errors:
            if not isinstance(diagnostic.target, TestIdentity):
                continue
            test_name = diagnostic.target.test_name
            if context.test_to_slices:
                candidates.update(context.test_to_slices.get(test_name, ()))
            if context.test_file_to_slices:
                candidates.update(context.test_file_to_slices.get(_test_file(test_name), ()))

    for diagnostic in errors:
        if isinstance(diagnostic.target, FileLine):
            if not dynamic or not symbol_candidates:
                candidates.update(_file_candidates(diagnostic.target.file_path, context))
                if context.dependency_file_to_slices:
                    candidates.update(
                        context.dependency_file_to_slices.get(diagnostic.target.file_path, ())
                    )

    interface_hits = set(context.interface_definition_slices) & candidates
    interface_hits.update(_mapped_slices(errors, context.interface_definition_files))
    call_hits = set(context.call_site_slices) & candidates
    call_hits.update(_mapped_slices(errors, context.call_site_files))
    strong_coupling = (
        bool(interface_hits and call_hits)
        and len(interface_hits | call_hits) >= 2
    )

    if dynamic:
        reliability = AttributionReliability.Dynamic
        reason = "symbol_evidence" if symbol_candidates else "file_fallback"
    elif unknown_error_count:
        reliability = AttributionReliability.Uncertain
        reason = "unknown_diagnostic"
    elif len(candidates) == 1 and context.coupling_evidence_complete and not strong_coupling:
        reliability = AttributionReliability.Reliable
        reason = "static_unique_scope"
    else:
        reliability = AttributionReliability.Uncertain
        reason = (
            "coupling_evidence_incomplete"
            if not context.coupling_evidence_complete
            else "static_ambiguous_scope"
            if candidates
            else "static_no_scope"
        )
    return AttributionResult(
        candidate_slice_set=_ordered(candidates),
        reliability=reliability,
        strong_coupling=strong_coupling,
        cross_generation_recurrence=context.cross_generation_recurrence,
        unknown_error_count=unknown_error_count,
        coupling_evidence_complete=context.coupling_evidence_complete,
        reason=reason,
    )
