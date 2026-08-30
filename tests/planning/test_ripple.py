from __future__ import annotations

from codemigrator.analysis import (
    EdgeConfidence,
    ImportEdge,
    ModuleTarget,
    ReferenceSite,
    SourcePosition,
    SourceRange,
    SymbolBinding,
    SymbolKind,
)
from codemigrator.core import ProjectModuleId
from codemigrator.planning import PlanLedger, calculate_ripple

from .conftest import module_id
from .test_validation import proposal, slice_


def test_ripple_uses_symbol_references_then_reverse_dependency_then_slice_mapping(
    planning_inputs: object,
) -> None:
    evidence = SourceRange(
        file_path="src/b.ts",
        start=SourcePosition(line=1, column=0),
        end=SourcePosition(line=1, column=1),
    )
    analysis = planning_inputs.analysis.model_copy(
        update={
            "reference_sites": [
                ReferenceSite(symbol="User", site=evidence, ambiguous=False)
            ],
            "symbol_bindings": [
                SymbolBinding(
                    symbol="User",
                    kind=SymbolKind.Type,
                    definition=SourceRange(
                        file_path="src/a.ts",
                        start=SourcePosition(line=1, column=0),
                        end=SourcePosition(line=1, column=4),
                    ),
                    signature_text="type User",
                    module=ProjectModuleId(module_id(1)),
                )
            ],
            "imports": [
                ImportEdge(
                    from_module=ProjectModuleId(module_id(2)),
                    to=ModuleTarget(module_id=ProjectModuleId(module_id(1))),
                    confidence=EdgeConfidence.Static,
                    evidence=evidence,
                )
            ],
        }
    )
    inputs = planning_inputs.model_copy(update={"analysis": analysis})
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
    )
    frozen = PlanLedger().freeze(plan, inputs)

    preview = calculate_ripple(frozen, analysis, ["User"], integrated_slices=["A"])

    assert preview.affected_modules == (module_id(1), module_id(2))
    assert preview.affected_symbols == ("User",)
    assert preview.invalidated_slices == ("B",)
    assert preview.compensation_slices == ("A",)
    assert preview.estimated_slice_count == 2
    assert preview.integration_rank_distribution == {0: 1, 1: 1}


def test_ripple_marks_text_fallback_as_degraded_and_does_not_mutate_plan(
    planning_inputs: object,
) -> None:
    analysis = planning_inputs.analysis.model_copy(update={"capability": "TEXT_FALLBACK"})
    inputs = planning_inputs.model_copy(update={"analysis": analysis})
    plan = proposal(
        slice_("A", 1, "target/a.py", root="target/a"),
        slice_("B", 2, "target/b.py", root="target/b"),
    )
    ledger = PlanLedger()
    frozen = ledger.freeze(plan, inputs)
    before = ledger.records[0].plan_hash

    preview = calculate_ripple(frozen, analysis, ["User"])

    assert preview.degraded is True
    assert preview.degradation_reasons
    assert ledger.records[0].plan_hash == before
