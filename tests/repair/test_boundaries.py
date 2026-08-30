import uuid

import pytest

from codemigrator.core import (
    AttributionReliability,
    RepairEvidence,
    WriteScope,
    WriteScopeOut,
)
from codemigrator.runtime.repair import (
    RepairBrief,
    RepairConstraints,
    RepairFailureFacts,
    RepairNavigationIndex,
    assemble_repair_brief,
)


def test_oversized_required_fact_must_have_controlled_cas_reference():
    evidence = RepairEvidence(
        candidate_slice_set=[uuid.uuid4()],
        reliability=AttributionReliability.Uncertain,
        strong_coupling=False,
        cross_generation_recurrence=False,
        conservation_signal_summary={},
    )
    facts = RepairFailureFacts(
        failed_test_refs=("test://case",),
        diagnostic_summary={"body": "x" * 100},
        cas_refs=(),
    )
    with pytest.raises(ValueError, match="cas"):
        assemble_repair_brief(
            evidence=evidence,
            failure_facts=facts,
            scope_index=RepairNavigationIndex(paths=("src/a.py",), positions=()),
            repair_history=(),
            constraints=RepairConstraints(
                write_scope=WriteScope(
                    out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[])
                ),
                verification_requirements=("local",),
                impact_preview_required=False,
            ),
            max_inline_bytes=32,
        )


def test_assembled_brief_preserves_input_and_internal_repair_has_no_impact_preview():
    evidence = RepairEvidence(
        candidate_slice_set=[uuid.uuid4()],
        reliability=AttributionReliability.Uncertain,
        strong_coupling=False,
        cross_generation_recurrence=False,
        conservation_signal_summary={"summary": "x" * 100},
    )
    brief = assemble_repair_brief(
        evidence=evidence,
        failure_facts=RepairFailureFacts(
            failed_test_refs=("test://case",),
            diagnostic_summary={"summary": "x" * 100},
            cas_refs=("cas://" + "b" * 64,),
        ),
        scope_index=RepairNavigationIndex(paths=("src/a.py",), positions=()),
        repair_history=(),
        constraints=RepairConstraints(
            write_scope=WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[])),
            verification_requirements=("local",),
            impact_preview_required=False,
        ),
        max_inline_bytes=32,
    )
    assert isinstance(brief, RepairBrief)
    assert brief.failure_facts.diagnostic_summary["summary"] == "x" * 100
    assert brief.constraints.impact_preview_required is False
