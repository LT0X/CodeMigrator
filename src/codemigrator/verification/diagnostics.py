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

Parser = Callable[[str, str], list[DiagnosticMapping]]


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


def _parse_file_lines(text: str, program: str) -> list[DiagnosticMapping]:
    mappings: list[DiagnosticMapping] = []
    for line in text.splitlines():
        match = _FILE_LINE.match(line.strip())
        if match is None:
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
    return mappings


def _parse_pytest(text: str, program: str) -> list[DiagnosticMapping]:
    del program
    mappings: list[DiagnosticMapping] = []
    for line in text.splitlines():
        match = _PYTEST_FAILURE.match(line.strip())
        if match is None:
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
    return mappings


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
    cross_generation_recurrence: bool = False


@dataclass(frozen=True)
class AttributionResult:
    """Mechanical evidence; it is not a Supervisor decision."""

    candidate_slice_set: tuple[object, ...]
    reliability: AttributionReliability
    strong_coupling: bool = False
    cross_generation_recurrence: bool = False
    reason: str = ""

    def to_repair_evidence(self) -> RepairEvidence:
        return RepairEvidence(
            candidate_slice_set=[_as_slice_id(item) for item in self.candidate_slice_set],
            reliability=self.reliability,
            strong_coupling=self.strong_coupling,
            cross_generation_recurrence=self.cross_generation_recurrence,
            conservation_signal_summary={},
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

    strong_coupling = (
        bool(context.interface_definition_slices and context.call_site_slices)
        and len(set(context.interface_definition_slices) | set(context.call_site_slices)) >= 2
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

    if dynamic:
        reliability = AttributionReliability.Dynamic
        reason = "symbol_evidence" if symbol_candidates else "file_fallback"
    elif len(candidates) == 1 and not strong_coupling:
        reliability = AttributionReliability.Reliable
        reason = "static_unique_scope"
    else:
        reliability = AttributionReliability.Uncertain
        reason = "static_ambiguous_scope" if candidates else "static_no_scope"
    return AttributionResult(
        candidate_slice_set=_ordered(candidates),
        reliability=reliability,
        strong_coupling=strong_coupling,
        cross_generation_recurrence=context.cross_generation_recurrence,
        reason=reason,
    )
