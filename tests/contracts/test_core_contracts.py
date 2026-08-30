from codemigrator import core


def test_core_public_exports_include_contract_primitives() -> None:
    for name in (
        "ArtifactRef",
        "Advice",
        "CreateRun",
        "PlanEdge",
        "RepairDecision",
        "ToolchainDescriptor",
        "VerificationOutcome",
    ):
        assert hasattr(core, name)
