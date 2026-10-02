from __future__ import annotations

import pytest

from codemigrator.core.models.plan import PlanProposal
from codemigrator.runtime.plan_agent_output import (
    PlanProposalAgentOutput,
    parse_plan_proposal_agent_output,
)


def test_plan_agent_output_converts_strict_wire_shape_to_core_contract() -> None:
    output = PlanProposalAgentOutput.model_validate(
        {
            "slices": [
                {
                    "local_ref": "Core",
                    "kind": "IMPLEMENTATION",
                    "source_modules": [],
                    "write_paths": ["target/core.py"],
                    "create_roots": [],
                    "rationale": [
                        {
                            "kind": "planner",
                            "content": "Keep the core migration isolated.",
                            "anchors": ['{"file":"src/core.py","start_line":1,"end_line":4}'],
                            "advisory": False,
                        }
                    ],
                    "required_checks": [],
                    "artifact_tasks": [],
                    "generated": False,
                    "generation_tag": None,
                    "minimum_nontrivial_assertions": 0,
                    "information_firewall": False,
                }
            ],
            "edges": [],
            "integration_ranks": [{"local_ref": "Core", "rank": 0}],
            "planner_rationale": [],
        }
    )

    proposal = output.to_plan_proposal()

    assert proposal == PlanProposal.model_validate(
        {
            "slices": [
                {
                    "local_ref": "Core",
                    "kind": "IMPLEMENTATION",
                    "source_modules": [],
                    "write_paths": ["target/core.py"],
                    "create_roots": [],
                    "rationale": [
                        {
                            "kind": "planner",
                            "content": "Keep the core migration isolated.",
                            "anchors": [
                                {"file": "src/core.py", "start_line": 1, "end_line": 4}
                            ],
                            "advisory": False,
                        }
                    ],
                    "required_checks": [],
                    "artifact_tasks": [],
                    "generated": False,
                    "generation_tag": None,
                    "minimum_nontrivial_assertions": 0,
                    "information_firewall": False,
                }
            ],
            "edges": [],
            "integration_ranks": {"Core": 0},
            "planner_rationale": [],
        }
    )


def test_plan_agent_output_rejects_duplicate_rank_keys() -> None:
    with pytest.raises(ValueError, match="unique"):
        PlanProposalAgentOutput.model_validate(
            {
                "slices": [],
                "edges": [],
                "integration_ranks": [
                    {"local_ref": "Core", "rank": 0},
                    {"local_ref": "Core", "rank": 1},
                ],
                "planner_rationale": [],
            }
        )


def test_plan_agent_output_normalizes_provider_anchor_objects() -> None:
    wire = {
        "slices": [
            {
                "local_ref": "Core",
                "kind": "IMPLEMENTATION",
                "source_modules": [],
                "write_paths": ["target/core.py"],
                "create_roots": [],
                "rationale": [
                    {
                        "kind": "planner",
                        "content": "Keep the core migration isolated.",
                        "anchors": [
                            {"file": "src/core.py", "start_line": 1, "end_line": 4}
                        ],
                        "advisory": False,
                    },
                    {
                        "kind": "planner",
                        "content": "An unanchored observation is advisory.",
                        "anchors": [],
                        "advisory": False,
                    },
                    {
                        "kind": "planner",
                        "content": "An invalid anchor cannot support a factual rationale.",
                        "anchors": ["not-an-anchor-object"],
                        "advisory": False,
                    },
                ],
                "required_checks": [],
                "artifact_tasks": [],
                "generated": False,
                "generation_tag": None,
                "minimum_nontrivial_assertions": 0,
                "information_firewall": False,
            }
        ],
        "edges": [],
        "integration_ranks": [{"local_ref": "Core", "rank": 0}],
        "planner_rationale": [
            {
                "content": "Use the frozen analysis facts.",
                "anchors": [
                    {"file": "src/core.py", "start_line": 1, "end_line": 4}
                ],
                "advisory": False,
            }
        ],
    }

    parsed = parse_plan_proposal_agent_output(wire)

    assert parsed is not None
    assert parsed["slices"][0]["rationale"][0]["anchors"] == [
        {"file": "src/core.py", "start_line": 1, "end_line": 4}
    ]
    assert parsed["slices"][0]["rationale"][1]["advisory"] is True
    assert parsed["slices"][0]["rationale"][2]["anchors"] == []
    assert parsed["slices"][0]["rationale"][2]["advisory"] is True
    assert parsed["planner_rationale"][0]["anchors"] == [
        {"file": "src/core.py", "start_line": 1, "end_line": 4}
    ]
    assert parsed["planner_rationale"][0]["kind"] == "planner"
    assert "kind" not in wire["planner_rationale"][0]
    assert isinstance(wire["slices"][0]["rationale"][0]["anchors"][0], dict)


def test_plan_agent_output_rederives_generated_metadata_from_slice_kind() -> None:
    parsed = parse_plan_proposal_agent_output(
        {
            "slices": [
                {
                    "local_ref": "Core",
                    "kind": "IMPLEMENTATION",
                    "source_modules": [],
                    "write_paths": ["target/core.py"],
                    "create_roots": [],
                    "rationale": [],
                    "required_checks": [],
                    "artifact_tasks": [],
                    "generated": True,
                    "generation_tag": "GENERATED",
                    "minimum_nontrivial_assertions": 3,
                    "information_firewall": True,
                }
            ],
            "edges": [],
            "integration_ranks": [{"local_ref": "Core", "rank": 0}],
            "planner_rationale": [],
        }
    )

    assert parsed is not None
    slice_ = parsed["slices"][0]
    assert slice_["kind"] == "IMPLEMENTATION"
    assert slice_["generated"] is False
    assert slice_["generation_tag"] is None
    assert slice_["minimum_nontrivial_assertions"] == 0
    assert slice_["information_firewall"] is False
