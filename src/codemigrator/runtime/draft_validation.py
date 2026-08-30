"""Pure validation functions used before a draft can enter CreateRun."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from codemigrator.analysis import SourcePosition, SourceRange
from codemigrator.core import MigrationSpec, RepoRelativePath, SpecScope, UnderstandingDossier
from codemigrator.core.paths import _validate_repo_relative_path, normalize_repo_relative_paths
from codemigrator.core.spec import SpecArtifact

from .draft_models import (
    CoverageResult,
    DomainSkeleton,
    DossierConsistencyResult,
)


def build_domain_skeleton(
    module_files: Mapping[str, Sequence[str]],
    *,
    max_files_per_domain: int = 20,
    max_fanout: int = 6,
) -> tuple[DomainSkeleton, ...]:
    """Build a stable exploration skeleton from deterministic module candidates.

    A large module is split by its first child directory.  Files directly under
    the module remain in the module domain because there is no safer directory
    boundary to infer.  The function never silently drops or duplicates a file.
    """

    if type(max_files_per_domain) is not int or max_files_per_domain < 1:
        raise ValueError("max_files_per_domain must be a positive integer")
    if type(max_fanout) is not int or max_fanout < 1:
        raise ValueError("max_fanout must be a positive integer")
    if not isinstance(module_files, Mapping) or not module_files:
        raise ValueError("module_files must contain at least one module")
    if any(not isinstance(path, str) for path in module_files):
        raise ValueError("module paths must be strings")

    domains: list[DomainSkeleton] = []
    seen_files: set[str] = set()
    for raw_module_path in sorted(module_files, key=lambda path: path.encode("utf-8")):
        module_path = _validate_repo_relative_path(raw_module_path)
        raw_files = module_files[raw_module_path]
        if isinstance(raw_files, (str, bytes)) or not isinstance(raw_files, Sequence):
            raise TypeError("module files must be a sequence")
        files = normalize_repo_relative_paths(raw_files)
        if not files:
            raise ValueError(f"module {module_path!r} must contain at least one file")
        if len(files) != len(raw_files):
            raise ValueError(f"module {module_path!r} contains duplicate files")
        module_prefix = f"{module_path}/"
        if any(
            file_path != module_path and not file_path.startswith(module_prefix)
            for file_path in files
        ):
            raise ValueError(f"files must be under module {module_path!r}")
        overlap = seen_files.intersection(files)
        if overlap:
            raise ValueError(f"files occur in more than one module: {sorted(overlap)!r}")
        seen_files.update(files)

        if len(files) <= max_files_per_domain:
            domains.append(
                DomainSkeleton(
                    domain_path=RepoRelativePath(module_path),
                    files=tuple(RepoRelativePath(path) for path in files),
                )
            )
            continue

        grouped: dict[str, list[str]] = {}
        for file_path in files:
            relative = file_path.removeprefix(module_prefix)
            relative_parts = relative.split("/")
            child = relative_parts[0] if len(relative_parts) > 1 else None
            domain_path = f"{module_path}/{child}" if child else module_path
            grouped.setdefault(domain_path, []).append(file_path)
        domains.extend(
            DomainSkeleton(
                domain_path=RepoRelativePath(domain_path),
                files=tuple(RepoRelativePath(path) for path in group_files),
            )
            for domain_path, group_files in sorted(
                grouped.items(), key=lambda item: item[0].encode("utf-8")
            )
        )

    if len(domains) > max_fanout:
        raise ValueError(
            f"exploration fanout {len(domains)} exceeds configured maximum {max_fanout}"
        )
    return tuple(domains)


def validate_exact_coverage(
    skeleton: Sequence[DomainSkeleton], expected_files: Sequence[str]
) -> CoverageResult:
    """Return whether the exploration skeleton covers every file exactly once."""

    if isinstance(skeleton, (str, bytes)) or not isinstance(skeleton, Sequence):
        raise TypeError("skeleton must be a sequence")
    expected_raw = list(expected_files)
    expected = normalize_repo_relative_paths(expected_raw)
    expected_duplicates = _duplicates(expected_raw)

    actual_raw = [file_path for domain in skeleton for file_path in domain.files]
    actual = normalize_repo_relative_paths(actual_raw)
    actual_duplicates = _duplicates(actual_raw)
    expected_set = set(expected)
    actual_set = set(actual)
    missing = tuple(path for path in expected if path not in actual_set)
    unknown = tuple(path for path in actual if path not in expected_set)
    duplicate_files = tuple(
        sorted(
            set(expected_duplicates).union(actual_duplicates),
            key=lambda path: path.encode("utf-8"),
        )
    )
    return CoverageResult(
        valid=(
            not missing
            and not unknown
            and not duplicate_files
            and len(actual_raw) == len(expected)
        ),
        missing_files=tuple(RepoRelativePath(path) for path in missing),
        duplicate_files=tuple(RepoRelativePath(path) for path in duplicate_files),
        unknown_files=tuple(RepoRelativePath(path) for path in unknown),
    )


def check_dossier_consistency(
    dossier: UnderstandingDossier,
    spec_or_scope: MigrationSpec | SpecArtifact | SpecScope,
    f1_files: Sequence[str],
    unresolved_conflict_count: int,
) -> DossierConsistencyResult:
    """Perform the D-01 mechanical dossier checks without side effects."""

    if type(unresolved_conflict_count) is not int or unresolved_conflict_count < 0:
        raise ValueError("unresolved_conflict_count must be a non-negative integer")
    if isinstance(spec_or_scope, SpecArtifact):
        scope = spec_or_scope.spec.scope
    elif isinstance(spec_or_scope, MigrationSpec):
        scope = spec_or_scope.scope
    else:
        scope = spec_or_scope
    f1 = set(normalize_repo_relative_paths(f1_files))
    reasons: list[str] = []
    sections = (
        ("architecture_narrative", dossier.architecture_narrative),
        ("semantic_modules", dossier.semantic_modules),
        ("dependency_resolutions", dossier.dependency_resolutions),
        ("test_map", dossier.test_map),
        ("risk_hotspots", dossier.risk_hotspots),
        ("strategy_advice", dossier.strategy_advice),
    )

    for section_name, entries in sections:
        for entry_index, entry in enumerate(entries):
            if not entry.anchors:
                if not entry.advisory:
                    reasons.append(f"{section_name}[{entry_index}] has empty non-advisory anchors")
                continue
            for anchor_index, anchor in enumerate(entry.anchors):
                try:
                    parsed = _parse_anchor(anchor)
                except (TypeError, ValueError) as exc:
                    label = (
                        "semantic module"
                        if section_name == "semantic_modules"
                        else section_name
                    )
                    reasons.append(
                        f"{label}[{entry_index}] anchor[{anchor_index}] invalid: {exc}"
                    )
                    continue
                if section_name == "semantic_modules":
                    path = str(parsed.file_path)
                    if path not in f1 or not scope.includes(path):
                        reasons.append(
                            f"semantic module anchor {path!r} is outside the Spec scope or F1 files"
                        )

    if unresolved_conflict_count:
        reasons.append(
            f"dossier merge has {unresolved_conflict_count} unresolved conflict(s)"
        )
    valid = not reasons
    return DossierConsistencyResult(
        valid=valid,
        reasons=tuple(reasons),
        unresolved_conflict_count=unresolved_conflict_count,
        reason_code=None if valid else "DOSSIER_INCONSISTENT",
    )


def _duplicates(paths: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for path in paths:
        if path in seen:
            duplicates.add(path)
        seen.add(path)
    return tuple(sorted(duplicates, key=lambda path: path.encode("utf-8")))


_COMPACT_RANGE = re.compile(r"^(?P<start>[1-9][0-9]*)\s*[-:]\s*(?P<end>[1-9][0-9]*)$")


def _parse_anchor(anchor: object) -> SourceRange:
    if not isinstance(anchor, Mapping):
        raise TypeError("anchor must be an object")
    raw_path = anchor.get("file", anchor.get("file_path"))
    path = _validate_repo_relative_path(raw_path)
    start = anchor.get("start_line")
    end = anchor.get("end_line")
    if start is None or end is None:
        raw_start = anchor.get("start")
        raw_end = anchor.get("end")
        if isinstance(raw_start, Mapping) and isinstance(raw_end, Mapping):
            start = raw_start.get("line")
            end = raw_end.get("line")
    if start is None or end is None:
        compact = anchor.get("range")
        if not isinstance(compact, str):
            raise ValueError("anchor needs start_line and end_line")
        match = _COMPACT_RANGE.fullmatch(compact.strip())
        if match is None:
            raise ValueError("range must be a positive line range such as 1-3")
        start = int(match.group("start"))
        end = int(match.group("end"))
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise ValueError("anchor line range must be positive and ordered")
    return SourceRange(
        file_path=RepoRelativePath(path),
        start=SourcePosition(line=start, column=0),
        end=SourcePosition(line=end, column=0),
    )


__all__ = [
    "build_domain_skeleton",
    "check_dossier_consistency",
    "validate_exact_coverage",
]
