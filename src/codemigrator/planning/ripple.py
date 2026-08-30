"""Read-only contract-drift ripple projection."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence

from pydantic import Field

from codemigrator.analysis import AnalysisCapability, AnalysisResult, ModuleRole
from codemigrator.core import ProjectModuleId
from codemigrator.core._base import CoreModel

from .models import FrozenPlan


class RipplePreview(CoreModel):
    """Facts supplied to M-16 without modifying the frozen plan."""

    affected_modules: tuple[ProjectModuleId, ...]
    affected_symbols: tuple[str, ...]
    invalidated_slices: tuple[str, ...]
    rebuilt_slices: tuple[str, ...]
    compensation_slices: tuple[str, ...]
    estimated_slice_count: int = Field(ge=0)
    integration_rank_distribution: dict[int, int]
    degraded: bool
    degradation_reasons: tuple[str, ...]


def calculate_ripple(
    plan: FrozenPlan,
    analysis: AnalysisResult,
    changed_symbols: Sequence[str],
    *,
    integrated_slices: Iterable[str] = (),
) -> RipplePreview:
    """Calculate symbol and dependency closure through the frozen slice map."""

    symbols = tuple(
        sorted(set(changed_symbols), key=lambda symbol: symbol.encode("utf-8"))
    )
    module_for_path = {
        str(path): module.module_id
        for module in analysis.modules
        for path in module.file_paths
    }
    affected: set[ProjectModuleId] = set()
    reasons: list[str] = []

    relevant_bindings = [
        binding for binding in analysis.symbol_bindings if binding.symbol in symbols
    ]
    bindings: dict[str, set[ProjectModuleId]] = defaultdict(set)
    for binding in relevant_bindings:
        if binding.module is not None:
            bindings[binding.symbol].add(binding.module)
    if any(binding.ambiguous for binding in relevant_bindings):
        reasons.append("ambiguous SymbolBinding degraded to module closure")
    for symbol in symbols:
        affected.update(bindings.get(symbol, set()))
    for binding in analysis.symbol_bindings:
        if binding.symbol in symbols and binding.module is None:
            module_id = module_for_path.get(str(binding.definition.file_path))
            if module_id is not None:
                affected.add(module_id)

    matching_references = [
        reference for reference in analysis.reference_sites if reference.symbol in symbols
    ]
    for reference in matching_references:
        module_id = module_for_path.get(str(reference.site.file_path))
        if module_id is None:
            continue
        affected.add(module_id)
        if reference.ambiguous:
            reasons.append(
                f"ambiguous ReferenceSite for {reference.symbol} degraded to module closure"
            )

    if analysis.capability is AnalysisCapability.TextFallback:
        affected.update(
            module.module_id for module in analysis.modules if module.role is ModuleRole.Source
        )
        reasons.append("text-fallback analysis has no reliable symbol closure")

    if not affected:
        # A symbol can be supplied as a changed contract name while the index has
        # no binding.  An exported-symbol lookup is the safe non-fallback route.
        for module in analysis.modules:
            if any(export.symbol in symbols for export in module.exported_symbols):
                affected.add(module.module_id)
        if not affected:
            reasons.append("changed symbols have no PSF-2 binding; module closure is empty")

    downstream = _reverse_import_closure(analysis, affected)
    affected.update(downstream)
    # The proposal map is keyed by local_ref; iterate directly to avoid relying
    # on UUID ordering or the mutable core output projection.
    refs_by_module: dict[ProjectModuleId, set[str]] = defaultdict(set)
    for slice_proposal in plan.proposal.slices:
        for module_id in slice_proposal.source_modules:
            refs_by_module[module_id].add(slice_proposal.local_ref)
    affected_refs = {
        local_ref for module_id in affected for local_ref in refs_by_module.get(module_id, set())
    }
    ref_set = set(plan.local_ref_to_id)
    integrated = {item for item in integrated_slices if item in ref_set}
    rank_by_ref = plan.proposal.integration_ranks

    def sort_ref(local_ref: str) -> tuple[int, str]:
        return rank_by_ref[local_ref], local_ref

    invalidated = tuple(sorted(affected_refs - integrated, key=sort_ref))
    rebuilt = invalidated
    compensation = tuple(sorted(affected_refs.intersection(integrated), key=sort_ref))
    distribution: dict[int, int] = defaultdict(int)
    for local_ref in affected_refs:
        distribution[rank_by_ref[local_ref]] += 1
    return RipplePreview(
        affected_modules=tuple(sorted(affected, key=lambda item: item.bytes)),
        affected_symbols=symbols,
        invalidated_slices=invalidated,
        rebuilt_slices=rebuilt,
        compensation_slices=compensation,
        estimated_slice_count=len(invalidated) + len(compensation),
        integration_rank_distribution=dict(sorted(distribution.items())),
        degraded=bool(reasons),
        degradation_reasons=tuple(dict.fromkeys(reasons)),
    )


def _reverse_import_closure(
    analysis: AnalysisResult, seeds: Iterable[ProjectModuleId]
) -> set[ProjectModuleId]:
    reverse: dict[ProjectModuleId, set[ProjectModuleId]] = defaultdict(set)
    for edge in analysis.imports:
        target = getattr(edge.to, "module_id", None)
        if target is not None:
            reverse[target].add(edge.from_module)
    for relation in analysis.relation_edges:
        if relation.kind in {"IMPORT", "COVERAGE"} and relation.from_module is not None:
            reverse[relation.to_module].add(relation.from_module)
    seen = set(seeds)
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        for dependent in reverse.get(current, set()):
            if dependent not in seen:
                seen.add(dependent)
                queue.append(dependent)
    return seen


__all__ = ["RipplePreview", "calculate_ripple"]
