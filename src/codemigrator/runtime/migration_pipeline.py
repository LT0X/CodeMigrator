"""V6 project migration session orchestration.

The pipeline is the local composition seam for the complete migration flow.  It
keeps pre-Run drafting evidence separate from the managed target and delegates
target writes and verification to the existing project migration runner.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from codemigrator.analysis import AnalysisResult, ModuleRole, SourcePosition, SourceRange
from codemigrator.core import (
    CheckAction,
    DescriptorResolution,
    DossierBudgetTier,
    DossierEntry,
    DossierEntryKind,
    FrozenArtifactBundle,
    InMemoryDescriptorRegistry,
    MigrationRulebook,
    MigrationSpec,
    ModelProfile,
    RepoRelativePath,
    RequiredCheckSelection,
    RulebookEntry,
    RulebookEntryKind,
    RuleEntrySource,
    TargetProjectBlueprint,
    UnderstandingDossier,
    canonical_json_bytes,
    validate_spec_bytes,
)
from codemigrator.core.spec import SpecArtifact
from codemigrator.planning import (
    FrozenPlan,
    PlanLedger,
    PlanningInputs,
    PlanningLimits,
    PlanProposal,
    PlanSliceProposal,
    derive_plan_proposal,
)
from codemigrator.workspace import SecureRoot

from .binding import LockedModelBinding
from .context import PromptMessage
from .draft import DraftFlow
from .draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    DraftArtifacts,
    ExplorationReport,
    QuestionOption,
)
from .draft_validation import build_domain_skeleton, check_dossier_consistency
from .project_migration import (
    ProjectMigrationRequest,
    ProjectMigrationRunner,
    ProjectTranslator,
    RepairingProjectTranslator,
    TranslationResult,
    _analysis_snapshot,
    _go_analysis_descriptor,
    _MigrationState,
    _ProjectMigrationPhase,
    _SourceSnapshot,
    _target_path,
    _validated_translation,
    _VerificationRunner,
)
from .project_migration import (
    _write_json as _atomic_write_json,
)
from .provider import OpenAICompatibleProvider, ProviderRequest, ProviderResponse

_STAGES = (
    "PREFLIGHT",
    "NAVIGATION",
    "DRAFT_ALIGNMENT",
    "PLANNING",
    "EXECUTE",
    "VERIFY_INTEGRATE",
    "REPORT",
)
_STAGE_DIRS = {
    "PREFLIGHT": "00-preflight",
    "NAVIGATION": "01-navigation",
    "DRAFT_ALIGNMENT": "02-draft-alignment",
    "PLANNING": "03-planning",
    "EXECUTE": "04-execution",
    "VERIFY_INTEGRATE": "05-verify-integrate",
    "REPORT": "06-report",
}


class PlannerAdvisor(Protocol):
    """A bounded advisory port; machine validation remains authoritative."""

    def advise(
        self, analysis: AnalysisResult, artifacts: DraftArtifacts
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProjectMigrationPipelineRequest:
    source: Path
    target: Path
    state_dir: Path | None = None
    resume: bool = False
    translator: ProjectTranslator | None = None
    planner: PlannerAdvisor | None = None
    verification_runner: _VerificationRunner | None = None
    max_parallelism: int = 1


@dataclass(frozen=True, slots=True)
class ProjectMigrationPipelineReport:
    status: str
    stage: str
    source_digest: str
    target: str
    state_dir: str
    stage_dir: str
    plan_hash: str | None
    included_files: int
    translated_files: int
    copied_files: int
    completed_stages: tuple[str, ...] = ()
    failed_files: tuple[str, ...] = ()
    skipped_paths: tuple[str, ...] = ()
    checks: tuple[dict[str, object], ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "workflow": "V6_FULL_MIGRATION",
            "status": self.status,
            "stage": self.stage,
            "source_digest": self.source_digest,
            "target": self.target,
            "state_dir": self.state_dir,
            "stage_dir": self.stage_dir,
            "plan_hash": self.plan_hash,
            "included_files": self.included_files,
            "translated_files": self.translated_files,
            "copied_files": self.copied_files,
            "completed_stages": list(self.completed_stages),
            "failed_files": list(self.failed_files),
            "skipped_paths": list(self.skipped_paths),
            "checks": [dict(item) for item in self.checks],
            "errors": list(self.errors),
        }


@dataclass(slots=True)
class _PipelineCheckpoint:
    source_digest: str
    descriptor_digest: str
    stages: dict[str, str]
    outputs: dict[str, list[str]]
    current_stage: str = "PREFLIGHT"
    plan_hash: str | None = None
    errors: list[str] | None = None

    @classmethod
    def fresh(cls, source_digest: str, descriptor_digest: str) -> _PipelineCheckpoint:
        return cls(
            source_digest=source_digest,
            descriptor_digest=descriptor_digest,
            stages={stage: "PENDING" for stage in _STAGES},
            outputs={},
            errors=[],
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> _PipelineCheckpoint:
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported full migration pipeline checkpoint schema")
        source_digest = payload.get("source_digest")
        descriptor_digest = payload.get("descriptor_digest")
        stages = payload.get("stages")
        outputs = payload.get("outputs")
        current_stage = payload.get("current_stage", "PREFLIGHT")
        plan_hash = payload.get("plan_hash")
        errors = payload.get("errors", [])
        if not isinstance(source_digest, str) or not isinstance(descriptor_digest, str):
            raise ValueError("pipeline checkpoint identity is invalid")
        if not isinstance(stages, dict) or not isinstance(outputs, dict):
            raise ValueError("pipeline checkpoint stages are invalid")
        if any(
            stage not in _STAGES or status not in {"PENDING", "RUNNING", "FAILED", "COMPLETE"}
            for stage, status in stages.items()
        ):
            raise ValueError("pipeline checkpoint contains an invalid stage")
        if set(stages) != set(_STAGES):
            raise ValueError("pipeline checkpoint must contain every stage")
        if not isinstance(current_stage, str) or current_stage not in _STAGES:
            raise ValueError("pipeline checkpoint current stage is invalid")
        if plan_hash is not None and not isinstance(plan_hash, str):
            raise ValueError("pipeline checkpoint plan hash is invalid")
        if not isinstance(errors, list) or not all(isinstance(item, str) for item in errors):
            raise ValueError("pipeline checkpoint errors are invalid")
        return cls(
            source_digest=source_digest,
            descriptor_digest=descriptor_digest,
            stages={str(key): str(value) for key, value in stages.items()},
            outputs={
                str(key): [str(item) for item in value]
                for key, value in outputs.items()
                if isinstance(value, list)
            },
            current_stage=current_stage,
            plan_hash=plan_hash,
            errors=list(errors),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_digest": self.source_digest,
            "descriptor_digest": self.descriptor_digest,
            "current_stage": self.current_stage,
            "stages": dict(self.stages),
            "outputs": {key: list(value) for key, value in self.outputs.items()},
            "plan_hash": self.plan_hash,
            "errors": list(self.errors or []),
        }


class ProjectMigrationPipeline:
    """Run the complete pre-Run drafting and post-freeze migration workflow."""

    def run(self, request: ProjectMigrationPipelineRequest) -> ProjectMigrationPipelineReport:
        source = request.source.expanduser().resolve()
        target = request.target.expanduser().resolve()
        state_dir = (
            (request.state_dir or target.parent / f".{target.name}.codemigrator")
            .expanduser()
            .resolve()
        )
        runner = ProjectMigrationRunner()

        try:
            snapshot = runner._preflight(source, target)
            descriptor = _go_analysis_descriptor()
            low_request = ProjectMigrationRequest(
                source=source,
                target=target,
                state_dir=state_dir,
                resume=request.resume,
                translator=request.translator,
                verification_runner=request.verification_runner,
                max_parallelism=request.max_parallelism,
            )
            low_state = runner._load_or_initialize(
                low_request,
                state_dir,
                snapshot.digest,
                descriptor.descriptor_sha256,
                snapshot,
                target,
            )
            low_state.state_dir = state_dir
            checkpoint = self._load_or_initialize_checkpoint(
                state_dir, request.resume, snapshot.digest, descriptor.descriptor_sha256
            )
        except (OSError, ValueError, RuntimeError):
            return self._failed(
                source_digest="",
                target=target,
                state_dir=state_dir,
                stage="PREFLIGHT",
                errors=("preflight failed",),
            )

        if request.resume and checkpoint.stages["REPORT"] == "COMPLETE" and all(
            item.status == "SUCCEEDED" for item in low_state.files
        ):
            return self._load_report_or_failed(checkpoint, state_dir, target, low_state)

        try:
            self._begin_stage(checkpoint, state_dir, "PREFLIGHT")
            if checkpoint.stages["PREFLIGHT"] != "COMPLETE":
                self._write_preflight(state_dir, snapshot)
                self._complete_stage(
                    checkpoint, state_dir, "PREFLIGHT", self._relative_outputs("PREFLIGHT")
                )

            analysis = self._navigation(
                checkpoint, state_dir, runner, low_state, snapshot, descriptor
            )
            artifacts, frozen_bundle = self._draft_alignment(
                checkpoint, state_dir, analysis, snapshot, request.translator
            )
            frozen_plan = self._planning(
                checkpoint, state_dir, analysis, artifacts, frozen_bundle, request
            )
            self._execute(
                checkpoint, state_dir, runner, low_state, snapshot, target, request, frozen_plan
            )
            self._verify_integrate(
                checkpoint, state_dir, runner, low_state, target, frozen_plan, request
            )
            report = self._report(checkpoint, state_dir, runner, low_state, target, frozen_plan)
            return report
        except (OSError, ValueError, RuntimeError):
            failed_stage = checkpoint.current_stage
            self._fail_stage(checkpoint, state_dir, failed_stage, "stage failed")
            return self._report_from_low(
                checkpoint, state_dir, target, low_state, failed_stage, ("stage failed",)
            )

    def _navigation(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        runner: ProjectMigrationRunner,
        low_state: _MigrationState,
        snapshot: _SourceSnapshot,
        descriptor: object,
    ) -> AnalysisResult:
        self._begin_stage(checkpoint, state_dir, "NAVIGATION")
        if checkpoint.stages["NAVIGATION"] == "COMPLETE":
            payload = _read_json(
                state_dir / "stages" / _STAGE_DIRS["NAVIGATION"] / "navigation-map.json"
            )
            analysis = AnalysisResult.model_validate(payload["analysis"])
            _persist_redacted_analysis(runner, low_state, analysis)
            return analysis
        source_snapshot = _analysis_snapshot(snapshot, low_state.files)
        runner._run_analysis(low_state, source_snapshot, descriptor)  # type: ignore[arg-type]
        result_payload = low_state.analysis.get("result")
        if not isinstance(result_payload, dict):
            raise ValueError("analysis did not produce a navigation map")
        analysis = AnalysisResult.model_validate(result_payload)
        redacted_analysis = _redacted_analysis_payload(analysis)
        navigation = {
            "schema_version": 1,
            "snapshot_oid": analysis.snapshot_oid,
            "analysis_sha256": analysis.canonical_sha256,
            "counts": _analysis_counts(analysis),
            "skipped_paths": list(snapshot.skipped_paths),
            "analysis": redacted_analysis,
        }
        _write_stage_json(state_dir, "NAVIGATION", "navigation-map.json", navigation)
        _write_stage_json(
            state_dir,
            "NAVIGATION",
            "navigation-summary.json",
            {"schema_version": 1, "counts": _analysis_counts(analysis)},
        )
        self._complete_stage(
            checkpoint, state_dir, "NAVIGATION", self._relative_outputs("NAVIGATION")
        )
        _persist_redacted_analysis(runner, low_state, analysis)
        return analysis

    def _draft_alignment(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        analysis: AnalysisResult,
        snapshot: _SourceSnapshot,
        translator: ProjectTranslator | None,
    ) -> tuple[DraftArtifacts, FrozenArtifactBundle]:
        self._begin_stage(checkpoint, state_dir, "DRAFT_ALIGNMENT")
        if checkpoint.stages["DRAFT_ALIGNMENT"] == "COMPLETE":
            artifacts = _read_draft_artifacts(state_dir)
            receipt_payload = _read_json(
                state_dir / "stages" / _STAGE_DIRS["DRAFT_ALIGNMENT"] / "freeze-receipt.json"
            )
            return artifacts, FrozenArtifactBundle.model_validate(
                receipt_payload["frozen_artifact_bundle"]
            )

        module_files = _module_files(analysis, snapshot.contents)
        skeleton = build_domain_skeleton(module_files, max_fanout=64)
        flow = DraftFlow(module_files=module_files, max_fanout=64)
        reports = tuple(
            ExplorationReport(
                domain_path=domain.domain_path,
                anchors=(_source_range(_first_anchor_path(domain.files, analysis)),),
                coverage=domain.files,
                confidence_reason=(
                    "mechanical analysis facts were merged without unresolved conflicts"
                ),
            )
            for domain in skeleton
        )
        for report in reports:
            flow.submit_report(report)
        merged = flow.finish_exploration(tuple(snapshot.contents))
        artifacts = _build_artifacts(analysis, snapshot, module_files)
        revision = flow.seed_artifacts(artifacts)
        alignment = _auto_align(flow, revision.revision_id)
        flow.finalize_alignment()

        # Publish the mechanical and alignment evidence before any model trial.
        # A slow or unavailable provider must not hide the completed read-only work.
        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "domain-skeleton.json",
            {
                "schema_version": 1,
                "domains": [domain.model_dump(mode="json") for domain in skeleton],
            },
        )
        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "exploration-merge.json",
            merged.model_dump(mode="json"),
        )
        _write_stage_json(state_dir, "DRAFT_ALIGNMENT", "alignment.json", alignment)
        _write_artifacts(state_dir, artifacts)
        flow.begin_calibration()
        hotspots = _hotspots(analysis)
        if translator is None:
            raise ValueError("a translator is required for calibration")
        constrained: dict[str, str] = {}
        freeform: dict[str, str] = {}
        for path in hotspots:
            source_text = snapshot.contents[path].decode("utf-8")
            constrained[path] = translator.translate(path, source_text).content
            freeform[path] = translator.translate(path, source_text).content
        trials = flow.trial_translate(hotspots, constrained, freeform)
        freeze_receipt = flow.confirm()

        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "domain-skeleton.json",
            {
                "schema_version": 1,
                "domains": [domain.model_dump(mode="json") for domain in skeleton],
            },
        )
        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "exploration-merge.json",
            merged.model_dump(mode="json"),
        )
        _write_stage_json(state_dir, "DRAFT_ALIGNMENT", "alignment.json", alignment)
        _write_artifacts(state_dir, artifacts)
        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "trial-translations.json",
            {
                "schema_version": 1,
                "discarded": True,
                "trials": [
                    {
                        "file_path": str(trial.file_path),
                        "constrained_sha256": _sha256_text(trial.constrained_output),
                        "freeform_sha256": _sha256_text(trial.freeform_output),
                        "constrained_ast": _is_python(trial.constrained_output),
                        "freeform_ast": _is_python(trial.freeform_output),
                    }
                    for trial in trials
                ],
            },
        )
        _write_stage_json(
            state_dir,
            "DRAFT_ALIGNMENT",
            "freeze-receipt.json",
            freeze_receipt.model_dump(mode="json"),
        )
        self._complete_stage(
            checkpoint, state_dir, "DRAFT_ALIGNMENT", self._relative_outputs("DRAFT_ALIGNMENT")
        )
        return artifacts, freeze_receipt.frozen_artifact_bundle

    def _planning(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        analysis: AnalysisResult,
        artifacts: DraftArtifacts,
        frozen_bundle: FrozenArtifactBundle,
        request: ProjectMigrationPipelineRequest,
    ) -> FrozenPlan:
        self._begin_stage(checkpoint, state_dir, "PLANNING")
        if checkpoint.stages["PLANNING"] == "COMPLETE":
            payload = _read_json(
                state_dir / "stages" / _STAGE_DIRS["PLANNING"] / "frozen-plan.json"
            )
            return FrozenPlan.model_validate(payload)

        planner_payload = _planner_advice(request.planner, request.translator, analysis, artifacts)
        _write_stage_json(
            state_dir,
            "PLANNING",
            "planner-request.json",
            {
                "schema_version": 1,
                "input": {
                    "snapshot_oid": analysis.snapshot_oid,
                    "analysis_counts": _analysis_counts(analysis),
                    "artifact_names": [
                        "spec",
                        "understanding_dossier",
                        "target_project_blueprint",
                        "migration_rulebook",
                    ],
                },
            },
        )
        advice_payload = planner_payload.get("advice")
        planner_advice = (
            {"schema_version": 1, **advice_payload}
            if isinstance(advice_payload, dict)
            else {
                "schema_version": 1,
                "status": planner_payload.get("status", "UNAVAILABLE"),
            }
        )
        _write_stage_json(state_dir, "PLANNING", "planner-advice.json", planner_advice)
        _write_stage_json(
            state_dir,
            "PLANNING",
            "planner-response-meta.json",
            {key: value for key, value in planner_payload.items() if key != "advice"},
        )

        spec = artifacts.spec.spec
        limits = PlanningLimits(
            max_slices=100,
            max_edges=500,
            max_write_paths_per_slice=200,
            max_total_write_paths=max(2000, len(analysis.modules) * 20),
        )
        inputs = PlanningInputs(
            frozen_artifacts=frozen_bundle,
            spec=spec,
            understanding_dossier=artifacts.understanding_dossier,
            target_project_blueprint=artifacts.target_project_blueprint,
            migration_rulebook=artifacts.migration_rulebook,
            analysis=analysis,
            snapshot_oid=analysis.snapshot_oid,
            limits=limits,
        )
        proposal = _align_proposal_to_executor(derive_plan_proposal(inputs), analysis)
        rationale = list(proposal.planner_rationale)
        advice = planner_payload.get("advice")
        if isinstance(advice, dict):
            summary = advice.get("summary")
            if isinstance(summary, str) and summary:
                rationale.append(
                    DossierEntry(
                        kind=DossierEntryKind("planner-advice"),
                        content=summary[:1024],
                        anchors=[],
                        advisory=True,
                    )
                )
        proposal = proposal.model_copy(update={"planner_rationale": rationale})
        frozen = PlanLedger().freeze(proposal, inputs)
        _write_stage_json(
            state_dir,
            "PLANNING",
            "planning-inputs.json",
            {
                "schema_version": 1,
                "snapshot_oid": analysis.snapshot_oid,
                "analysis_sha256": analysis.canonical_sha256,
                "frozen_artifact_bundle": frozen_bundle.model_dump(mode="json"),
                "limits": limits.model_dump(mode="json"),
            },
        )
        _write_stage_json(state_dir, "PLANNING", "proposal.json", proposal.model_dump(mode="json"))
        _write_stage_json(
            state_dir,
            "PLANNING",
            "validation.json",
            frozen.validation.model_dump(mode="json"),
        )
        _write_stage_json(state_dir, "PLANNING", "frozen-plan.json", frozen.model_dump(mode="json"))
        checkpoint.plan_hash = frozen.plan_hash
        self._complete_stage(checkpoint, state_dir, "PLANNING", self._relative_outputs("PLANNING"))
        return frozen

    def _execute(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        runner: ProjectMigrationRunner,
        low_state: _MigrationState,
        snapshot: _SourceSnapshot,
        target: Path,
        request: ProjectMigrationPipelineRequest,
        frozen_plan: FrozenPlan,
    ) -> None:
        self._begin_stage(checkpoint, state_dir, "EXECUTE")
        stage_was_complete = checkpoint.stages["EXECUTE"] == "COMPLETE"
        if stage_was_complete and all(item.status == "SUCCEEDED" for item in low_state.files):
            generated_package_scaffolds = _materialize_python_package_scaffolding(target, low_state)
            if generated_package_scaffolds:
                summary_path = (
                    state_dir / "stages" / _STAGE_DIRS["EXECUTE"] / "execution-summary.json"
                )
                if summary_path.exists():
                    summary = _read_json(summary_path)
                    summary["generated_package_scaffolds"] = list(generated_package_scaffolds)
                    _write_json(summary_path, summary)
            return
        low_state.plan = {
            "plan_hash": frozen_plan.plan_hash,
            "slice_count": len(frozen_plan.slices),
            "coverage": "EXACTLY_ONCE",
            "scope": "TARGET_ROOT_ONLY",
            "integration_order": [str(item) for item in frozen_plan.integration_order],
        }
        low_state.phase = _ProjectMigrationPhase.PLAN.value
        runner._write_state_from_phase(low_state)
        runner._run_execute(
            low_state,
            snapshot.contents,
            target,
            request.translator,
            request.max_parallelism,
        )
        generated_package_scaffolds = _materialize_python_package_scaffolding(target, low_state)
        _write_stage_json(
            state_dir,
            "EXECUTE",
            "file-results.json",
            {"schema_version": 1, "files": [item.as_dict() for item in low_state.files]},
        )
        _write_stage_json(
            state_dir,
            "EXECUTE",
            "slice-results.json",
            _slice_results(frozen_plan, low_state, snapshot),
        )
        _write_stage_json(
            state_dir,
            "EXECUTE",
            "execution-summary.json",
            {
                "schema_version": 1,
                "translated": sum(
                    item.kind == "translate" and item.status == "SUCCEEDED"
                    for item in low_state.files
                ),
                "copied": sum(
                    item.kind == "copy" and item.status == "SUCCEEDED" for item in low_state.files
                ),
                "failed": [item.source_path for item in low_state.files if item.status == "FAILED"],
                "generated_package_scaffolds": list(generated_package_scaffolds),
            },
        )
        if runner._failed_files(low_state):
            raise ValueError("one or more file translations failed")
        self._complete_stage(checkpoint, state_dir, "EXECUTE", self._relative_outputs("EXECUTE"))

    def _verify_integrate(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        runner: ProjectMigrationRunner,
        low_state: _MigrationState,
        target: Path,
        frozen_plan: FrozenPlan,
        request: ProjectMigrationPipelineRequest,
    ) -> None:
        self._begin_stage(checkpoint, state_dir, "VERIFY_INTEGRATE")
        if checkpoint.stages["VERIFY_INTEGRATE"] == "COMPLETE":
            return
        runner._run_verify(low_state, target, request.verification_runner)
        _write_stage_json(
            state_dir,
            "VERIFY_INTEGRATE",
            "checks.json",
            {"schema_version": 1, "checks": [dict(item) for item in low_state.checks]},
        )
        integration = _integration_manifest(frozen_plan, low_state, target)
        _write_stage_json(state_dir, "VERIFY_INTEGRATE", "integration-manifest.json", integration)
        if low_state.errors:
            raise ValueError("verification did not pass")
        self._complete_stage(
            checkpoint, state_dir, "VERIFY_INTEGRATE", self._relative_outputs("VERIFY_INTEGRATE")
        )

    def _report(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        runner: ProjectMigrationRunner,
        low_state: _MigrationState,
        target: Path,
        frozen_plan: FrozenPlan,
    ) -> ProjectMigrationPipelineReport:
        self._begin_stage(checkpoint, state_dir, "REPORT")
        if checkpoint.stages["REPORT"] == "COMPLETE":
            return self._load_report_or_failed(checkpoint, state_dir, target, low_state)
        runner._run_report(low_state, target)
        report = self._report_from_low(
            checkpoint, state_dir, target, low_state, "REPORT", tuple(low_state.errors)
        )
        payload = report.as_dict()
        if report.status == "COMPLETED":
            payload["stage"] = "COMPLETED"
            payload["completed_stages"] = list(_STAGES)
        payload["plan_hash"] = frozen_plan.plan_hash
        payload["stages"] = {
            stage: (
                "COMPLETE"
                if report.status == "COMPLETED" and stage == "REPORT"
                else checkpoint.stages[stage]
            )
            for stage in _STAGES
        }
        _write_stage_json(state_dir, "REPORT", "report.json", payload)
        _write_report_markdown(state_dir, payload)
        self._complete_stage(checkpoint, state_dir, "REPORT", self._relative_outputs("REPORT"))
        checkpoint.current_stage = "REPORT"
        self._write_checkpoint(state_dir, checkpoint)
        return ProjectMigrationPipelineReport(
            status=report.status,
            stage="COMPLETED" if report.status == "COMPLETED" else report.stage,
            source_digest=report.source_digest,
            target=report.target,
            state_dir=report.state_dir,
            stage_dir=str(state_dir / "stages"),
            plan_hash=frozen_plan.plan_hash,
            included_files=report.included_files,
            translated_files=report.translated_files,
            copied_files=report.copied_files,
            completed_stages=tuple(
                stage_name for stage_name in _STAGES if checkpoint.stages[stage_name] == "COMPLETE"
            ),
            failed_files=report.failed_files,
            skipped_paths=report.skipped_paths,
            checks=report.checks,
            errors=report.errors,
        )

    def _load_or_initialize_checkpoint(
        self, state_dir: Path, resume: bool, source_digest: str, descriptor_digest: str
    ) -> _PipelineCheckpoint:
        path = state_dir / "pipeline.json"
        if resume:
            checkpoint = _PipelineCheckpoint.from_payload(_read_json(path))
            if (
                checkpoint.source_digest != source_digest
                or checkpoint.descriptor_digest != descriptor_digest
            ):
                raise ValueError("resume identity does not match pipeline checkpoint")
            return checkpoint
        if path.exists():
            raise ValueError(
                "pipeline checkpoint already exists; use resume or choose a new state directory"
            )
        checkpoint = _PipelineCheckpoint.fresh(source_digest, descriptor_digest)
        self._write_checkpoint(state_dir, checkpoint)
        return checkpoint

    def _begin_stage(self, checkpoint: _PipelineCheckpoint, state_dir: Path, stage: str) -> None:
        checkpoint.current_stage = stage
        if checkpoint.stages[stage] == "COMPLETE":
            return
        checkpoint.stages[stage] = "RUNNING"
        self._write_checkpoint(state_dir, checkpoint)

    def _complete_stage(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        stage: str,
        outputs: Sequence[str],
    ) -> None:
        checkpoint.stages[stage] = "COMPLETE"
        checkpoint.outputs[stage] = list(outputs)
        checkpoint.current_stage = stage
        self._write_checkpoint(state_dir, checkpoint)

    def _fail_stage(
        self, checkpoint: _PipelineCheckpoint, state_dir: Path, stage: str, error: str
    ) -> None:
        checkpoint.stages[stage] = "FAILED"
        checkpoint.errors = [*(checkpoint.errors or []), error]
        self._write_checkpoint(state_dir, checkpoint)

    def _write_checkpoint(self, state_dir: Path, checkpoint: _PipelineCheckpoint) -> None:
        _write_json(state_dir / "pipeline.json", checkpoint.as_dict())

    @staticmethod
    def _relative_outputs(stage: str) -> tuple[str, ...]:
        return tuple(
            f"{_STAGE_DIRS[stage]}/{name}"
            for name in {
                "PREFLIGHT": ("source-snapshot.json", "source-files.json"),
                "NAVIGATION": ("navigation-map.json", "navigation-summary.json"),
                "DRAFT_ALIGNMENT": (
                    "domain-skeleton.json",
                    "exploration-merge.json",
                    "alignment.json",
                    "spec.json",
                    "understanding-dossier.json",
                    "target-project-blueprint.json",
                    "migration-rulebook.json",
                    "trial-translations.json",
                    "freeze-receipt.json",
                ),
                "PLANNING": (
                    "planner-request.json",
                    "planner-response-meta.json",
                    "planner-advice.json",
                    "planning-inputs.json",
                    "proposal.json",
                    "validation.json",
                    "frozen-plan.json",
                ),
                "EXECUTE": ("file-results.json", "slice-results.json", "execution-summary.json"),
                "VERIFY_INTEGRATE": ("checks.json", "integration-manifest.json"),
                "REPORT": ("report.json", "report.md"),
            }[stage]
        )

    def _write_preflight(self, state_dir: Path, snapshot: _SourceSnapshot) -> None:
        _write_stage_json(
            state_dir,
            "PREFLIGHT",
            "source-snapshot.json",
            {
                "schema_version": 1,
                "source_digest": snapshot.digest,
                "included_files": len(snapshot.contents),
                "skipped_paths": list(snapshot.skipped_paths),
            },
        )
        _write_stage_json(
            state_dir,
            "PREFLIGHT",
            "source-files.json",
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": path,
                        "sha256": _sha256_bytes(content),
                        "size": len(content),
                    }
                    for path, content in sorted(snapshot.contents.items())
                ],
            },
        )

    def _report_from_low(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        target: Path,
        low_state: _MigrationState,
        stage: str,
        errors: Sequence[str],
    ) -> ProjectMigrationPipelineReport:
        safe_errors = tuple(dict.fromkeys((*errors, *low_state.errors)))
        return ProjectMigrationPipelineReport(
            status="FAILED" if safe_errors or runner_failed(low_state) else "COMPLETED",
            stage=stage,
            source_digest=low_state.source_digest,
            target=target.name,
            state_dir=state_dir.name,
            stage_dir=str(state_dir / "stages"),
            plan_hash=checkpoint.plan_hash,
            included_files=len(low_state.files),
            translated_files=sum(item.kind == "translate" for item in low_state.files),
            copied_files=sum(item.kind == "copy" for item in low_state.files),
            completed_stages=tuple(
                stage_name for stage_name in _STAGES if checkpoint.stages[stage_name] == "COMPLETE"
            ),
            failed_files=tuple(
                item.source_path for item in low_state.files if item.status == "FAILED"
            ),
            skipped_paths=tuple(low_state.skipped_paths),
            checks=tuple(dict(item) for item in low_state.checks),
            errors=safe_errors,
        )

    def _load_report_or_failed(
        self,
        checkpoint: _PipelineCheckpoint,
        state_dir: Path,
        target: Path,
        low_state: _MigrationState,
    ) -> ProjectMigrationPipelineReport:
        path = state_dir / "stages" / _STAGE_DIRS["REPORT"] / "report.json"
        if path.exists():
            payload = _read_json(path)
            return ProjectMigrationPipelineReport(
                status=str(payload.get("status", "FAILED")),
                stage=str(payload.get("stage", "REPORT")),
                source_digest=str(payload.get("source_digest", low_state.source_digest)),
                target=str(payload.get("target", target.name)),
                state_dir=str(payload.get("state_dir", state_dir.name)),
                stage_dir=str(payload.get("stage_dir", state_dir / "stages")),
                plan_hash=_optional_string(payload.get("plan_hash")) or checkpoint.plan_hash,
                included_files=_int_value(payload.get("included_files"), len(low_state.files)),
                translated_files=_int_value(payload.get("translated_files"), 0),
                copied_files=_int_value(payload.get("copied_files"), 0),
                completed_stages=_string_tuple(payload.get("completed_stages", _STAGES)),
                failed_files=_string_tuple(payload.get("failed_files", [])),
                skipped_paths=_string_tuple(payload.get("skipped_paths", [])),
                checks=_dict_tuple(payload.get("checks", [])),
                errors=_string_tuple(payload.get("errors", [])),
            )
        return self._report_from_low(
            checkpoint, state_dir, target, low_state, "REPORT", ("report missing",)
        )

    @staticmethod
    def _failed(
        source_digest: str,
        target: Path,
        state_dir: Path,
        stage: str,
        errors: Sequence[str],
    ) -> ProjectMigrationPipelineReport:
        return ProjectMigrationPipelineReport(
            status="FAILED",
            stage=stage,
            source_digest=source_digest,
            target=target.name,
            state_dir=state_dir.name,
            stage_dir=str(state_dir / "stages"),
            plan_hash=None,
            included_files=0,
            translated_files=0,
            copied_files=0,
            errors=tuple(errors),
        )


def runner_failed(state: _MigrationState) -> bool:
    return any(item.status == "FAILED" for item in state.files)


def _redacted_analysis_payload(analysis: AnalysisResult) -> dict[str, object]:
    payload = analysis.model_dump(mode="json")
    for module in payload.get("modules", []):
        if isinstance(module, dict):
            for symbol in module.get("exported_symbols", []):
                if isinstance(symbol, dict) and "signature_text" in symbol:
                    symbol["signature_text"] = "<signature-redacted>"
    for binding in payload.get("symbol_bindings", []):
        if isinstance(binding, dict) and "signature_text" in binding:
            binding["signature_text"] = "<signature-redacted>"
    return payload


def _persist_redacted_analysis(
    runner: ProjectMigrationRunner, state: _MigrationState, analysis: AnalysisResult
) -> None:
    persisted = dict(state.analysis)
    persisted["result"] = _redacted_analysis_payload(analysis)
    state.analysis = persisted
    runner._write_state_from_phase(state)


def _materialize_python_package_scaffolding(
    target: Path, state: _MigrationState
) -> tuple[str, ...]:
    """Create lazy package exports for translated Python module directories.

    Go packages do not require an explicit package entry file, while Python
    projects commonly use one to expose public symbols from sibling modules.
    This deterministic integration step is deliberately separate from model
    translation: it only inspects already-generated Python ASTs and writes
    minimal lazy ``__init__.py`` adapters.  Existing files are never replaced.
    """

    package_dirs: set[str] = set()
    direct_modules: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for item in state.files:
        if item.kind != "translate" or item.status != "SUCCEEDED":
            continue
        target_path = item.target_path
        if not target_path.endswith(".py") or "/" not in target_path:
            continue
        parts = target_path.split("/")
        parent_parts = parts[:-1]
        for index in range(1, len(parent_parts) + 1):
            package_dirs.add("/".join(parent_parts[:index]))
        parent = "/".join(parent_parts)
        direct_modules.setdefault(parent, []).append((target_path, parts[-1][:-3]))

    if not package_dirs:
        return ()

    generated: list[str] = []
    root = SecureRoot("package-scaffold", target)
    try:
        for package_dir in sorted(package_dirs, key=lambda value: value.encode("utf-8")):
            init_path = f"{package_dir}/__init__.py"
            if root.exists(init_path):
                continue
            exports: dict[str, str] = {}
            for target_path, module_name in sorted(
                direct_modules.get(package_dir, ()), key=lambda value: value[0].encode("utf-8")
            ):
                if module_name == "__init__":
                    continue
                try:
                    module = ast.parse(
                        root.read_bytes(target_path).decode("utf-8"), filename=target_path
                    )
                except (OSError, UnicodeError, SyntaxError):
                    continue
                for node in module.body:
                    if isinstance(
                        node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)
                    ) and not node.name.startswith("_"):
                        exports.setdefault(node.name, module_name)
            payload = json.dumps(exports, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            content = (
                '"""Package exports generated by CodeMigrator."""\n'
                "from importlib import import_module as _import_module\n\n"
                f"_EXPORTS = {payload}\n\n"
                "def __getattr__(name: str):\n"
                "    module_name = _EXPORTS.get(name)\n"
                "    if module_name is None:\n"
                '        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")\n'
                '    module = _import_module(f"{__name__}.{module_name}")\n'
                "    value = getattr(module, name)\n"
                "    globals()[name] = value\n"
                "    return value\n\n"
                "__all__ = tuple(sorted(_EXPORTS))\n"
            )
            root.write_atomic(init_path, content.encode("utf-8"))
            generated.append(init_path)
    finally:
        root.close()
    return tuple(generated)


def repair_generated_file(
    source: Path,
    target: Path,
    source_path: str,
    translator: RepairingProjectTranslator,
    verification_feedback: str,
) -> TranslationResult:
    """Regenerate one target file from source evidence and a local failure.

    The source and target are both read through ``SecureRoot`` and the repaired
    result is passed through the normal translation validation gate before the
    atomic target write.  This keeps a verification repair inside CodeMigrator
    instead of turning it into an untracked hand edit.
    """

    expected_target = _target_path(source_path)
    source_root = SecureRoot("repair-source", source)
    target_root = SecureRoot("repair-target", target)
    try:
        source_text = source_root.read_bytes(source_path).decode("utf-8")
        target_text = target_root.read_bytes(expected_target).decode("utf-8")
        result = translator.repair(
            source_path,
            source_text,
            target_text,
            verification_feedback,
        )
        content = _validated_translation(result, expected_target)
        target_root.write_atomic(expected_target, content.encode("utf-8"))
        return TranslationResult(content=content, target_path=result.target_path)
    finally:
        target_root.close()
        source_root.close()


def record_generated_repairs(state_dir: Path, repairs: Sequence[Mapping[str, object]]) -> None:
    """Persist redacted file-repair facts in the verification stage ledger."""

    normalized = [
        {
            key: value
            for key, value in record.items()
            if key
            in {
                "source_path",
                "target_path",
                "status",
                "method",
                "source_sha256",
                "target_sha256",
                "feedback_sha256",
                "repair_count",
            }
        }
        for record in repairs
    ]
    _write_stage_json(
        state_dir,
        "VERIFY_INTEGRATE",
        "repairs.json",
        {"schema_version": 1, "repairs": normalized},
    )
    checkpoint_path = state_dir / "pipeline.json"
    if checkpoint_path.exists():
        checkpoint = _PipelineCheckpoint.from_payload(_read_json(checkpoint_path))
        outputs = checkpoint.outputs.setdefault("VERIFY_INTEGRATE", [])
        output = f"{_STAGE_DIRS['VERIFY_INTEGRATE']}/repairs.json"
        if output not in outputs:
            outputs.append(output)
            outputs.sort()
            _write_json(checkpoint_path, checkpoint.as_dict())


def adopt_repaired_file(state_dir: Path, target: Path, target_path: str) -> None:
    """Adopt one validated repair and reopen only verification/report stages."""

    state_payload = _read_json(state_dir / "state.json")
    state = _MigrationState.from_dict(state_payload)
    item = next((entry for entry in state.files if entry.target_path == target_path), None)
    if item is None or item.kind != "translate":
        raise ValueError("repaired target path is not a translated checkpoint file")
    root = SecureRoot("repair-adoption", target)
    try:
        content = root.read_bytes(target_path)
    finally:
        root.close()
    try:
        ast.parse(content.decode("utf-8"), filename=target_path)
    except (UnicodeError, SyntaxError) as exc:
        raise ValueError("repaired target is not valid Python") from exc
    item.status = "SUCCEEDED"
    item.target_sha256 = _sha256_bytes(content)
    item.error = None
    _write_json(state_dir / "state.json", state.as_dict())

    checkpoint_path = state_dir / "pipeline.json"
    checkpoint = _PipelineCheckpoint.from_payload(_read_json(checkpoint_path))
    if (
        checkpoint.stages["VERIFY_INTEGRATE"] != "PENDING"
        or checkpoint.stages["REPORT"] != "PENDING"
    ):
        checkpoint.stages["VERIFY_INTEGRATE"] = "PENDING"
        checkpoint.stages["REPORT"] = "PENDING"
        checkpoint.current_stage = "VERIFY_INTEGRATE"
        checkpoint.errors = []
        _write_json(checkpoint_path, checkpoint.as_dict())


def redact_persisted_analysis(state_dir: Path) -> None:
    """Remove source-bearing analysis signatures from existing local evidence."""

    state_path = state_dir / "state.json"
    state_payload = _read_json(state_path)
    raw_state_analysis = state_payload.get("analysis")
    if isinstance(raw_state_analysis, dict) and isinstance(raw_state_analysis.get("result"), dict):
        analysis = AnalysisResult.model_validate(raw_state_analysis["result"])
        raw_state_analysis["result"] = _redacted_analysis_payload(analysis)
        state_payload["analysis"] = raw_state_analysis
        _write_json(state_path, state_payload)

    navigation_path = state_dir / "stages" / _STAGE_DIRS["NAVIGATION"] / "navigation-map.json"
    if navigation_path.exists():
        navigation_payload = _read_json(navigation_path)
        raw_navigation_analysis = navigation_payload.get("analysis")
        if isinstance(raw_navigation_analysis, dict):
            analysis = AnalysisResult.model_validate(raw_navigation_analysis)
            navigation_payload["analysis"] = _redacted_analysis_payload(analysis)
            _write_json(navigation_path, navigation_payload)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int_value(value: object, default: int) -> int:
    return value if type(value) is int else default


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _dict_tuple(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _analysis_counts(analysis: AnalysisResult) -> dict[str, int | str]:
    return {
        "capability": analysis.capability.value,
        "modules": len(analysis.modules),
        "source_modules": sum(module.role is ModuleRole.Source for module in analysis.modules),
        "test_modules": sum(module.role is ModuleRole.Test for module in analysis.modules),
        "imports": len(analysis.imports),
        "coverage": len(analysis.coverage),
        "artifacts": len(analysis.artifacts),
        "references": len(analysis.reference_sites),
        "call_edges": len(analysis.call_edges),
        "relation_edges": len(analysis.relation_edges),
        "errors": len(analysis.errors),
    }


def _module_files(
    analysis: AnalysisResult, source_files: Mapping[str, bytes]
) -> dict[str, list[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    seen: set[str] = set()
    for module in analysis.modules:
        paths = [str(path) for path in module.file_paths]
        for path in paths:
            grouped[path.rsplit("/", 1)[0] if "/" in path else "."].add(path)
            seen.add(path)
    for path in source_files:
        if path not in seen:
            grouped[path.split("/", 1)[0] if "/" in path else "."].add(path)
    return {
        domain: sorted(paths, key=lambda item: item.encode("utf-8"))
        for domain, paths in sorted(grouped.items(), key=lambda item: item[0].encode("utf-8"))
    }


def _first_anchor_path(paths: Sequence[str], analysis: AnalysisResult) -> str:
    known = {str(path) for module in analysis.modules for path in module.file_paths}
    for path in paths:
        if path in known:
            return path
    return paths[0]


def _anchor(path: str) -> dict[str, object]:
    return {"file": path, "start_line": 1, "end_line": 1}


def _source_range(path: str) -> SourceRange:
    return SourceRange(
        file_path=RepoRelativePath(path),
        start=SourcePosition(line=1, column=0),
        end=SourcePosition(line=1, column=0),
    )


def _hotspots(analysis: AnalysisResult) -> tuple[str, ...]:
    paths = sorted(
        {
            str(path)
            for module in analysis.modules
            if module.role is ModuleRole.Source
            for path in module.file_paths
        },
        key=lambda item: item.encode("utf-8"),
    )
    if len(paths) < 2:
        raise ValueError("at least two source files are required for calibration")
    return tuple(paths[:2])


def _auto_align(flow: DraftFlow, revision_id: object) -> dict[str, object]:
    questions = (
        (
            "scope-boundary",
            (
                "Should the frontend and generated cache directories remain outside "
                "this backend migration?"
            ),
            "keep-excluded",
            "Keep the explicitly excluded frontend/cache boundary.",
        ),
        (
            "external-services",
            "How should unavailable external services be represented in the Python target?",
            "injectable-adapters",
            "Keep explicit injectable adapters and report services as unexecuted.",
        ),
        (
            "test-strategy",
            "Which verification policy should govern generated target code?",
            "oracle-first",
            "Use deterministic compile checks and the declared sandbox test boundary.",
        ),
    )
    answers: list[dict[str, object]] = []
    for key, prompt, option_key, reason in questions:
        question = AskUserQuestion(
            revision_id=revision_id,  # type: ignore[arg-type]
            prompt=prompt,
            options=(
                QuestionOption(key=option_key, label=reason, impact=reason, recommended=True),
                QuestionOption(
                    key=f"defer-{key}",
                    label="Defer decision",
                    impact="Keep the stage pending until a user supplies the missing decision.",
                    recommended=False,
                ),
            ),
        )
        flow.ask_user(question)
        answer = AskUserAnswer(
            question_id=question.question_id,
            revision_id=question.revision_id,
            selected_option=option_key,
        )
        flow.answer_user(answer)
        answers.append(
            {
                "question_id": str(question.question_id),
                "key": key,
                "selected_option": option_key,
                "auto_aligned": True,
                "reason": reason,
            }
        )
    return {"schema_version": 1, "auto_aligned": True, "answers": answers}


def _build_artifacts(
    analysis: AnalysisResult, snapshot: _SourceSnapshot, module_files: Mapping[str, Sequence[str]]
) -> DraftArtifacts:
    descriptor_root = Path(__file__).resolve().parents[3] / "descriptors"
    source_bytes = (descriptor_root / "source/go/descriptor.json").read_bytes()
    target_bytes = (descriptor_root / "target/python/descriptor.json").read_bytes()
    source_digest = _sha256_bytes(source_bytes)
    target_digest = _sha256_bytes(target_bytes)
    image_digest = _descriptor_image_digest(target_bytes)
    compile_hash = _command_hash(
        CheckAction.Compile, "python", ("-m", "compileall", "-q", "."), 300
    )
    test_hash = _command_hash(CheckAction.Test, "uv", ("run", "pytest", "-q"), 120)
    spec_payload = {
        "schema": "codemigrator.migration-spec",
        "version": 3,
        "name": "click-video-go-to-python",
        "description": "Migrate the click-video backend through the CodeMigrator V6 pipeline.",
        "source_language_id": "go",
        "target_language_id": "python",
        "descriptor_lock": {
            "descriptor_version": "1.0.0",
            "source_descriptor_sha256": source_digest,
            "target_descriptor_sha256": target_digest,
            "toolchain_image_digest": image_digest,
        },
        "scope": {
            "include": _scope_includes(snapshot),
            "exclude": ["frontend/"]
            if any(path.startswith("frontend/") for path in snapshot.skipped_paths)
            else [],
        },
        "required_checks": [
            {"action": "COMPILE", "template_sha256": compile_hash},
            {"action": "TEST", "template_sha256": test_hash},
        ],
        "decomposition": {
            "module_granularity": "source-module",
            "max_parallelism": 4,
            "test_grouping": "by-module",
        },
    }
    registry = InMemoryDescriptorRegistry(
        {
            ("go", "python"): DescriptorResolution(
                source_language_id="go",
                target_language_id="python",
                descriptor_version="1.0.0",
                source_descriptor_sha256=source_digest,
                target_descriptor_sha256=target_digest,
                toolchain_image_digest=image_digest,
                checks=(
                    RequiredCheckSelection(
                        action=CheckAction.Compile, template_sha256=compile_hash
                    ),
                    RequiredCheckSelection(action=CheckAction.Test, template_sha256=test_hash),
                ),
                grammar_available=True,
                image_available=True,
            )
        }
    )
    spec_result = validate_spec_bytes(
        json.dumps(spec_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        registry=registry,
    )
    if not spec_result.accepted:
        raise ValueError("generated migration spec did not pass its gates")
    spec_artifact = SpecArtifact.from_result(spec_result)
    fact_paths = [str(path) for module in analysis.modules for path in module.file_paths]
    fact_path_set = set(fact_paths)
    domain_entries = [
        DossierEntry(
            kind=DossierEntryKind("semantic-module"),
            content=f"Domain {domain} contains {len(paths)} mechanically indexed source file(s).",
            anchors=[_anchor(_first_anchor_path(paths, analysis))],
            advisory=False,
        )
        for domain, paths in module_files.items()
        if any(path in fact_path_set for path in paths)
    ]
    first_fact = sorted(fact_path_set, key=lambda item: item.encode("utf-8"))[0]
    architecture = DossierEntry(
        kind=DossierEntryKind("architecture"),
        content=(
            f"The frozen Go snapshot contains {len(analysis.modules)} indexed modules, "
            f"{len(analysis.imports)} import facts and {len(analysis.coverage)} coverage facts."
        ),
        anchors=[_anchor(first_fact)],
        advisory=False,
    )
    hotspot_entries = [
        DossierEntry(
            kind=DossierEntryKind("risk-hotspot"),
            content="Source module selected for calibration and conservative translation review.",
            anchors=[_anchor(path)],
            advisory=False,
        )
        for path in _hotspots(analysis)
    ]
    test_entries = [
        DossierEntry(
            kind=DossierEntryKind("test-map"),
            content="Test module is assigned to the declared translation or generation track.",
            anchors=[_anchor(str(path))],
            advisory=False,
        )
        for module in analysis.modules
        if module.role is ModuleRole.Test
        for path in module.file_paths[:1]
    ]
    dossier = UnderstandingDossier(
        architecture_narrative=[architecture],
        semantic_modules=domain_entries,
        dependency_resolutions=[],
        test_map=test_entries,
        risk_hotspots=hotspot_entries,
        strategy_advice=[],
        coverage_self_report={
            "module_count": len(analysis.modules),
            "import_count": len(analysis.imports),
            "coverage_count": len(analysis.coverage),
            "conservation_status": "FACTS_ONLY",
        },
        budget_tier=DossierBudgetTier.Deep,
    )
    consistency = check_dossier_consistency(dossier, spec_artifact, fact_paths, 0)
    if not consistency.valid:
        raise ValueError("generated understanding dossier is inconsistent")
    blueprint = TargetProjectBlueprint(
        module_boundaries=[
            {
                "name": domain,
                "source_paths": list(paths),
                "target_path_policy": "preserve-relative-file-name",
            }
            for domain, paths in module_files.items()
        ],
        granularity_principles=["one migration slice per indexed source module"],
        target_layout_principles=["preserve source-relative target file names in the managed root"],
        parallelism_rules=["independent file translations may run up to the configured limit"],
        generated_artifact_policy=(
            "generated tests remain explicitly marked and externally verified"
        ),
        version=1,
    )
    rulebook = MigrationRulebook(
        entries=[
            RulebookEntry(
                kind=RulebookEntryKind("language-mapping"),
                content=(
                    "Translate Go source files to Python modules while preserving "
                    "public names where possible."
                ),
                source=RuleEntrySource("alignment"),
                rationale_ref=None,
                advisory=False,
            ),
            RulebookEntry(
                kind=RulebookEntryKind("resource-handling"),
                content=(
                    "Copy resources and convert the Go manifest to the target metadata boundary."
                ),
                source=RuleEntrySource("descriptor"),
                rationale_ref=None,
                advisory=False,
            ),
            RulebookEntry(
                kind=RulebookEntryKind("test-handling"),
                content=(
                    "Translate existing tests and generate missing tests only from "
                    "indexed module facts."
                ),
                source=RuleEntrySource("alignment"),
                rationale_ref=None,
                advisory=False,
            ),
            RulebookEntry(
                kind=RulebookEntryKind("external-service-boundary"),
                content=(
                    "Represent unavailable infrastructure with injectable adapters; "
                    "do not invent credentials or calls."
                ),
                source=RuleEntrySource("alignment"),
                rationale_ref=None,
                advisory=False,
            ),
        ],
        version=1,
    )
    return DraftArtifacts(
        spec=spec_artifact,
        understanding_dossier=dossier,
        target_project_blueprint=blueprint,
        migration_rulebook=rulebook,
    )


def _scope_includes(snapshot: _SourceSnapshot) -> list[str]:
    patterns: set[str] = set()
    for path in snapshot.contents:
        if "/" in path:
            patterns.add(path.split("/", 1)[0] + "/")
        else:
            patterns.add(path)
    if any(path.startswith("frontend/") for path in snapshot.skipped_paths):
        patterns.add("frontend/")
    return sorted(patterns, key=lambda item: item.encode("utf-8"))


def _descriptor_image_digest(content: bytes) -> str:
    payload = json.loads(content.decode("utf-8"))
    value = payload.get("toolchain_image_digest") if isinstance(payload, dict) else None
    if not isinstance(value, str):
        raise ValueError("target descriptor image digest is missing")
    return value.removeprefix("sha256:")


def _command_hash(action: CheckAction, program: str, argv: Sequence[str], timeout: int) -> str:
    return _sha256_bytes(
        canonical_json_bytes(
            {
                "action": action.value,
                "program": program,
                "argv": list(argv),
                "timeout_secs": timeout,
            }
        )
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


def _is_python(content: str) -> bool:
    try:
        ast.parse(content)
    except SyntaxError:
        return False
    return True


def _write_artifacts(state_dir: Path, artifacts: DraftArtifacts) -> None:
    stage = "DRAFT_ALIGNMENT"
    spec = artifacts.spec
    _write_stage_json(
        state_dir,
        stage,
        "spec.json",
        {
            "schema_version": 1,
            "spec": spec.spec.model_dump(mode="json", by_alias=True),
            "canonical_sha256": spec.canonical_sha256,
            "canonical_bytes_base64": base64.b64encode(spec.canonical_bytes).decode("ascii"),
        },
    )
    for filename, value in (
        ("understanding-dossier.json", artifacts.understanding_dossier),
        ("target-project-blueprint.json", artifacts.target_project_blueprint),
        ("migration-rulebook.json", artifacts.migration_rulebook),
    ):
        _write_stage_json(state_dir, stage, filename, value.model_dump(mode="json"))


def _read_draft_artifacts(state_dir: Path) -> DraftArtifacts:
    stage_dir = state_dir / "stages" / _STAGE_DIRS["DRAFT_ALIGNMENT"]
    spec_payload = _read_json(stage_dir / "spec.json")
    raw_bytes = base64.b64decode(str(spec_payload["canonical_bytes_base64"]))
    spec = SpecArtifact(
        spec=MigrationSpec.model_validate(spec_payload["spec"]),
        canonical_bytes=raw_bytes,
        canonical_sha256=str(spec_payload["canonical_sha256"]),
    )
    return DraftArtifacts(
        spec=spec,
        understanding_dossier=UnderstandingDossier.model_validate(
            _read_json(stage_dir / "understanding-dossier.json")
        ),
        target_project_blueprint=TargetProjectBlueprint.model_validate(
            _read_json(stage_dir / "target-project-blueprint.json")
        ),
        migration_rulebook=MigrationRulebook.model_validate(
            _read_json(stage_dir / "migration-rulebook.json")
        ),
    )


def _align_proposal_to_executor(proposal: PlanProposal, analysis: AnalysisResult) -> PlanProposal:
    modules = {module.module_id: module for module in analysis.modules}
    updated: list[PlanSliceProposal] = []
    for slice_proposal in proposal.slices:
        if slice_proposal.source_modules and slice_proposal.kind.value in {
            "IMPLEMENTATION",
            "TEST_TRANSLATION",
        }:
            source_paths = [
                str(path)
                for module_id in slice_proposal.source_modules
                for path in modules[module_id].file_paths
            ]
            physical_paths = tuple(_target_path(path) for path in sorted(source_paths))
            roots: tuple[str, ...] = ()
            tasks = tuple(
                task.model_copy(update={"target_path": _target_path(str(task.source_path))})
                for task in slice_proposal.artifact_tasks
            )
            updated.append(
                slice_proposal.model_copy(
                    update={
                        "write_paths": physical_paths,
                        "create_roots": roots,
                        "artifact_tasks": tasks,
                    }
                )
            )
        else:
            updated.append(
                slice_proposal.model_copy(
                    update={
                        "write_paths": tuple(
                            _physical_plan_path(str(path)) for path in slice_proposal.write_paths
                        ),
                        "create_roots": tuple(
                            _physical_plan_path(str(path)) for path in slice_proposal.create_roots
                        ),
                    }
                )
            )
    return proposal.model_copy(update={"slices": updated})


def _physical_plan_path(path: str) -> str:
    physical = path.removeprefix("target/")
    if physical.startswith("tests/generated/") and not physical.endswith(".py"):
        return physical + ".py"
    return physical


def _slice_results(
    frozen_plan: FrozenPlan, state: _MigrationState, snapshot: _SourceSnapshot
) -> dict[str, object]:
    files = {item.source_path: item for item in state.files}
    modules = {module.module_id: module for module in _analysis_from_state(state).modules}
    results: list[dict[str, object]] = []
    for slice_ in frozen_plan.slices:
        source_paths = [
            str(path)
            for module_id in slice_.source_modules
            for path in modules[module_id].file_paths
        ]
        owned = [files[path] for path in source_paths if path in files]
        results.append(
            {
                "slice_id": str(slice_.id),
                "kind": slice_.kind.value,
                "integration_rank": slice_.integration_rank,
                "source_paths": source_paths,
                "status": "SUCCEEDED"
                if owned and all(item.status == "SUCCEEDED" for item in owned)
                else "PLANNED",
                "target_paths": [item.target_path for item in owned],
            }
        )
    return {"schema_version": 1, "slices": results}


def _analysis_from_state(state: _MigrationState) -> AnalysisResult:
    payload = state.analysis.get("result")
    if not isinstance(payload, dict):
        raise ValueError("analysis facts are missing from migration checkpoint")
    return AnalysisResult.model_validate(payload)


def _integration_manifest(
    frozen_plan: FrozenPlan, state: _MigrationState, target: Path
) -> dict[str, object]:
    hashes: dict[str, str] = {}
    for item in state.files:
        if item.target_sha256 is not None:
            hashes[item.target_path] = item.target_sha256
    return {
        "schema_version": 1,
        "plan_hash": frozen_plan.plan_hash,
        "target": target.name,
        "order": [
            {
                "rank": slice_.integration_rank,
                "slice_id": str(slice_.id),
                "kind": slice_.kind.value,
                "target_hashes": {
                    path: hashes[path]
                    for path in slice_.write_scope.out.write_paths
                    if str(path) in hashes
                },
            }
            for slice_ in sorted(frozen_plan.slices, key=lambda item: item.integration_rank)
        ],
        "external_services": "not executed; adapters remain explicit boundaries",
    }


def _planner_advice(
    planner: PlannerAdvisor | None,
    translator: ProjectTranslator | None,
    analysis: AnalysisResult,
    artifacts: DraftArtifacts,
) -> dict[str, object]:
    if planner is not None:
        try:
            advice = dict(planner.advise(analysis, artifacts))
            return {
                "status": "AVAILABLE",
                "advice_sha256": _sha256_text(
                    json.dumps(advice, sort_keys=True, ensure_ascii=False)
                ),
                "advice": _sanitize_advice(advice),
            }
        except (OSError, RuntimeError, ValueError):
            return {"status": "UNAVAILABLE", "reason": "planner advisory failed"}
    if all(hasattr(translator, name) for name in ("_endpoint", "_api_key", "_binding")):
        try:
            advisor = _OpenAIPlannerAdvisor(
                endpoint=str(getattr(translator, "_endpoint")),
                api_key=str(getattr(translator, "_api_key")),
                model=str(getattr(getattr(translator, "_binding"), "model_id")),
            )
            return advisor.advise_with_meta(analysis, artifacts)
        except (OSError, RuntimeError, ValueError):
            return {"status": "UNAVAILABLE", "reason": "planner advisory failed"}
    return {"status": "UNAVAILABLE", "reason": "no planner advisory port"}


def _sanitize_advice(advice: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in ("summary", "priority_paths", "risk_notes"):
        value = advice.get(key)
        if isinstance(value, str):
            result[key] = value[:2048]
        elif isinstance(value, list):
            result[key] = [str(item)[:256] for item in value[:32]]
    return result


class _OpenAIPlannerAdvisor:
    def __init__(self, *, endpoint: str, api_key: str, model: str) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._binding = LockedModelBinding(
            provider_id="openai-compatible",
            model_id=model,
            profile=ModelProfile.Reasoning,
            config_revision=_sha256_text(f"{endpoint}\0{model}"),
            context_window=128_000,
            output_cap=4096,
        )

    def advise(self, analysis: AnalysisResult, artifacts: DraftArtifacts) -> Mapping[str, object]:
        return self.advise_with_meta(analysis, artifacts).get("advice", {})  # type: ignore[return-value]

    def advise_with_meta(
        self, analysis: AnalysisResult, artifacts: DraftArtifacts
    ) -> dict[str, object]:
        del artifacts
        context = {
            "modules": len(analysis.modules),
            "imports": len(analysis.imports),
            "coverage": len(analysis.coverage),
            "source_paths": sorted(
                str(path) for module in analysis.modules for path in module.file_paths
            )[:256],
        }
        prompt = (
            "Return JSON only with keys summary, priority_paths, risk_notes. "
            "Give bounded migration planning advice from these mechanical facts. "
            "Do not include source code, credentials, prompts, or invented verification results.\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True)
        )
        response = asyncio.run(self._complete(prompt))
        parsed = _parse_json_object(response.content)
        advice = _sanitize_advice(parsed)
        return {
            "status": "AVAILABLE",
            "advice_sha256": _sha256_text(response.content),
            "provider_receipt_id": response.provider_receipt_id,
            "finish_reason": response.finish_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "advice": advice,
        }

    async def _complete(self, prompt: str) -> ProviderResponse:
        provider = OpenAICompatibleProvider(
            endpoint=self._endpoint,
            api_key=self._api_key,
            client=httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)),
        )
        try:
            return await provider.complete(
                ProviderRequest(
                    binding=self._binding,
                    tools=(),
                    messages=(
                        PromptMessage(
                            role="system", content="You are a migration planner. Output JSON only."
                        ),
                        PromptMessage(role="user", content=prompt),
                    ),
                )
            )
        finally:
            await provider.aclose()


def _parse_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("planner response must be a JSON object")
    return value


def _write_stage_json(
    state_dir: Path, stage: str, filename: str, payload: Mapping[str, object]
) -> None:
    _write_json(state_dir / "stages" / _STAGE_DIRS[stage] / filename, payload)


def _write_report_markdown(state_dir: Path, payload: Mapping[str, object]) -> None:
    lines = [
        "# CodeMigrator V6 migration report",
        "",
        f"- Status: `{payload.get('status', 'UNKNOWN')}`",
        f"- Stage: `{payload.get('stage', 'UNKNOWN')}`",
        f"- Included files: `{payload.get('included_files', 0)}`",
        f"- Plan hash: `{payload.get('plan_hash') or 'none'}`",
        "",
        (
            "This report contains migration facts only. External services were not "
            "executed unless a declared verification port supplied evidence."
        ),
    ]
    path = state_dir / "stages" / _STAGE_DIRS["REPORT"] / "report.md"
    with SecureRoot("report", path.parent) as root:
        root.write_atomic(path.name, ("\n".join(lines) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stage artifact must contain an object")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    _atomic_write_json(path, payload)


__all__ = [
    "adopt_repaired_file",
    "PlannerAdvisor",
    "ProjectMigrationPipeline",
    "ProjectMigrationPipelineReport",
    "ProjectMigrationPipelineRequest",
    "redact_persisted_analysis",
    "record_generated_repairs",
    "repair_generated_file",
]
