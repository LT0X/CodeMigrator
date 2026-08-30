"""Data-driven Slice, artifact, naming, and test-anchor derivation helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Literal

from codemigrator.analysis import (
    AnalysisCapability,
    AnalysisResult,
    ArtifactFact,
    ModuleCoverage,
    ModuleFact,
    ModuleRole,
)
from codemigrator.core import (
    ArtifactKind,
    DossierEntry,
    DossierEntryKind,
    ProjectModuleId,
    RepoRelativePath,
    SliceKind,
)
from codemigrator.core._base import CoreModel

from .models import ArtifactAction, ArtifactTask, PlanningInputs, PlanProposal, PlanSliceProposal


class TestGenerationAnchor(CoreModel):
    """The symbol or module-level context allowed into a generated-test session."""

    module_id: ProjectModuleId
    level: Literal["SYMBOL", "MODULE"]
    symbols: tuple[str, ...]
    module_summary: tuple[str, ...]
    degraded: bool
    reason: str
    information_firewall: bool = True


def normalize_group_name(value: str) -> str:
    """Normalize a group name to a stable target naming component."""

    if not isinstance(value, str):
        raise TypeError("group name must be a string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return normalized or "group"


def normalize_group_names(values: Sequence[str]) -> tuple[str, ...]:
    """Normalize names and suffix collisions in input order."""

    counts: dict[str, int] = {}
    result: list[str] = []
    for value in values:
        base = normalize_group_name(value)
        counts[base] = counts.get(base, 0) + 1
        result.append(base if counts[base] == 1 else f"{base}-{counts[base]}")
    return tuple(result)


def derive_artifact_tasks(
    facts: Sequence[ArtifactFact],
    *,
    owner_local_ref: str,
    target_paths: Mapping[str, str] | None = None,
) -> tuple[ArtifactTask, ...]:
    """Map F4 artifact facts to the one permitted action for their kind."""

    del owner_local_ref  # Ownership is the containing Slice's proposal field.
    paths = target_paths or {}
    tasks: list[ArtifactTask] = []
    actions = {
        ArtifactKind.GeneratedCode: ArtifactAction.Generate,
        ArtifactKind.DeclarativeConfig: ArtifactAction.Translate,
        ArtifactKind.ResourceFile: ArtifactAction.Copy,
    }
    for fact in sorted(facts, key=lambda item: str(item.path).encode("utf-8")):
        if fact.artifact_kind is ArtifactKind.GeneratedCode and fact.source_path is None:
            raise ValueError("GeneratedCode artifacts require source_path")
        source_path = str(fact.source_path or fact.path)
        target_path = paths.get(str(fact.path), f"target/{fact.path}")
        tasks.append(
            ArtifactTask(
                kind=fact.artifact_kind,
                action=actions[fact.artifact_kind],
                source_path=RepoRelativePath(source_path),
                target_path=RepoRelativePath(target_path),
                artifact_path=fact.path,
            )
        )
    return tuple(tasks)


def derive_plan_proposal(inputs: PlanningInputs) -> PlanProposal:
    """Build a conservative no-model proposal from analysis facts.

    This helper is a deterministic fixture/adapter for callers that already have
    grouping facts.  It does not replace an LLM planner; all resulting facts still
    pass through :class:`PlanValidator` before freeze.
    """

    modules = sorted(inputs.analysis.modules, key=lambda item: item.module_id.bytes)
    source_modules = [
        module
        for module in modules
        if module.role is ModuleRole.Source and _module_is_in_scope(module, inputs)
    ]
    test_modules = [
        module
        for module in modules
        if module.role is ModuleRole.Test and _module_is_in_scope(module, inputs)
    ]
    names = normalize_group_names([str(module.module_id) for module in source_modules])
    slices: list[PlanSliceProposal] = []

    artifact_tasks = derive_artifact_tasks(
        inputs.analysis.artifacts,
        owner_local_ref="CT",
    )
    if artifact_tasks:
        slices.append(
            PlanSliceProposal(
                local_ref="CT",
                kind=SliceKind.Contract,
                source_modules=(),
                write_paths=tuple(task.target_path for task in artifact_tasks),
                create_roots=(),
                artifact_tasks=artifact_tasks,
                rationale=(_rationale("classified artifacts use descriptor-declared handling"),),
            )
        )

    for module, name in zip(source_modules, names, strict=True):
        paths = _target_paths(module.file_paths, prefix=f"target/src/{name}")
        slices.append(
            PlanSliceProposal(
                local_ref=f"I-{name}",
                kind=SliceKind.Implementation,
                source_modules=(module.module_id,),
                write_paths=paths,
                create_roots=(RepoRelativePath(f"target/src/{name}"),),
                rationale=(_rationale("implementation follows the source module boundary"),),
            )
        )

    status_by_module = {status.module: status.status for status in inputs.analysis.coverage_status}
    covered_test_modules = _covered_test_modules(inputs.analysis, test_modules)
    for test_module in test_modules:
        if test_module.module_id in covered_test_modules:
            name = normalize_group_name(str(test_module.module_id))
            slices.append(
                PlanSliceProposal(
                    local_ref=f"TT-{name}",
                    kind=SliceKind.TestTranslation,
                    source_modules=(test_module.module_id,),
                    write_paths=_target_paths(
                        test_module.file_paths, prefix=f"target/tests/{name}"
                    ),
                    create_roots=(RepoRelativePath(f"target/tests/{name}"),),
                    rationale=(_rationale("covered source tests use the translation track"),),
                )
            )

    for module, name in zip(source_modules, names, strict=True):
        if status_by_module.get(module.module_id) is not ModuleCoverage.EmptyTestSuite:
            continue
        slices.append(
            PlanSliceProposal(
                local_ref=f"TG-{name}",
                kind=SliceKind.TestGeneration,
                source_modules=(module.module_id,),
                write_paths=(RepoRelativePath(f"target/tests/generated/{name}/{name}_test"),),
                create_roots=(RepoRelativePath(f"target/tests/generated/{name}"),),
                rationale=(_rationale("empty test suite requires behavior-anchored generation"),),
            )
        )

    return PlanProposal(
        slices=slices,
        edges=[],
        integration_ranks={slice_.local_ref: index for index, slice_ in enumerate(slices)},
        planner_rationale=[_rationale("derived from frozen analysis facts")],
    )


def resolve_test_generation_anchor(
    module_id: ProjectModuleId, analysis: AnalysisResult
) -> TestGenerationAnchor:
    """Resolve symbol anchors, degrading visibly when the index is unreliable."""

    module = next((item for item in analysis.modules if item.module_id == module_id), None)
    if module is None:
        raise ValueError("module is not present in analysis facts")
    module_paths = set(module.file_paths)
    references = [
        site for site in analysis.reference_sites if site.site.file_path in module_paths
    ]
    symbols = tuple(
        sorted({site.symbol for site in references}, key=lambda item: item.encode("utf-8"))
    )
    ambiguous = any(site.ambiguous for site in references)
    summary = tuple(
        f"{export.symbol}: {export.signature_text}"
        for export in module.exported_symbols
    )
    if analysis.capability is AnalysisCapability.TextFallback or ambiguous or not symbols:
        reason = (
            "text fallback prevents reliable symbol anchoring"
            if analysis.capability is AnalysisCapability.TextFallback
            else "ambiguous or missing symbol references require module fallback"
        )
        return TestGenerationAnchor(
            module_id=module_id,
            level="MODULE",
            symbols=(),
            module_summary=summary,
            degraded=True,
            reason=reason,
        )
    return TestGenerationAnchor(
        module_id=module_id,
        level="SYMBOL",
        symbols=symbols,
        module_summary=summary,
        degraded=False,
        reason="PSF-2 symbol references are unambiguous",
    )


def _covered_test_modules(
    analysis: AnalysisResult, test_modules: Sequence[ModuleFact]
) -> set[ProjectModuleId]:
    paths_by_module = {
        module.module_id: set(module.file_paths)
        for module in test_modules
    }
    return {
        module_id
        for entry in analysis.coverage
        for module_id, paths in paths_by_module.items()
        if entry.test_file in paths
    }


def _module_is_in_scope(module: ModuleFact, inputs: PlanningInputs) -> bool:
    return all(inputs.spec.scope.includes(str(path)) for path in module.file_paths)


def _target_paths(paths: Sequence[str], *, prefix: str) -> tuple[RepoRelativePath, ...]:
    return tuple(
        RepoRelativePath(f"{prefix}/{str(path).rsplit('/', maxsplit=1)[-1]}")
        for path in sorted(paths, key=lambda item: str(item).encode("utf-8"))
    )


def _rationale(content: str) -> DossierEntry:
    return DossierEntry(
        kind=DossierEntryKind("planner"), content=content, anchors=[], advisory=True
    )


__all__ = [
    "TestGenerationAnchor",
    "derive_artifact_tasks",
    "derive_plan_proposal",
    "normalize_group_name",
    "normalize_group_names",
    "resolve_test_generation_anchor",
]
