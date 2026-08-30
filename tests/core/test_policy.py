import hashlib

import pytest

from codemigrator.core.policy import (
    load_phase_tool_policy,
    load_resource,
    load_session_budget,
    load_session_templates,
    load_verification_policy,
)


def test_phase_policy_is_exact_and_versioned() -> None:
    assert load_phase_tool_policy() == {
        "PLAN": ["ReadFile", "QuerySourceAst", "Exec"],
        "EXECUTE": ["ReadFile", "WriteFile", "EditFile", "QuerySourceAst", "Shell", "Exec"],
        "VERIFY": [],
        "REPORT": [],
    }
    document = load_resource("core://phase-tool-policy/v2")
    assert document.uri == "core://phase-tool-policy/v2"
    assert document.version == 2
    assert document.sha256 == "ed92ba9a5ac7610ed480b2fa2266f6f490e209a51790c1e2dcbfa7b4e19c0391"


def test_resource_digest_is_sha256_of_canonical_payload() -> None:
    document = load_resource("core://phase-tool-policy/v2")
    canonical_payload = (
        b'{"EXECUTE":["ReadFile","WriteFile","EditFile","QuerySourceAst","Shell","Exec"],'
        b'"PLAN":["ReadFile","QuerySourceAst","Exec"],"REPORT":[],"VERIFY":[]}'
    )
    assert document.sha256 == hashlib.sha256(canonical_payload).hexdigest()


def test_versioned_resources_cover_verification_and_ten_template_slots() -> None:
    verification = load_verification_policy()
    assert verification["flaky_reruns"] == 2
    assert verification["majority"] == {"required": 2, "total": 3}
    assert verification["feedback_repair_limit"] == 2
    assert verification["conservation_bandwidth"] == [0.5, 2.0]
    assert verification["global_repair_attempts"] == 3

    budget = load_session_budget()
    assert set(budget) == {
        "ANALYZE_AUXILIARY",
        "PLAN_AUXILIARY",
        "CONTRACT",
        "IMPLEMENTATION",
        "TEST_TRANSLATION",
        "TEST_GENERATION",
        "EXPLORE_COORDINATOR",
        "EXECUTE_SUPERVISOR",
        "REPAIR_SESSION",
        "DRAFTING",
    }
    assert budget["IMPLEMENTATION"] == {"max_rounds": 500, "eviction_watermark_pct": 75}
    assert budget["DRAFTING"] == {"max_rounds": 200, "eviction_watermark_pct": 80}

    templates = load_session_templates()
    assert set(templates) == set(budget)
    assert all(isinstance(value, str) and value for value in templates.values())


def test_missing_or_unsupported_resource_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported resource URI"):
        load_resource("core://phase-tool-policy/v9")
    with pytest.raises(ValueError, match="unsupported resource URI"):
        load_resource("file:///tmp/policy.json")


def test_loaded_resource_is_a_fresh_copy() -> None:
    first = load_phase_tool_policy()
    first["PLAN"].append("Shell")
    assert load_phase_tool_policy()["PLAN"] == ["ReadFile", "QuerySourceAst", "Exec"]
