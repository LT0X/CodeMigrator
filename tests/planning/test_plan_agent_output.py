from __future__ import annotations

import pytest

from codemigrator.core.models.plan import PlanProposal
from codemigrator.runtime.plan_agent_output import PlanProposalAgentOutput


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
