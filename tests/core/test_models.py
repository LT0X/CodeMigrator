import uuid

import pytest
import semver
from pydantic import ValidationError

from codemigrator.core.enums import (
    AdviceKind,
    AttributionReliability,
    CheckAction,
    CheckStatus,
    DiagnosticSeverity,
    Phase,
    ResidentRole,
    SessionKind,
    SliceKind,
)
from codemigrator.core.ids import (
    AdviceId,
    CheckId,
    CorrectionIntentId,
    GitOid,
    PlanRevisionId,
    ProjectId,
    ProjectModuleId,
    ProjectSnapshotId,
    RepairDecisionId,
    RepoRelativePath,
    RunId,
    Sha256,
    SliceId,
    new_uuid7,
)
from codemigrator.core.models.advice import Advice, GlobalRepairSession, RepairDecision
from codemigrator.core.models.common import (
    ArtifactRef,
    DossierEntry,
    MigrationRulebook,
    RulebookEntry,
    TargetProjectBlueprint,
    UnderstandingDossier,
)
from codemigrator.core.models.context import ContextPack, ContextPackIdentity, SessionBudgetProfile
from codemigrator.core.models.descriptor import (
    CheckCommandTemplate,
    ContractArtifact,
    ManifestParserRef,
    SourceToolchain,
    TargetToolchain,
    ToolchainDescriptor,
    TreeSitterGrammarRef,
)
from codemigrator.core.models.plan import PlanEdge, PlanProposal, PlanValidation
from codemigrator.core.models.run import (
    CreateRun,
    FrozenArtifactBundle,
    GitRunRefs,
    RegisteredProject,
    RemoteRepository,
)
from codemigrator.core.models.slice import ActiveDispatch, MigrationSlice, SliceCandidate, WriteScope, WriteScopeOut
from codemigrator.core.models.verification import (
    CheckResult,
    DiagnosticMapping,
    DerivedVerificationGuard,
    FileLine,
    FinalVerified,
    IntegrationIntent,
    LocalCandidate,
    ProspectiveIntegration,
    RepairEvidence,
    TestIdentity as DiagnosticTestIdentity,
    VerificationOutcome,
)


def uid() -> uuid.UUID:
    return new_uuid7()


def ref() -> ArtifactRef:
    return ArtifactRef(sha256=Sha256("a" * 64), size=1, media_type="text/plain")


def test_core_models_round_trip_and_reject_unknown_fields() -> None:
    artifact = ref()
    assert ArtifactRef.model_validate_json(artifact.model_dump_json()) == artifact
    with pytest.raises(ValidationError):
        ArtifactRef(sha256="a" * 64, size=1, media_type="text/plain", extra_field=True)


def test_plan_edge_uses_from_alias_in_json() -> None:
    edge = PlanEdge(from_=SliceId(uid()), to=SliceId(uid()), kind="REQUIRES")
    assert "from" in edge.model_dump(by_alias=True)
    assert "from_" not in edge.model_dump(by_alias=True)
    assert PlanEdge.model_validate(edge.model_dump(by_alias=True)) == edge


def test_create_run_source_union_is_closed() -> None:
    artifacts = FrozenArtifactBundle(
        spec=ref(),
        understanding_dossier=ref(),
        target_project_blueprint=ref(),
        migration_rulebook=ref(),
    )
    remote = CreateRun(
        source=RemoteRepository(repository_url="https://example.test/repo.git", base_ref="main"),
        branch_prefix="feature/core",
        frozen_artifacts=artifacts,
    )
    registered = CreateRun(
        source=RegisteredProject(project_id=ProjectId(uid()), snapshot_id=ProjectSnapshotId(uid())),
        branch_prefix="feature/core",
        frozen_artifacts=artifacts,
    )
    assert isinstance(remote.source, RemoteRepository)
    assert isinstance(registered.source, RegisteredProject)
    assert "target_branch" not in remote.model_dump()


