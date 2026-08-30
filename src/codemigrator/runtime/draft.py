"""In-memory drafting orchestration and its append-only revision ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import cast

from codemigrator.core import (
    ArtifactRef,
    FrozenArtifactBundle,
    QuestionId,
    RepoRelativePath,
    Sha256,
    TaskDraftRevisionId,
    canonical_json_bytes,
    new_uuid7,
)

from .draft_models import (
    ArtifactSnapshot,
    AskUserAnswer,
    AskUserQuestion,
    DraftArtifactName,
    DraftArtifacts,
    DraftFreezeReceipt,
    DraftStage,
    ExplorationReport,
    ExploreReassignment,
    ReadOnlyDraftTool,
    TaskDraftRevision,
    TrialTranslation,
)
from .draft_validation import validate_exact_coverage


class DraftConflictError(ValueError):
    """Raised when a draft operation targets stale or conflicting state."""


class DraftLedger:
    """Store draft revisions and AskUser records without creating Run state."""

    def __init__(self) -> None:
        self._revisions: dict[TaskDraftRevisionId, TaskDraftRevision] = {}
        self._current_revision_id: TaskDraftRevisionId | None = None
        self._questions: dict[QuestionId, AskUserQuestion] = {}
        self._answers: dict[QuestionId, AskUserAnswer] = {}
        self._freeze_receipt: DraftFreezeReceipt | None = None

    @property
    def current_revision(self) -> TaskDraftRevision | None:
        if self._current_revision_id is None:
            return None
        return self._revisions[self._current_revision_id]

    @property
    def questions(self) -> tuple[AskUserQuestion, ...]:
        return tuple(
            sorted(self._questions.values(), key=lambda question: str(question.question_id))
        )

    @property
    def answers(self) -> tuple[AskUserAnswer, ...]:
        return tuple(sorted(self._answers.values(), key=lambda answer: str(answer.question_id)))

    def create_revision(self, artifacts: DraftArtifacts) -> TaskDraftRevision:
        """Create the first revision, or revise the current one when already initialized."""

        if self.current_revision is None:
            return self._new_revision(artifacts, revision_number=1)
        return self.revise(self.current_revision.revision_id, artifacts)

    def revise(
        self, revision_id: TaskDraftRevisionId, artifacts: DraftArtifacts
    ) -> TaskDraftRevision:
        current = self._require_current_revision(revision_id)
        if self._freeze_receipt is not None:
            raise DraftConflictError("draft is already frozen")
        snapshots = _artifact_snapshots(artifacts, current.revision_number)
        if snapshots == current.artifact_snapshots:
            return current
        return self._new_revision(artifacts, revision_number=current.revision_number + 1)

    def append_question(self, question: AskUserQuestion) -> AskUserQuestion:
        self._require_current_revision(question.revision_id)
        if question.question_id in self._questions:
            raise DraftConflictError("question id already exists")
        self._questions[question.question_id] = question
        return question

    def answer_question(self, answer: AskUserAnswer) -> AskUserAnswer:
        self._require_current_revision(answer.revision_id)
        question = self._questions.get(answer.question_id)
        if question is None:
            raise DraftConflictError("answer references an unknown question")
        if question.revision_id != answer.revision_id:
            raise DraftConflictError("answer is bound to a different revision")
        if answer.selected_option is not None:
            allowed = {option.key for option in question.options}
            if answer.selected_option not in allowed:
                raise DraftConflictError("answer selects an unknown option")
        elif not question.allow_free_text:
            raise DraftConflictError("free-text answer is not allowed")

        previous = self._answers.get(answer.question_id)
        if previous is not None:
            if _same_answer(previous, answer):
                return previous
            raise DraftConflictError("answer conflicts with the existing answer")
        self._answers[answer.question_id] = answer
        return answer

    def freeze(
        self,
        revision_id: TaskDraftRevisionId,
        answer_question_ids: Sequence[QuestionId] | None = None,
    ) -> DraftFreezeReceipt:
        revision = self._require_current_revision(revision_id)
        if self._freeze_receipt is not None:
            if self._freeze_receipt.revision_id == revision_id:
                return self._freeze_receipt
            raise DraftConflictError("only the current revision can be frozen")

        current_questions = [
            question for question in self._questions.values() if question.revision_id == revision_id
        ]
        expected_ids = {question.question_id for question in current_questions}
        selected_ids = (
            list(expected_ids) if answer_question_ids is None else list(answer_question_ids)
        )
        if len(selected_ids) != len(set(selected_ids)):
            raise DraftConflictError("freeze answer pointers must be unique")
        if set(selected_ids) != expected_ids:
            raise DraftConflictError("freeze must include every current question")
        unanswered = [
            question_id for question_id in selected_ids if question_id not in self._answers
        ]
        if unanswered:
            raise DraftConflictError("cannot freeze with unanswered questions")
        receipt = DraftFreezeReceipt(
            revision_id=revision.revision_id,
            revision_number=revision.revision_number,
            artifact_snapshots=revision.artifact_snapshots,
            frozen_artifact_bundle=_frozen_artifact_bundle(revision.artifact_snapshots),
            answer_question_ids=tuple(sorted(selected_ids, key=str)),
        )
        self._freeze_receipt = receipt
        return receipt

    def _new_revision(
        self, artifacts: DraftArtifacts, *, revision_number: int
    ) -> TaskDraftRevision:
        if self._freeze_receipt is not None:
            raise DraftConflictError("draft is already frozen")
        stored_artifacts = artifacts.model_copy(deep=True)
        revision = TaskDraftRevision(
            revision_id=TaskDraftRevisionId(new_uuid7()),
            revision_number=revision_number,
            artifacts=stored_artifacts,
            artifact_snapshots=_artifact_snapshots(stored_artifacts, revision_number),
        )
        self._revisions[revision.revision_id] = revision
        self._current_revision_id = revision.revision_id
        return revision

    def _require_current_revision(self, revision_id: TaskDraftRevisionId) -> TaskDraftRevision:
        current = self.current_revision
        if current is None or current.revision_id != revision_id:
            raise DraftConflictError("operation must target the current revision")
        return current


class DraftFlow:
    """Stage-gated pre-Run drafting flow with no external side-effect ports."""

    def __init__(self, ledger: DraftLedger | None = None) -> None:
        self.ledger = ledger or DraftLedger()
        self._stage = DraftStage.Explore
        self._reports: list[ExplorationReport] = []
        self._reassignments: list[ExploreReassignment] = []
        self._trial_results: tuple[TrialTranslation, ...] = ()

    @property
    def stage(self) -> DraftStage:
        return self._stage

    @property
    def reports(self) -> tuple[ExplorationReport, ...]:
        return tuple(self._reports)

    @property
    def reassignments(self) -> tuple[ExploreReassignment, ...]:
        return tuple(self._reassignments)

    @property
    def side_effects(self) -> tuple[str, ...]:
        """The draft phase intentionally has no Run, filesystem, or output sink."""

        return ()

    @property
    def run_count(self) -> int:
        return 0

    @property
    def run_event_count(self) -> int:
        return 0

    @property
    def slice_count(self) -> int:
        return 0

    @property
    def candidate_count(self) -> int:
        return 0

    @property
    def managed_output_count(self) -> int:
        return 0

    def tool_is_allowed(self, tool_name: str) -> bool:
        return tool_name in {tool.value for tool in ReadOnlyDraftTool}

    def submit_report(self, report: ExplorationReport) -> None:
        self._require_stage(DraftStage.Explore)
        self._reports.append(report)

    def record_reassignment(self, advice: ExploreReassignment) -> None:
        if self.stage not in {DraftStage.Explore, DraftStage.Align}:
            raise DraftConflictError("reassignment is only available during exploration alignment")
        self._reassignments.append(advice)

    def finish_exploration(self, expected_files: Sequence[str]) -> None:
        self._require_stage(DraftStage.Explore)
        if not self._reports:
            raise DraftConflictError("at least one exploration report is required")
        from .draft_models import DomainSkeleton

        skeleton = tuple(
            DomainSkeleton(domain_path=report.domain_path, files=report.coverage)
            for report in self._reports
        )
        coverage = validate_exact_coverage(skeleton, expected_files)
        if not coverage.valid:
            raise DraftConflictError(f"exploration coverage is not exact: {coverage.model_dump()}")
        self._stage = DraftStage.Align

    def save_artifacts(self, artifacts: DraftArtifacts) -> TaskDraftRevision:
        if self.stage not in {DraftStage.Align, DraftStage.Draft}:
            raise DraftConflictError("artifacts can only be saved after exploration")
        revision = self.ledger.create_revision(artifacts)
        self._stage = DraftStage.Draft
        self._trial_results = ()
        return revision

    def revise_artifacts(self, artifacts: DraftArtifacts) -> TaskDraftRevision:
        """Apply an alignment or calibration conclusion as a new artifact revision."""

        if self.stage not in {DraftStage.Draft, DraftStage.Calibrate}:
            raise DraftConflictError("artifacts can only be revised during drafting or calibration")
        revision = self.ledger.current_revision
        if revision is None:
            raise DraftConflictError("artifact revision requires an existing draft")
        updated = self.ledger.revise(revision.revision_id, artifacts)
        self._stage = DraftStage.Draft
        self._trial_results = ()
        return updated

    def ask_user(self, question: AskUserQuestion) -> AskUserQuestion:
        self._require_stage(DraftStage.Draft)
        return self.ledger.append_question(question)

    def answer_user(self, answer: AskUserAnswer) -> AskUserAnswer:
        self._require_stage(DraftStage.Draft)
        return self.ledger.answer_question(answer)

    def begin_calibration(self) -> None:
        self._require_stage(DraftStage.Draft)
        if self.ledger.current_revision is None:
            raise DraftConflictError("calibration requires a draft revision")
        self._stage = DraftStage.Calibrate

    def trial_translate(
        self,
        risk_hotspots: Sequence[str],
        constrained_outputs: Mapping[str, str],
        freeform_outputs: Mapping[str, str],
    ) -> tuple[TrialTranslation, ...]:
        self._require_stage(DraftStage.Calibrate)
        paths = select_trial_paths(risk_hotspots)
        if set(constrained_outputs) != set(paths) or set(freeform_outputs) != set(paths):
            raise ValueError("trial outputs must cover exactly the selected 2 or 3 files")
        trials = tuple(
            TrialTranslation(
                file_path=RepoRelativePath(path),
                constrained_output=constrained_outputs[path],
                freeform_output=freeform_outputs[path],
            )
            for path in paths
        )
        self._trial_results = trials
        return trials

    def confirm(self) -> DraftFreezeReceipt:
        self._require_stage(DraftStage.Calibrate)
        if not self._trial_results:
            raise DraftConflictError("confirmation requires a completed trial translation")
        revision = self.ledger.current_revision
        if revision is None:
            raise DraftConflictError("confirmation requires a draft revision")
        receipt = self.ledger.freeze(revision.revision_id)
        self._stage = DraftStage.Confirmed
        return receipt

    def _require_stage(self, expected: DraftStage) -> None:
        if self._stage is not expected:
            raise DraftConflictError(
                f"draft operation requires stage {expected.value}, got {self._stage.value}"
            )


def select_trial_paths(risk_hotspots: Sequence[str]) -> tuple[str, ...]:
    """Select two or three deterministic hotspot files for an in-session trial."""

    paths = tuple(normalize_paths(risk_hotspots))
    if len(paths) < 2:
        raise ValueError("trial translation requires 2 or 3 risk-hotspot files")
    return paths[:3]


def normalize_paths(paths: Sequence[str]) -> list[str]:
    from codemigrator.core.paths import normalize_repo_relative_paths

    return normalize_repo_relative_paths(paths)


def _artifact_snapshots(artifacts: DraftArtifacts, version: int) -> tuple[ArtifactSnapshot, ...]:
    names = (
        "spec",
        "understanding_dossier",
        "target_project_blueprint",
        "migration_rulebook",
    )
    return tuple(
        _artifact_snapshot(getattr(artifacts, name), cast(DraftArtifactName, name), version)
        for name in names
    )


def _artifact_snapshot(artifact: object, name: DraftArtifactName, version: int) -> ArtifactSnapshot:
    payload = canonical_json_bytes(artifact)
    return ArtifactSnapshot(
        name=name,
        version=version,
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        media_type="application/json",
    )


def _frozen_artifact_bundle(
    snapshots: Sequence[ArtifactSnapshot],
) -> FrozenArtifactBundle:
    refs = {
        snapshot.name: ArtifactRef(
            sha256=Sha256(snapshot.sha256),
            size=snapshot.size,
            media_type=snapshot.media_type,
        )
        for snapshot in snapshots
    }
    return FrozenArtifactBundle(
        spec=refs["spec"],
        understanding_dossier=refs["understanding_dossier"],
        target_project_blueprint=refs["target_project_blueprint"],
        migration_rulebook=refs["migration_rulebook"],
    )


def _same_answer(left: AskUserAnswer, right: AskUserAnswer) -> bool:
    return (
        left.question_id == right.question_id
        and left.revision_id == right.revision_id
        and left.selected_option == right.selected_option
        and left.free_text == right.free_text
    )


__all__ = [
    "DraftConflictError",
    "DraftFlow",
    "DraftLedger",
    "normalize_paths",
    "select_trial_paths",
]
