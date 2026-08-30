from codemigrator import core


def test_core_public_exports_include_contract_primitives() -> None:
    for name in (
        "ArtifactRef",
        "Advice",
        "CreateRun",
        "DerivedVerificationGuard",
        "IntegrationIntent",
        "PlanEdge",
        "RepairDecision",
        "RequestId",
        "ToolchainDescriptor",
        "VerificationOutcome",
        "validate_candidate_generation",
    ):
        assert hasattr(core, name)