def test_discriminated_targets_reject_unknown_variants_and_extra_fields() -> None:
    line = FileLine(kind="FILE_LINE", file_path=RepoRelativePath("src/main.py"), line=7)
    test_identity = DiagnosticTestIdentity(kind="TEST_IDENTITY", test_name="test_main")
    assert DiagnosticMapping(
        severity=DiagnosticSeverity.Error,
        target=line,
        code="E001",
        message_hash=Sha256("b" * 64),
    ).target == line
    assert DiagnosticMapping(
        severity="Warning",
        target=test_identity,
        code="W001",
        message_hash="c" * 64,
    ).target == test_identity
    with pytest.raises(ValidationError):
        DiagnosticMapping.model_validate(
            {
                "severity": "Error",
                "target": {"kind": "NOT_A_TARGET"},
                "code": "E001",
                "message_hash": "b" * 64,
            }
        )
    with pytest.raises(ValidationError):
        DiagnosticMapping.model_validate(
            {
                "severity": "Error",
                "target": {"kind": "UNKNOWN", "line": 1},
                "code": "E001",
                "message_hash": "b" * 64,
            }
        )


def test_verification_subject_variants_and_extra_fields() -> None:
    slice_id = SliceId(uid())
    local = LocalCandidate(
        kind="LOCAL_CANDIDATE",
        slice_id=slice_id,
        generation=0,
        candidate_commit_oid=GitOid("1" * 40),
    )
    prospective = ProspectiveIntegration(
        kind="PROSPECTIVE_INTEGRATION",
        slice_id=slice_id,
        generation=1,
        expected_verified_oid=GitOid("2" * 40),
        prospective_commit_oid=GitOid("3" * 40),
    )
    final = FinalVerified(kind="FINAL_VERIFIED", verified_commit_oid=GitOid("4" * 40))
    assert local.kind == "LOCAL_CANDIDATE"
    assert prospective.generation == 1
    assert final.verified_commit_oid == "4" * 40
    with pytest.raises(ValidationError):
        LocalCandidate(
            kind="LOCAL_CANDIDATE",
            slice_id=slice_id,
            generation=0,
            candidate_commit_oid="1" * 40,
            unexpected="nope",
        )


def test_descriptor_and_plan_contracts_are_constructible() -> None:
    parser = ManifestParserRef(manifest_kind="package.json", parser_id="npm-v1")
    grammar = TreeSitterGrammarRef(grammar_id="typescript", grammar_sha256="d" * 64)
    source = SourceToolchain(
        language_id="typescript",
        extensions=[".ts"],
        parser=grammar,
        manifest_parsers=[parser],
        module_boundary_strategy="MANIFEST_PER_MODULE",
    )
    command = CheckCommandTemplate(action=CheckAction.Compile, program="python", argv=["-m", "compileall"], timeout_secs=300)
    target = TargetToolchain(
        language_id="python",
        package_manager="uv",
        scaffold=[command],
        build=[command],
        test=[command],
        lint=[],
        typecheck=[],
        toolchain_image_digest="e" * 64,
        build_excludes=[".venv"],
    )
    descriptor = ToolchainDescriptor(
        descriptor_version=semver.Version.parse("1.0.0"),
        descriptor_sha256="f" * 64,
        source=source,
        target=target,
    )
    assert descriptor.descriptor_version == semver.Version.parse("1.0.0")
    proposal = PlanProposal(slices=[], edges=[], integration_ranks={}, planner_rationale=[])
    validation = PlanValidation(
        accepted=True,
        violations=[],
        source_coverage={},
        checked_scope_pairs=0,
        cycle_check="PASS",
        size_check="PASS",
    )
    assert proposal.slices == []
    assert validation.accepted is True


def test_context_advice_repair_and_slice_contracts() -> None:
    run_id = RunId(uid())
    slice_id = SliceId(uid())
    scope = WriteScope(out=WriteScopeOut(write_paths=["src/main.py"], create_roots=["src"]))
    migration_slice = MigrationSlice(
        id=slice_id,
        kind=SliceKind.Implementation,
        source_modules=[ProjectModuleId(uid())],
        write_scope=scope,
        required_checks=[],
        integration_rank=0,
        proposal_ref=None,
    )
    identity = ContextPackIdentity(
        run_id=run_id,
        phase=Phase.Execute,
        session=SessionKind.Implementation,
        slice=None,
        spec_sha256="1" * 64,
        model_binding_sha256="2" * 64,
        phase_policy_sha256="3" * 64,
        contract_refs_sha256="4" * 64,
    )
    budget = SessionBudgetProfile(session=SessionKind.Implementation, max_rounds=500, eviction_watermark_pct=75)
    pack = ContextPack(identity=identity, budget=budget, assembled_tokens=100)
    advice = Advice(
        advice_id=AdviceId(uid()),
        kind=AdviceKind.RouteSuggestion,
        run_id=run_id,
        role=ResidentRole.ExecuteSupervisor,
        payload={"suggested_route": "delegate_regen"},
        proposal_hash="5" * 64,
    )
    decision = RepairDecision(
        decision_id=RepairDecisionId(uid()),
        run_id=run_id,
        repair_set=[slice_id],
        domain_split={slice_id: [RepoRelativePath("src/main.py")]},
        brief_refs=[ref()],
    )
    evidence = RepairEvidence(
        candidate_slice_set=[slice_id],
        reliability=AttributionReliability.Reliable,
        strong_coupling=False,
        cross_generation_recurrence=False,
        conservation_signal_summary={"test_count_ratio": 1.0},
    )
    assert migration_slice.id == slice_id
    assert pack.budget.max_rounds == 500
    assert advice.payload["suggested_route"] == "delegate_regen"
    assert decision.domain_split[slice_id] == ["src/main.py"]
    assert evidence.reliability is AttributionReliability.Reliable


