from codemigrator.core.enums import SessionKind
from codemigrator.core.policy import (
    load_phase_tool_policy,
    load_session_budget,
    load_session_templates,
)


def test_policy_resources_keep_empty_phases_and_drafting_out_of_session_kind() -> None:
    policy = load_phase_tool_policy()
    assert policy["VERIFY"] == []
    assert policy["REPORT"] == []

    budget = load_session_budget()
    templates = load_session_templates()
    assert len(budget) == len(templates) == 10
    assert "DRAFTING" in templates
    assert "DRAFTING" not in {member.value for member in SessionKind}