def test_remaining_m00_models_have_closed_shapes() -> None:
    run_id = RunId(uid())
    slice_id = SliceId(uid())
    module_id = ProjectModuleId(uid())
    entry = DossierEntry(kind="architecture", content="facts", anchors=[], advisory=True)
    dossier = UnderstandingDossier(
        architecture_narrative=[entry],
        semantic_modules=[],
        dependency_resolutions=[],
        test_map=[],
        risk_hotspots=[],
        strategy_advice=[],
        coverage_self_report={"touched_paths": []},
        budget_tier="Shallow",
    )
    rulebook = MigrationRulebook(
        entries=[
            RulebookEntry(
                kind="naming",
                content="use snake_case",
                source="DraftingSession",
                rationale_ref=None,
                advisory=True,
            )
        ],
        version=1,
    )
    blueprint = TargetProjectBlueprint(
        module_boundaries=[],
        granularity_principles=[],
        target_layout_principles=[],
        parallelism_rules=[],
        generated_artifact_policy="regenerate",
        version=1,
    )
    candidate = SliceCandidate(
        run_id=run_id,
        slice_id=slice_id,
        generation=2,
        base_verified_oid="0" * 40,
        candidate_commit_oid="1" * 40,
    )
    check_id = CheckId(uid())
    local_subject = LocalCandidate(
        kind="LOCAL_CANDIDATE",
        slice_id=slice_id,
        generation=0,
        candidate_commit_oid="1" * 40,
    )
    dispatch = ActiveDispatch(
        dispatch_attempt_id=uid(),
        subject=local_subject,
        check_id=check_id,
        tested_commit_oid="1" * 40,
    )
    check_result = CheckResult(
        check_id=check_id,
        invocation_hash="2" * 64,
        status=CheckStatus.Passed,
        receipt_id=uid(),
        stdout=ref(),
        stderr=ref(),
        diagnostics=[],
    )
    outcome = VerificationOutcome(
        run_id=run_id,
        subject=local_subject,
        tested_commit_oid="1" * 40,
        frozen_required_checks_sha256="3" * 64,
        check_results=[check_result],
        verification_fingerprint="4" * 64,
    )
    guard = DerivedVerificationGuard(all_required_checks_passed=True, error_unknown_count=0)
    intent = IntegrationIntent(
        run_id=run_id,
        slice_id=slice_id,
        generation=0,
        expected_verified_oid="0" * 40,
        prospective_commit_oid="1" * 40,
        guard_sha256="5" * 64,
        verification_fingerprint="4" * 64,
        idempotency_key="6" * 64,
    )
    git_refs = GitRunRefs(base_commit_oid="0" * 40, verified_commit_oid="1" * 40)
    contract = ContractArtifact(
        module_id=module_id,
        target_module_path="src/main.py",
        public_signatures=["main()"],
        types_hash="7" * 64,
    )
    session = GlobalRepairSession(
        repair_decision_id=RepairDecisionId(uid()),
        run_id=run_id,
        joint_write_scope=WriteScope(
            out=WriteScopeOut(write_paths=["src/main.py"], create_roots=["src"])
        ),
    )
    assert dossier.budget_tier.value == "Shallow"
    assert rulebook.version == blueprint.version == 1
    assert candidate.generation == 2
    assert dispatch.check_id == check_id
    assert outcome.check_results[0].status is CheckStatus.Passed
    assert guard.all_required_checks_passed is True
    assert intent.idempotency_key == "6" * 64
    assert git_refs.verified_commit_oid == "1" * 40
    assert contract.module_id == module_id
    assert session.joint_write_scope.out.create_roots == ["src"]
