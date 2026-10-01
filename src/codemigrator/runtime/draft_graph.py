"""Persistent pre-Run Draft graph and Draft-owned receipt boundary."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, TypedDict, cast
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from codemigrator.core import CreateRun, RunId, canonical_json_bytes
from codemigrator.core.paths import normalize_repo_relative_paths

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .cas import CasObject, FileHostCAS
from .contracts import (
    DraftOwnerReceipt,
    DraftSessionEventSpec,
    RunCreatedReceipt,
    agent_run_lifecycle_spec,
)
from .create_run import CreateRunService
from .draft import DraftConflictError, DraftFlow, DraftLedger, select_trial_paths
from .draft_models import (
    AskUserAnswer,
    AskUserQuestion,
    DraftFreezeReceipt,
    DraftStage,
    ExplorationMerge,
    ExplorationReport,
    ExploreReassignment,
    TaskDraftRevision,
    TrialTranslation,
)
from .loop_contracts import SessionExit, SessionState
from .store import (
    DraftLedgerChangedError,
    DraftOwnerFrozenError,
    RuntimeStore,
    StoreCommitError,
)


class DraftAgentRecoveryError(ValueError):
    """Persisted Draft AgentRun facts do not satisfy the recovery contract."""


class DraftAgentResultUnavailable(RuntimeError):
    """A terminal Draft AgentRun has no matching durable result reference."""


class DraftAgentResultInvalid(ValueError):
    """A durable Draft AgentRun result does not match a Draft result contract."""


class DraftAgentExecutionTerminated(RuntimeError):
    """A Draft AgentRun reached a valid non-completed terminal exit."""

    def __init__(self, exit: SessionExit) -> None:
        self.exit = exit
        super().__init__(f"Draft AgentRun terminated with {exit.value}")


class DraftAgentExecutionFailed(DraftAgentExecutionTerminated):
    """A Draft AgentRun failed or exhausted its execution budget."""


class _DraftGraphState(TypedDict, total=False):
    """Only owner IDs, graph cursor and committed receipt references belong here."""

    draft_id: str
    cursor: str
    question_id: str
    question_receipt_key: str
    answer_receipt_key: str
    agent_run_id: str
    agent_receipt_id: str
    result_reference_key: str
    result_sha256: str
    owner_result_receipt_key: str


class _DraftAgentGraphState(TypedDict, total=False):
    """Agent subgraph checkpoints contain references only, never task or result bodies."""

    draft_id: str
    cursor: str
    agent_run_id: str
    agent_receipt_id: str
    result_reference_key: str
    result_sha256: str
    owner_result_receipt_key: str


@dataclass(frozen=True, slots=True)
class _DraftAgentContext:
    """Invocation-only data; LangGraph context is not part of persisted graph state."""

    logical_task_key: str
    expected_category: str
    task: str
    trial_group_paths: tuple[str, ...] | None = None
    trial_revision_id: str | None = None


DraftAgentResult: TypeAlias = (
    ExplorationReport | ExploreReassignment | ExplorationMerge | TrialTranslation
)


class DraftOwnerPort(Protocol):
    draft_id: UUID
    freeze_receipt: DraftFreezeReceipt | None
    current_revision_id: str | None

    async def load_fact(
        self, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None: ...

    async def commit_question(self, question: AskUserQuestion) -> DraftOwnerReceipt: ...

    async def load_question(self, question_id: str) -> AskUserQuestion | None: ...

    async def load_answer_receipt(self, answer: AskUserAnswer) -> DraftOwnerReceipt | None: ...

    async def commit_answer(self, answer: AskUserAnswer) -> DraftOwnerReceipt: ...

    async def persist_current_revision(self) -> DraftOwnerReceipt: ...

    async def persist_freeze_receipt(self) -> DraftOwnerReceipt: ...

    async def restore_ledger(self) -> None: ...

    async def has_receipt(self, receipt_key: str) -> bool: ...

    async def commit_lifecycle_fact(
        self, receipt_key: str, category: str, fact: Mapping[str, object]
    ) -> DraftOwnerReceipt: ...

    async def commit_agent_started(self, record: AgentRun) -> DraftOwnerReceipt: ...

    async def commit_agent_terminal(
        self, record: AgentRun, receipt: AgentRunReceipt
    ) -> DraftOwnerReceipt: ...

    async def materialize_agent_result(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        result_reference: CasObject,
        result: DraftAgentResult,
        *,
        trial_group_paths: Sequence[str] | None = None,
        trial_revision_id: str | None = None,
    ) -> DraftOwnerReceipt: ...

    def validate_trial_group(self, paths: Sequence[str]) -> tuple[str, ...]: ...


class DraftFlowOwner:
    """Commit and recover Draft business facts behind durable owner receipts."""

    def __init__(self, *, draft_id: UUID, flow: DraftFlow, store: RuntimeStore) -> None:
        self.draft_id = draft_id
        self.flow = flow
        self.store = store
        self._materialized_agent_results: set[str] = set()
        self._trial_agent_results: dict[str, TrialTranslation] = {}
        self._trial_group_paths: tuple[str, ...] | None = None
        self._trial_revision_id: str | None = None

    @property
    def freeze_receipt(self) -> DraftFreezeReceipt | None:
        return self.flow.ledger.freeze_receipt

    @property
    def current_revision_id(self) -> str | None:
        revision = self.flow.ledger.current_revision
        return str(revision.revision_id) if revision is not None else None

    def validate_trial_group(self, paths: Sequence[str]) -> tuple[str, ...]:
        selected = select_trial_paths(paths)
        revision_id = self.current_revision_id
        if revision_id is None:
            raise DraftConflictError("trial translation requires a current TaskDraftRevision")
        if self.flow.stage is not DraftStage.Calibrate:
            raise DraftConflictError("trial results can only be materialized in Calibrate")
        if self._trial_revision_id != revision_id:
            self._trial_agent_results.clear()
            self._trial_group_paths = None
            self.flow._trial_results = ()
            self._trial_revision_id = revision_id
        if self._trial_group_paths is not None and self._trial_group_paths != selected:
            raise DraftConflictError("Draft trial replay changed the selected file group")
        self._trial_group_paths = selected
        return selected

    async def load_fact(
        self, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None:
        return await self.store.load_draft_owner_fact(self.draft_id, receipt_key)

    async def persist_current_revision(self) -> DraftOwnerReceipt:
        revision = self.flow.ledger.current_revision
        if revision is None:
            raise DraftConflictError("Draft revision must exist before it can be persisted")
        persisted = await self._load_persisted_ledger()
        receipt_key = _revision_receipt_key(revision.revision_number)
        existing = await self.store.load_draft_owner_fact(self.draft_id, receipt_key)
        body = revision.model_dump(mode="json", by_alias=True)
        if existing is not None:
            if existing[0].category != "draft.task_revision" or existing[1] != body:
                raise StoreCommitError("Draft revision receipt replay mismatch")
            if persisted.current_revision != revision:
                raise DraftConflictError("persisted Draft revision is no longer current")
            return existing[0]
        if persisted.freeze_receipt is not None:
            raise DraftConflictError("a frozen Draft cannot append another revision")
        if revision.revision_number != len(persisted.revisions) + 1:
            raise DraftConflictError("Draft revision persistence must remain contiguous")
        try:
            return await self.store.commit_draft_owner_fact(
                self.draft_id,
                receipt_key,
                "draft.task_revision",
                body,
            )
        except DraftOwnerFrozenError as exc:
            await self.restore_ledger()
            raise DraftConflictError("Draft is already frozen; a revision cannot be added") from exc

    async def persist_freeze_receipt(self) -> DraftOwnerReceipt:
        freeze_receipt = self.flow.ledger.freeze_receipt
        if freeze_receipt is None:
            raise DraftConflictError("Draft must be explicitly confirmed before freeze persistence")
        await self.persist_current_revision()
        facts = await self.store.list_draft_owner_facts(self.draft_id)
        persisted = await self._load_persisted_ledger(facts)
        if persisted.questions != self.flow.ledger.questions or (
            persisted.answers != self.flow.ledger.answers
        ):
            raise DraftConflictError("Draft questions and answers must be committed before freeze")
        if persisted.freeze_receipt is not None and persisted.freeze_receipt != freeze_receipt:
            raise StoreCommitError("Draft freeze receipt conflicts with the committed receipt")
        try:
            return await self.store.commit_draft_owner_fact(
                self.draft_id,
                "draft.freeze",
                "draft.freeze",
                freeze_receipt.model_dump(mode="json", by_alias=True),
                expected_existing_facts=tuple(receipt for receipt, _ in facts),
            )
        except DraftLedgerChangedError as exc:
            await self.restore_ledger()
            raise DraftConflictError(
                "persisted Draft owner facts changed before freeze could commit"
            ) from exc

    async def restore_ledger(self) -> None:
        facts = await self.store.list_draft_owner_facts(self.draft_id)
        self.flow.ledger = await self._load_persisted_ledger(facts)
        materialized = [
            (receipt, fact) for receipt, fact in facts if receipt.category == "draft.agent.result"
        ]
        initial_stage = self.flow.stage
        if materialized:
            self.flow._stage = DraftStage.Explore
            self.flow._reports.clear()
            self.flow._reassignments.clear()
            self.flow._merged_exploration = None
            self.flow._trial_results = ()
            self._materialized_agent_results.clear()
            self._trial_agent_results.clear()
            self._trial_group_paths = None
            self._trial_revision_id = None
        for receipt, fact in sorted(materialized, key=_agent_result_restore_key):
            result, logical_task_key, agent_run_id = _agent_result_from_fact(receipt, fact)
            trial_group_paths: tuple[str, ...] | None = None
            trial_revision_id: str | None = None
            if isinstance(result, TrialTranslation):
                raw_trial_group = fact.get("trial_group_paths")
                raw_trial_revision = fact.get("draft_revision_id")
                if (
                    not isinstance(raw_trial_group, (list, tuple))
                    or not all(isinstance(path, str) for path in raw_trial_group)
                    or not isinstance(raw_trial_revision, str)
                ):
                    raise StoreCommitError("stored trial result has no Draft revision identity")
                trial_group_paths = tuple(raw_trial_group)
                trial_revision_id = raw_trial_revision
                if trial_revision_id != self.current_revision_id:
                    # Old AgentRun output remains auditable but cannot affect the current Draft.
                    continue
                self._trial_revision_id = trial_revision_id
            self._validate_agent_result(
                result,
                logical_task_key,
                trial_group_paths=trial_group_paths,
                trial_revision_id=trial_revision_id,
                restoring=True,
            )
            self._apply_agent_result(
                receipt.receipt_key,
                result,
                trial_group_paths=trial_group_paths,
                restoring=True,
            )
            if receipt.receipt_key != f"draft.agent.result:{agent_run_id}":
                raise StoreCommitError("Draft agent result receipt key is inconsistent")

        if self.flow.ledger.freeze_receipt is not None:
            if self._trial_agent_results:
                self.flow._stage = DraftStage.Confirmed
            else:
                self.flow._stage = initial_stage
        elif self._trial_group_paths is not None:
            self.flow._stage = DraftStage.Calibrate
            self._apply_trial_results()
        elif self.flow.ledger.current_revision is not None:
            self.flow._stage = (
                DraftStage.Draft if self.flow.merged_exploration is not None else DraftStage.Align
            )

    async def materialize_agent_result(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        result_reference: CasObject,
        result: DraftAgentResult,
        *,
        trial_group_paths: Sequence[str] | None = None,
        trial_revision_id: str | None = None,
    ) -> DraftOwnerReceipt:
        """Persist a typed AgentRun result and apply it through the DraftFlow owner."""

        if (
            record.owner_kind != "draft"
            or record.owner_id != self.draft_id
            or record.exit is not SessionExit.Completed
            or receipt.agent_run_id != record.agent_run_id
            or result_reference.digest != record.result_sha256
        ):
            raise DraftAgentResultUnavailable(
                "Draft result materialization requires its completed AgentRun CAS receipt"
            )
        if await self.has_receipt("draft.closed") or await self.has_receipt("draft.attached"):
            raise DraftConflictError("Draft session is already closed or attached to a Run")
        if self.freeze_receipt is not None:
            raise DraftConflictError("a frozen Draft cannot accept another AgentRun result")

        logical_task_key = record.logical_task_key
        receipt_key = f"draft.agent.result:{record.agent_run_id}"
        fact: dict[str, object] = {
            "agent_run_id": str(record.agent_run_id),
            "logical_task_key": logical_task_key,
            "agent_receipt_id": str(receipt.receipt_id),
            "result_sha256": result_reference.digest,
            "result_type": type(result).__name__,
            "result": result.model_dump(mode="json"),
            "trial_group_paths": (
                list(trial_group_paths)
                if isinstance(result, TrialTranslation) and trial_group_paths is not None
                else None
            ),
            "draft_revision_id": (
                trial_revision_id if isinstance(result, TrialTranslation) else None
            ),
        }
        previous = await self.store.load_draft_owner_fact(self.draft_id, receipt_key)
        if previous is not None:
            if previous[0].category != "draft.agent.result" or previous[1] != fact:
                raise StoreCommitError("Draft AgentRun result receipt replay mismatch")
            if receipt_key in self._materialized_agent_results:
                return previous[0]

        self._validate_agent_result(
            result,
            logical_task_key,
            trial_group_paths=trial_group_paths,
            trial_revision_id=trial_revision_id,
        )
        if isinstance(result, TrialTranslation):
            await self.persist_current_revision()
        owner_receipt = (
            previous[0]
            if previous is not None
            else await self.store.commit_draft_owner_fact(
                self.draft_id,
                receipt_key,
                "draft.agent.result",
                fact,
            )
        )
        if receipt_key not in self._materialized_agent_results:
            self._apply_agent_result(
                receipt_key,
                result,
                trial_group_paths=trial_group_paths,
            )
            self._materialized_agent_results.add(receipt_key)
        return owner_receipt

    def _validate_agent_result(
        self,
        result: DraftAgentResult,
        logical_task_key: str,
        *,
        trial_group_paths: Sequence[str] | None = None,
        trial_revision_id: str | None = None,
        restoring: bool = False,
    ) -> None:
        if isinstance(result, ExplorationReport):
            if logical_task_key != exploration_task_key(result.domain_path):
                raise DraftAgentResultInvalid(
                    "exploration result domain does not match its logical task"
                )
            if not restoring and self.flow.stage is not DraftStage.Explore:
                raise DraftConflictError("exploration reports can only be materialized in Explore")
            if any(report.domain_path == result.domain_path for report in self.flow.reports):
                if result not in self.flow.reports:
                    raise DraftConflictError("exploration domain already has a different report")
            return
        if isinstance(result, ExploreReassignment):
            if _coordinator_task_round(logical_task_key) is None:
                raise DraftAgentResultInvalid(
                    "reassignment result does not match the coordinator task"
                )
            if not restoring and self.flow.stage not in {DraftStage.Explore, DraftStage.Align}:
                raise DraftConflictError("exploration reassignment is outside its Draft stage")
            return
        if isinstance(result, ExplorationMerge):
            if _coordinator_task_round(logical_task_key) is None:
                raise DraftAgentResultInvalid("merge result does not match the coordinator task")
            if not restoring and self.flow.stage is not DraftStage.Explore:
                raise DraftConflictError("exploration merge can only be materialized in Explore")
            if self.flow.merged_exploration is None:
                expected = self._preview_exploration_merge()
                if expected != result:
                    raise DraftAgentResultInvalid(
                        "coordinator merge does not match DraftFlow coverage validation"
                    )
            elif self.flow.merged_exploration != result:
                raise DraftConflictError("Draft exploration already has a different merge")
            return
        if isinstance(result, TrialTranslation):
            if (
                trial_revision_id is None
                or trial_revision_id != self.current_revision_id
                or logical_task_key
                != trial_translation_task_key(result.file_path, trial_revision_id)
            ):
                raise DraftAgentResultInvalid(
                    "trial result does not match the current Draft revision and logical task"
                )
            if not restoring and self.flow.stage is not DraftStage.Calibrate:
                raise DraftConflictError("trial results can only be materialized in Calibrate")
            selected_paths = _normalize_trial_group(trial_group_paths)
            if result.file_path not in selected_paths:
                raise DraftAgentResultInvalid("trial result is outside its selected file group")
            if (
                self._trial_group_paths is not None
                and self._trial_group_paths != selected_paths
            ):
                raise DraftConflictError("Draft trial replay changed the selected file group")
            if set(self._trial_agent_results).difference(selected_paths):
                raise DraftConflictError("Draft trial has results outside its selected file group")
            return
        raise DraftAgentResultInvalid("Draft AgentRun result type is unsupported")

    def _preview_exploration_merge(self) -> ExplorationMerge:
        if self.flow.merged_exploration is not None:
            return self.flow.merged_exploration
        expected_files = self._expected_exploration_files()
        candidate = DraftFlow(
            self.flow.ledger,
            module_files=self.flow._module_files,
            max_fanout=self.flow._max_fanout,
        )
        for report in self.flow.reports:
            candidate.submit_report(report)
        return candidate.finish_exploration(expected_files)

    def _expected_exploration_files(self) -> tuple[str, ...]:
        if self.flow._module_files is not None:
            return tuple(
                file_path
                for module_path in sorted(self.flow._module_files)
                for file_path in self.flow._module_files[module_path]
            )
        return tuple(file_path for report in self.flow.reports for file_path in report.coverage)

    def _apply_agent_result(
        self,
        receipt_key: str,
        result: DraftAgentResult,
        *,
        trial_group_paths: Sequence[str] | None = None,
        restoring: bool = False,
    ) -> None:
        if isinstance(result, ExplorationReport):
            if result not in self.flow.reports:
                self.flow.submit_report(result)
        elif isinstance(result, ExploreReassignment):
            if result not in self.flow.reassignments:
                self.flow.record_reassignment(result)
        elif isinstance(result, ExplorationMerge):
            if self.flow.merged_exploration != result:
                self.flow.finish_exploration(self._expected_exploration_files())
        elif isinstance(result, TrialTranslation):
            selected_paths = _normalize_trial_group(trial_group_paths)
            revision_id = self.current_revision_id
            if revision_id is None or revision_id != self._trial_revision_id:
                raise DraftConflictError("trial result belongs to a stale Draft revision")
            self._trial_group_paths = selected_paths
            self._trial_agent_results[result.file_path] = result
            if set(self._trial_agent_results) == set(selected_paths):
                self._apply_trial_results()
        self._materialized_agent_results.add(receipt_key)

    def _apply_trial_results(self) -> None:
        paths = self._trial_group_paths
        if paths is None or set(self._trial_agent_results) != set(paths):
            return
        self.flow.trial_translate(
            paths,
            {path: self._trial_agent_results[path].constrained_output for path in paths},
            {path: self._trial_agent_results[path].freeform_output for path in paths},
        )

    async def _load_persisted_ledger(
        self,
        facts: Sequence[tuple[DraftOwnerReceipt, dict[str, object]]] | None = None,
    ) -> DraftLedger:
        revisions: list[TaskDraftRevision] = []
        questions: list[AskUserQuestion] = []
        answers: list[AskUserAnswer] = []
        freeze_receipt: DraftFreezeReceipt | None = None
        persisted_facts = (
            await self.store.list_draft_owner_facts(self.draft_id) if facts is None else facts
        )
        for receipt, fact in persisted_facts:
            try:
                if receipt.category == "draft.task_revision":
                    revision = TaskDraftRevision.model_validate(fact)
                    if receipt.receipt_key != _revision_receipt_key(revision.revision_number):
                        raise StoreCommitError("Draft revision receipt key is inconsistent")
                    revisions.append(revision)
                elif receipt.category == "draft.ask_user.question":
                    question = AskUserQuestion.model_validate(fact)
                    if receipt.receipt_key != _question_receipt_key(question.question_id):
                        raise StoreCommitError("Draft question receipt key is inconsistent")
                    questions.append(question)
                elif receipt.category == "draft.ask_user.answer":
                    answer = AskUserAnswer.model_validate(fact)
                    if receipt.receipt_key != _answer_receipt_key(answer.question_id):
                        raise StoreCommitError("Draft answer receipt key is inconsistent")
                    answers.append(answer)
                elif receipt.category == "draft.freeze":
                    if receipt.receipt_key != "draft.freeze" or freeze_receipt is not None:
                        raise StoreCommitError("Draft freeze receipt key is inconsistent")
                    freeze_receipt = DraftFreezeReceipt.model_validate(fact)
            except StoreCommitError:
                raise
            except (TypeError, ValueError) as exc:
                raise StoreCommitError("stored Draft ledger fact is invalid") from exc
        try:
            return DraftLedger.restore(
                revisions=revisions,
                questions=questions,
                answers=answers,
                freeze_receipt=freeze_receipt,
            )
        except DraftConflictError as exc:
            raise StoreCommitError("stored Draft ledger facts are inconsistent") from exc

    async def commit_question(self, question: AskUserQuestion) -> DraftOwnerReceipt:
        if self.flow.ledger.current_revision is None or (
            self.flow.ledger.current_revision.revision_id != question.revision_id
        ):
            raise DraftConflictError("AskUser question must target the current Draft revision")
        await self.persist_current_revision()
        receipt_key = _question_receipt_key(question.question_id)
        body = question.model_dump(mode="json")
        previous = await self.store.load_draft_owner_fact(self.draft_id, receipt_key)
        if previous is not None and (
            previous[0].category != "draft.ask_user.question" or previous[1] != body
        ):
            raise StoreCommitError("Draft question receipt replay mismatch")
        self.flow.ask_user(question)
        if previous is not None:
            return previous[0]
        try:
            return await self.store.commit_draft_owner_fact(
                self.draft_id,
                receipt_key,
                "draft.ask_user.question",
                body,
                events=(
                    DraftSessionEventSpec(
                        "session.question.asked", {"question_id": str(question.question_id)}
                    ),
                ),
            )
        except DraftOwnerFrozenError as exc:
            await self.restore_ledger()
            raise DraftConflictError("Draft is already frozen; a question cannot be added") from exc

    async def load_question(self, question_id: str) -> AskUserQuestion | None:
        match = next(
            (item for item in self.flow.ledger.questions if str(item.question_id) == question_id),
            None,
        )
        if match is not None:
            return match
        record = await self.store.load_draft_owner_fact(
            self.draft_id, f"draft.question:{question_id}"
        )
        if record is None or record[0].category != "draft.ask_user.question":
            return None
        question = AskUserQuestion.model_validate(record[1])
        self.flow.ask_user(question)
        return question

    async def load_answer_receipt(self, answer: AskUserAnswer) -> DraftOwnerReceipt | None:
        key = _answer_receipt_key(answer.question_id)
        record = await self.store.load_draft_owner_fact(self.draft_id, key)
        if record is None:
            return None
        if record[0].category != "draft.ask_user.answer" or record[1] != answer.model_dump(
            mode="json"
        ):
            raise DraftConflictError("answer conflicts with the committed Draft answer")
        return record[0]

    async def commit_answer(self, answer: AskUserAnswer) -> DraftOwnerReceipt:
        self.flow.answer_user(answer)
        existing = await self.load_answer_receipt(answer)
        if existing is not None:
            return existing
        try:
            return await self.store.commit_draft_owner_fact(
                self.draft_id,
                _answer_receipt_key(answer.question_id),
                "draft.ask_user.answer",
                answer.model_dump(mode="json"),
                events=(
                    DraftSessionEventSpec(
                        "session.question.answered", {"question_id": str(answer.question_id)}
                    ),
                ),
            )
        except DraftOwnerFrozenError as exc:
            await self.restore_ledger()
            raise DraftConflictError("Draft is already frozen; an answer cannot be added") from exc

    async def has_receipt(self, receipt_key: str) -> bool:
        return await self.store.load_draft_owner_fact(self.draft_id, receipt_key) is not None

    async def commit_lifecycle_fact(
        self, receipt_key: str, category: str, fact: Mapping[str, object]
    ) -> DraftOwnerReceipt:
        events: tuple[DraftSessionEventSpec, ...] = ()
        if category == "draft.attached_to_run":
            events = (DraftSessionEventSpec("session.attached_to_run", {"run_id": fact["run_id"]}),)
        elif category == "draft.closed":
            events = (DraftSessionEventSpec("session.closed", {"status": "CLOSED"}),)
        return await self.store.commit_draft_owner_fact(
            self.draft_id, receipt_key, category, fact, events=events
        )

    async def commit_agent_started(self, record: AgentRun) -> DraftOwnerReceipt:
        spec = agent_run_lifecycle_spec(record)
        return await self.store.commit_draft_owner_fact(
            self.draft_id,
            f"draft.agent.started:{record.agent_run_id}",
            "draft.agent.started",
            {"agent_run_id": str(record.agent_run_id)},
            events=(DraftSessionEventSpec(spec.event_type, spec.data),),
        )

    async def commit_agent_terminal(
        self, record: AgentRun, receipt: AgentRunReceipt
    ) -> DraftOwnerReceipt:
        spec = agent_run_lifecycle_spec(record, receipt)
        return await self.store.commit_draft_owner_fact(
            self.draft_id,
            f"draft.agent.terminal:{record.agent_run_id}",
            "draft.agent.terminal",
            {"agent_run_id": str(record.agent_run_id), "receipt_id": str(receipt.receipt_id)},
            events=(DraftSessionEventSpec(spec.event_type, spec.data),),
        )


class DraftAgentRunStore(Protocol):
    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun: ...

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None: ...

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None: ...

    async def get_cas_reference(
        self, owner_kind: str, owner_id: UUID, reference_key: str
    ) -> CasObject | None: ...

    async def list_agent_runs_by_owner(
        self, owner_kind: str, owner_id: UUID
    ) -> tuple[AgentRun, ...]: ...


@dataclass(frozen=True, slots=True)
class DraftAgentCompletion:
    """A durable AgentRun receipt, opaque result reference and typed owner result."""

    record: AgentRun
    receipt: AgentRunReceipt
    result: CasObject | None
    materialized: DraftAgentResult | None = None


class DraftAgentRunnerPort(Protocol):
    async def run(
        self,
        draft_id: UUID,
        logical_task_key: str,
        task: str,
        *,
        lifecycle: DraftAgentLifecyclePort,
    ) -> DraftAgentCompletion: ...


class DraftAgentLifecyclePort(Protocol):
    async def started(self, record: AgentRun) -> None: ...

    async def terminal(self, record: AgentRun, receipt: AgentRunReceipt) -> None: ...


@dataclass(slots=True)
class _DraftAgentLifecycle:
    graph: MigrationSessionGraph
    logical_task_key: str
    expected_category: str
    started_seen: bool = False
    terminal_seen: bool = False

    async def started(self, record: AgentRun) -> None:
        await self.graph._commit_agent_started(record, self.logical_task_key)
        self.started_seen = True

    async def terminal(self, record: AgentRun, receipt: AgentRunReceipt) -> None:
        if not self.started_seen:
            raise ValueError("Draft AgentRun terminal requires a published start")
        await self.graph._commit_agent_terminal(
            record, receipt, self.logical_task_key, self.expected_category
        )
        self.terminal_seen = True


class MigrationSessionGraph:
    """Own one durable Draft thread; Run graphs always use separate threads."""

    def __init__(
        self,
        *,
        owner: DraftOwnerPort,
        agent_runs: DraftAgentRunStore,
        checkpointer: BaseCheckpointSaver[Any],
        agent_checkpointer: BaseCheckpointSaver[Any] | None = None,
        thread_id: str | None = None,
        create_run_service: CreateRunService | None = None,
        agent_runner: DraftAgentRunnerPort | None = None,
    ) -> None:
        self.owner = owner
        self.agent_runs = agent_runs
        self.checkpointer = checkpointer
        self.agent_checkpointer = agent_checkpointer or checkpointer
        self.thread_id = thread_id or str(uuid4())
        try:
            parsed_thread_id = UUID(self.thread_id)
        except ValueError as exc:
            raise ValueError("Draft graph thread id must be UUID") from exc
        if parsed_thread_id == owner.draft_id:
            raise ValueError("Draft graph thread must be independent of its owner id")
        self.create_run_service = create_run_service
        self.agent_runner = agent_runner
        agent_builder = StateGraph(
            _DraftAgentGraphState,
            context_schema=_DraftAgentContext,
        )
        agent_builder.add_node("execute_agent", self._execute_agent_node)
        agent_builder.add_node("materialize_result", self._materialize_result_node)
        agent_builder.set_entry_point("execute_agent")
        agent_builder.add_edge("execute_agent", "materialize_result")
        agent_builder.add_edge("materialize_result", END)
        self._draft_agent_graph = agent_builder.compile()

        builder = StateGraph(
            _DraftGraphState,
            context_schema=_DraftAgentContext,
        )
        builder.add_node("ask_user", self._ask_user)
        builder.add_node("draft_agent", self._invoke_draft_agent_subgraph)
        builder.add_conditional_edges(
            START,
            self._route_entry,
            {"ask_user": "ask_user", "draft_agent": "draft_agent"},
        )
        builder.add_edge("ask_user", END)
        builder.add_edge("draft_agent", END)
        self._graph = builder.compile(checkpointer=checkpointer)

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.thread_id}}

    async def restore(self) -> Any | None:
        """Restore the durable owner ledger and this Draft thread checkpoint."""

        await self.owner.restore_ledger()
        return await self._graph.aget_state(self.config)

    async def ask_user(self, question: AskUserQuestion) -> DraftOwnerReceipt:
        await self._ensure_open()
        receipt = await self.owner.commit_question(question)
        if await self.owner.has_receipt(_answer_receipt_key(question.question_id)):
            return receipt
        snapshot = await self._graph.aget_state(self.config)
        if snapshot is not None and _has_interrupt(snapshot):
            current_id = snapshot.values.get("question_id")
            if current_id == str(question.question_id):
                return receipt
            raise DraftConflictError("Draft graph is waiting for a different AskUser answer")
        await self._graph.ainvoke(
            {
                "draft_id": str(self.owner.draft_id),
                "cursor": "ASK_USER",
                "question_id": str(question.question_id),
                "question_receipt_key": receipt.receipt_key,
            },
            config=self.config,
        )
        return receipt

    async def answer_user(self, answer: AskUserAnswer) -> DraftOwnerReceipt:
        await self._ensure_open()
        existing = await self.owner.load_answer_receipt(answer)
        snapshot = await self._graph.aget_state(self.config)
        pending = snapshot is not None and _has_interrupt(snapshot)
        if pending:
            question_id = snapshot.values.get("question_id")
            if question_id != str(answer.question_id):
                raise DraftConflictError("answer does not match the pending Draft question")
        elif existing is None:
            raise DraftConflictError("Draft graph has no matching AskUser interrupt")

        receipt = await self.owner.commit_answer(answer)
        if not pending:
            return receipt
        await self._graph.ainvoke(
            Command(resume={"answer_receipt_key": receipt.receipt_key}),
            config=self.config,
        )
        return receipt

    async def get_or_create_agent_run(self, candidate: AgentRun) -> AgentRun:
        await self._ensure_open()
        if candidate.owner_kind != "draft" or candidate.owner_id != self.owner.draft_id:
            raise ValueError("Draft AgentRun candidate has a different owner")
        if candidate.thread_id == self.thread_id:
            raise ValueError("Draft AgentRun and MigrationSessionGraph need distinct threads")
        if not candidate.logical_task_key.startswith("draft."):
            raise ValueError("Draft AgentRun logical task key must use the Draft namespace")
        return await self.agent_runs.create_or_get_agent_run(candidate)

    async def explore_domain(self, domain_path: str, task: str) -> DraftAgentCompletion:
        return await self._run_agent_graph(
            exploration_task_key(domain_path), task, "draft.exploration.completed"
        )

    async def coordinate_exploration(
        self, task: str, *, round_number: int = 1
    ) -> DraftAgentCompletion:
        return await self._run_agent_graph(
            coordinator_task_key(round_number), task, "draft.coordinator.completed"
        )

    async def trial_translate(
        self, risk_hotspots: Sequence[str], tasks_by_file: Mapping[str, str]
    ) -> tuple[DraftAgentCompletion, ...]:
        paths = self.owner.validate_trial_group(risk_hotspots)
        revision_id = self.owner.current_revision_id
        if revision_id is None:
            raise DraftConflictError("trial translation requires a current TaskDraftRevision")
        if set(tasks_by_file) != set(paths):
            raise ValueError("trial tasks must cover exactly the selected 2 or 3 files")
        completions: list[DraftAgentCompletion] = []
        for path in paths:
            task = tasks_by_file[path]
            if not isinstance(task, str) or not task.strip():
                raise ValueError("each Draft trial task must be non-empty text")
            completions.append(
                await self._run_agent_graph(
                    trial_translation_task_key(path, revision_id),
                    task,
                    "draft.trial.completed",
                    trial_group_paths=paths,
                    trial_revision_id=revision_id,
                )
            )
        return tuple(completions)

    async def attach_to_run(self, run_id: RunId, request: CreateRun) -> RunCreatedReceipt:
        if await self.owner.has_receipt("draft.closed"):
            raise DraftConflictError("a closed Draft cannot create a Run")
        if self.thread_id == str(run_id):
            raise ValueError("Draft and Run graphs cannot share a thread id")
        request_digest = hashlib.sha256(canonical_json_bytes(request)).hexdigest()
        previous = await self.owner.load_fact("draft.attached")
        if previous is not None:
            fact = previous[1]
            if fact.get("run_id") != str(run_id):
                raise DraftConflictError("Draft is already attached to another Run")
            if fact.get("create_request_sha256") != request_digest:
                raise DraftConflictError("Draft attachment replay changed the CreateRun request")
            receipt_key = fact.get("run_receipt_key")
            event_sequence = fact.get("event_sequence")
            state_version = fact.get("state_version")
            if (
                not isinstance(receipt_key, str)
                or type(event_sequence) is not int
                or type(state_version) is not int
            ):
                raise StoreCommitError("stored Draft attachment receipt is invalid")
            await self._release_owned_threads()
            return RunCreatedReceipt(
                run_id=run_id,
                receipt_key=receipt_key,
                event_sequence=event_sequence,
                state_version=state_version,
            )
        frozen = self.owner.freeze_receipt
        if frozen is None or request.frozen_artifacts != frozen.frozen_artifact_bundle:
            raise DraftConflictError("CreateRun requires the confirmed Draft artifact freeze")
        await self.owner.persist_freeze_receipt()
        if self.create_run_service is None:
            raise RuntimeError("Draft graph has no CreateRun service")
        receipt = await self.create_run_service.create(run_id, request)
        await self.owner.commit_lifecycle_fact(
            "draft.attached",
            "draft.attached_to_run",
            {
                "run_id": str(run_id),
                "run_receipt_key": receipt.receipt_key,
                "event_sequence": receipt.event_sequence,
                "state_version": receipt.state_version,
                "create_request_sha256": request_digest,
            },
        )
        await self._release_owned_threads()
        return receipt

    async def close(self) -> DraftOwnerReceipt:
        if await self.owner.has_receipt("draft.attached"):
            raise DraftConflictError("an attached Draft cannot be closed independently")
        previous = await self.owner.load_fact("draft.closed")
        if previous is not None:
            await self._release_owned_threads()
            return previous[0]
        receipt = await self.owner.commit_lifecycle_fact(
            "draft.closed", "draft.closed", {"thread_id": self.thread_id}
        )
        await self._release_owned_threads()
        return receipt

    async def _ask_user(self, state: _DraftGraphState) -> _DraftGraphState:
        draft_id = state.get("draft_id")
        question_id = state.get("question_id")
        receipt_key = state.get("question_receipt_key")
        if draft_id != str(self.owner.draft_id) or not question_id or not receipt_key:
            raise ValueError("Draft AskUser graph state is incomplete")
        if not await self.owner.has_receipt(receipt_key):
            raise ValueError("Draft graph cannot interrupt before the question receipt")
        question = await self.owner.load_question(question_id)
        if question is None or str(question.question_id) != question_id:
            raise ValueError("Draft question is not present in its owner ledger")
        resumed = interrupt({"question_id": question_id, "question_receipt_key": receipt_key})
        if not isinstance(resumed, Mapping):
            raise ValueError("Draft graph resume payload is invalid")
        answer_receipt_key = resumed.get("answer_receipt_key")
        if not isinstance(answer_receipt_key, str) or not await self.owner.has_receipt(
            answer_receipt_key
        ):
            raise ValueError("Draft graph cannot advance before the answer receipt")
        return {"cursor": "ASK_USER_ANSWERED", "answer_receipt_key": answer_receipt_key}

    @staticmethod
    def _route_entry(state: _DraftGraphState) -> str:
        cursor = state.get("cursor")
        if cursor == "ASK_USER":
            return "ask_user"
        if cursor == "DRAFT_AGENT":
            return "draft_agent"
        raise ValueError("Draft graph request cursor is invalid")

    async def _invoke_draft_agent_subgraph(
        self,
        state: _DraftGraphState,
        runtime: Runtime[_DraftAgentContext],
    ) -> _DraftGraphState:
        context = runtime.context
        if not isinstance(context, _DraftAgentContext):
            raise ValueError("Draft Agent graph invocation context is missing")
        if state.get("draft_id") != str(self.owner.draft_id):
            raise ValueError("Draft Agent graph state has a different owner")
        result = await self._draft_agent_graph.ainvoke(
            {"draft_id": str(self.owner.draft_id), "cursor": "AGENT_RUN"},
            context=context,
        )
        return cast(
            _DraftGraphState,
            {
                key: result[key]
                for key in (
                    "cursor",
                    "agent_run_id",
                    "agent_receipt_id",
                    "result_reference_key",
                    "result_sha256",
                    "owner_result_receipt_key",
                )
            },
        )

    async def _execute_agent_node(
        self,
        state: _DraftAgentGraphState,
        runtime: Runtime[_DraftAgentContext],
    ) -> _DraftAgentGraphState:
        context = runtime.context
        if not isinstance(context, _DraftAgentContext):
            raise ValueError("Draft Agent graph invocation context is missing")
        if state.get("draft_id") != str(self.owner.draft_id):
            raise ValueError("Draft Agent graph state has a different owner")
        completion = await self._run_agent(
            context.logical_task_key,
            context.task,
            context.expected_category,
        )
        if completion.result is None:
            raise DraftAgentResultUnavailable(
                "completed Draft AgentRun did not return a durable result reference"
            )
        return {
            "cursor": "AGENT_RUN_COMPLETED",
            "agent_run_id": str(completion.record.agent_run_id),
            "agent_receipt_id": str(completion.receipt.receipt_id),
            "result_reference_key": _agent_result_reference_key(completion.record.agent_run_id),
            "result_sha256": completion.result.digest,
        }

    async def _materialize_result_node(
        self,
        state: _DraftAgentGraphState,
        runtime: Runtime[_DraftAgentContext],
    ) -> _DraftAgentGraphState:
        context = runtime.context
        agent_run_id = state.get("agent_run_id")
        receipt_id = state.get("agent_receipt_id")
        reference_key = state.get("result_reference_key")
        result_sha256 = state.get("result_sha256")
        if (
            not isinstance(context, _DraftAgentContext)
            or state.get("cursor") != "AGENT_RUN_COMPLETED"
            or not isinstance(agent_run_id, str)
            or not isinstance(receipt_id, str)
            or not isinstance(reference_key, str)
            or not isinstance(result_sha256, str)
        ):
            raise ValueError("Draft Agent result materialization state is incomplete")
        run_id = AgentRunId(UUID(agent_run_id))
        record = await self.agent_runs.load_agent_run(run_id)
        receipt = await self.agent_runs.load_agent_run_receipt(run_id)
        if (
            record is None
            or receipt is None
            or receipt.receipt_id != UUID(receipt_id)
            or record.logical_task_key != context.logical_task_key
            or record.exit is not SessionExit.Completed
        ):
            raise DraftAgentRecoveryError(
                "Draft Agent result materialization requires its durable completed receipt"
            )
        result_reference = await self._load_result_reference(record)
        if (
            reference_key != _agent_result_reference_key(record.agent_run_id)
            or result_reference.digest != result_sha256
        ):
            raise DraftAgentResultUnavailable(
                "Draft Agent result cursor does not match its durable CAS reference"
            )
        cas = getattr(self.agent_checkpointer, "cas", None)
        if not isinstance(cas, FileHostCAS):
            raise DraftAgentResultUnavailable(
                "Draft Agent result materialization needs the owner-bound CAS reader"
            )
        body = await asyncio.to_thread(cas.read, result_reference)
        result = _parse_draft_agent_result(context.logical_task_key, body)
        owner_receipt = await self.owner.materialize_agent_result(
            record,
            receipt,
            result_reference,
            result,
            trial_group_paths=context.trial_group_paths,
            trial_revision_id=context.trial_revision_id,
        )
        return {
            "cursor": "DRAFT_AGENT_COMPLETED",
            "agent_run_id": agent_run_id,
            "agent_receipt_id": receipt_id,
            "result_reference_key": reference_key,
            "result_sha256": result_sha256,
            "owner_result_receipt_key": owner_receipt.receipt_key,
        }

    async def _run_agent_graph(
        self,
        logical_task_key: str,
        task: str,
        expected_category: str,
        *,
        trial_group_paths: tuple[str, ...] | None = None,
        trial_revision_id: str | None = None,
    ) -> DraftAgentCompletion:
        await self._ensure_open()
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Draft Agent task must be non-empty text")
        snapshot = await self._graph.aget_state(self.config)
        if snapshot is not None and _has_interrupt(snapshot):
            raise DraftConflictError("Draft graph is waiting for an AskUser answer")
        context = _DraftAgentContext(
            logical_task_key=logical_task_key,
            expected_category=expected_category,
            task=task,
            trial_group_paths=trial_group_paths,
            trial_revision_id=trial_revision_id,
        )
        state = await self._graph.ainvoke(
            {"draft_id": str(self.owner.draft_id), "cursor": "DRAFT_AGENT"},
            config=self.config,
            context=context,
        )
        return await self._load_materialized_completion(state)

    async def _load_materialized_completion(
        self, state: Mapping[str, object]
    ) -> DraftAgentCompletion:
        agent_run_id = state.get("agent_run_id")
        agent_receipt_id = state.get("agent_receipt_id")
        reference_key = state.get("result_reference_key")
        result_sha256 = state.get("result_sha256")
        owner_receipt_key = state.get("owner_result_receipt_key")
        if (
            not isinstance(agent_run_id, str)
            or not isinstance(agent_receipt_id, str)
            or not isinstance(reference_key, str)
            or not isinstance(result_sha256, str)
            or not isinstance(owner_receipt_key, str)
        ):
            raise DraftAgentRecoveryError("Draft graph did not return complete receipt references")
        run_id = AgentRunId(UUID(agent_run_id))
        record = await self.agent_runs.load_agent_run(run_id)
        receipt = await self.agent_runs.load_agent_run_receipt(run_id)
        result_reference = await self.agent_runs.get_cas_reference(
            "draft", self.owner.draft_id, reference_key
        )
        owner_fact = await self.owner.load_fact(owner_receipt_key)
        if (
            record is None
            or receipt is None
            or result_reference is None
            or owner_fact is None
            or str(receipt.receipt_id) != agent_receipt_id
            or record.result_sha256 != result_sha256
            or result_reference.digest != result_sha256
            or owner_fact[0].receipt_key != owner_receipt_key
            or owner_fact[0].category != "draft.agent.result"
            or owner_fact[1].get("agent_receipt_id") != agent_receipt_id
            or owner_fact[1].get("result_sha256") != result_sha256
        ):
            raise DraftAgentRecoveryError("Draft graph receipt references failed revalidation")
        materialized, logical_task_key, fact_run_id = _agent_result_from_fact(
            owner_fact[0], owner_fact[1]
        )
        if fact_run_id != run_id or record.logical_task_key != logical_task_key:
            raise DraftAgentRecoveryError("Draft result fact has a different AgentRun identity")
        return DraftAgentCompletion(record, receipt, result_reference, materialized)

    async def _run_agent(
        self, logical_task_key: str, task: str, expected_category: str
    ) -> DraftAgentCompletion:
        await self._ensure_open()
        if self.agent_runner is None:
            raise RuntimeError("Draft graph has no AgentRun runner")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Draft Agent task must be non-empty text")
        existing = await self._find_agent_run(logical_task_key)
        if existing is not None and existing.is_terminal:
            return await self._recover_terminal_agent(existing, logical_task_key, expected_category)
        lifecycle = _DraftAgentLifecycle(self, logical_task_key, expected_category)
        completion = await self.agent_runner.run(
            self.owner.draft_id, logical_task_key, task, lifecycle=lifecycle
        )
        if not lifecycle.started_seen or not lifecycle.terminal_seen:
            raise ValueError("Draft AgentRun runner omitted a lifecycle callback")
        record = completion.record
        self._validate_agent_identity(record, logical_task_key)
        if (
            not record.is_terminal
            or record.exit is None
            or completion.receipt.agent_run_id != record.agent_run_id
        ):
            raise ValueError("Draft AgentRun completion identity is invalid")
        expected_state, expected_receipt_category, outcome = _agent_terminal_contract(
            record, expected_category
        )
        if record.state is not expected_state:
            raise ValueError(f"Draft AgentRun {outcome} state is invalid")
        if completion.receipt.category != expected_receipt_category:
            raise ValueError(f"Draft AgentRun {outcome} category is invalid")
        persisted_record = await self.agent_runs.load_agent_run(record.agent_run_id)
        persisted_receipt = await self.agent_runs.load_agent_run_receipt(record.agent_run_id)
        if persisted_record != record or persisted_receipt != completion.receipt:
            raise ValueError("Draft AgentRun cannot advance without its durable receipt")
        if record.exit in {SessionExit.Failed, SessionExit.BudgetExhausted}:
            raise DraftAgentExecutionFailed(record.exit)
        if record.exit is not SessionExit.Completed:
            raise DraftAgentExecutionTerminated(record.exit)
        result_reference = await self.agent_runs.get_cas_reference(
            "draft", self.owner.draft_id, _agent_result_reference_key(record.agent_run_id)
        )
        if (
            not isinstance(completion.result, CasObject)
            or record.result_sha256 is None
            or completion.result.digest != record.result_sha256
            or result_reference != completion.result
        ):
            raise DraftAgentResultUnavailable(
                "Draft AgentRun completion lacks its matching durable CAS result reference"
            )
        return completion

    async def _find_agent_run(self, logical_task_key: str) -> AgentRun | None:
        matches = tuple(
            record
            for record in await self.agent_runs.list_agent_runs_by_owner(
                "draft", self.owner.draft_id
            )
            if record.logical_task_key == logical_task_key
        )
        if len(matches) > 1:
            raise DraftAgentRecoveryError("Draft AgentRun logical task has conflicting records")
        return matches[0] if matches else None

    async def _recover_terminal_agent(
        self,
        record: AgentRun,
        logical_task_key: str,
        expected_category: str,
    ) -> DraftAgentCompletion:
        self._validate_agent_identity(record, logical_task_key)
        persisted_record = await self.agent_runs.load_agent_run(record.agent_run_id)
        if persisted_record != record:
            raise DraftAgentRecoveryError("Draft AgentRun terminal record changed during recovery")
        if not await self._has_matching_agent_start_fact(record):
            raise DraftAgentRecoveryError(
                "Draft AgentRun terminal recovery requires its durable start receipt"
            )
        receipt = await self.agent_runs.load_agent_run_receipt(record.agent_run_id)
        if receipt is None or receipt.agent_run_id != record.agent_run_id:
            raise DraftAgentRecoveryError(
                "Draft AgentRun terminal receipt is missing or mismatched"
            )
        try:
            expected_state, expected_receipt_category, outcome = _agent_terminal_contract(
                record, expected_category
            )
        except ValueError as exc:
            raise DraftAgentRecoveryError(str(exc)) from exc
        if record.state is not expected_state:
            raise DraftAgentRecoveryError(f"Draft AgentRun {outcome} state is inconsistent")
        if receipt.category != expected_receipt_category:
            raise DraftAgentRecoveryError(
                f"Draft AgentRun {outcome} category does not match its owner task"
            )
        if record.exit is SessionExit.Completed:
            result_reference = await self._load_result_reference(record)
        else:
            result_reference = None

        lifecycle = _DraftAgentLifecycle(self, logical_task_key, expected_category)
        await lifecycle.started(record)
        await lifecycle.terminal(record, receipt)
        if record.exit in {SessionExit.Failed, SessionExit.BudgetExhausted}:
            raise DraftAgentExecutionFailed(record.exit or SessionExit.Failed)
        if record.exit is not SessionExit.Completed:
            raise DraftAgentExecutionTerminated(record.exit or SessionExit.Failed)
        if result_reference is None:
            raise DraftAgentResultUnavailable(
                "Draft AgentRun terminal receipt has no matching durable CAS result reference"
            )
        return DraftAgentCompletion(record, receipt, result_reference)

    async def _commit_agent_started(self, record: AgentRun, logical_task_key: str) -> None:
        self._validate_agent_identity(record, logical_task_key)
        persisted = await self.agent_runs.load_agent_run(record.agent_run_id)
        if persisted != record:
            raise ValueError("Draft AgentRun start lacks its durable identity")
        already_published = await self._has_matching_agent_start_fact(record)
        if record.state is SessionState.Created and record.exit is None:
            await self.owner.commit_agent_started(record)
            return
        if record.is_terminal and already_published:
            return
        if record.is_terminal:
            raise DraftAgentRecoveryError(
                "Draft AgentRun terminal start replay requires its durable start receipt"
            )
        raise ValueError("Draft AgentRun start requires a created record")

    async def _commit_agent_terminal(
        self,
        record: AgentRun,
        receipt: AgentRunReceipt,
        logical_task_key: str,
        expected_category: str,
    ) -> None:
        self._validate_agent_identity(record, logical_task_key)
        if (
            not record.is_terminal
            or record.exit is None
            or receipt.agent_run_id != record.agent_run_id
        ):
            raise ValueError("Draft AgentRun terminal identity is invalid")
        expected_state, expected_receipt_category, outcome = _agent_terminal_contract(
            record, expected_category
        )
        if record.state is not expected_state:
            raise ValueError(f"Draft AgentRun {outcome} state is invalid")
        if receipt.category != expected_receipt_category:
            raise ValueError(f"Draft AgentRun {outcome} category is invalid")
        if not await self._has_matching_agent_start_fact(record):
            raise DraftAgentRecoveryError(
                "Draft AgentRun terminal requires its durable start receipt"
            )
        persisted = await self.agent_runs.load_agent_run(record.agent_run_id)
        persisted_receipt = await self.agent_runs.load_agent_run_receipt(record.agent_run_id)
        if persisted != record or persisted_receipt != receipt:
            raise ValueError("Draft AgentRun terminal lacks its durable receipt")
        if record.exit is SessionExit.Completed:
            await self._load_result_reference(record)
        await self.owner.commit_agent_terminal(record, receipt)

    async def _load_result_reference(self, record: AgentRun) -> CasObject:
        result_reference = await self.agent_runs.get_cas_reference(
            "draft", self.owner.draft_id, _agent_result_reference_key(record.agent_run_id)
        )
        if (
            record.result_sha256 is None
            or result_reference is None
            or result_reference.digest != record.result_sha256
        ):
            raise DraftAgentResultUnavailable(
                "Draft AgentRun terminal receipt has no matching durable CAS result reference"
            )
        return result_reference

    async def _has_matching_agent_start_fact(self, record: AgentRun) -> bool:
        key = f"draft.agent.started:{record.agent_run_id}"
        persisted = await self.owner.load_fact(key)
        if persisted is None:
            return False
        receipt, fact = persisted
        if (
            receipt.draft_id != self.owner.draft_id
            or receipt.receipt_key != key
            or receipt.category != "draft.agent.started"
            or fact != {"agent_run_id": str(record.agent_run_id)}
        ):
            raise DraftAgentRecoveryError(
                "Draft AgentRun start owner fact conflicts with its identity"
            )
        return True

    def _validate_agent_identity(self, record: AgentRun, logical_task_key: str) -> None:
        if (
            record.owner_kind != "draft"
            or record.owner_id != self.owner.draft_id
            or record.logical_task_key != logical_task_key
            or record.thread_id == self.thread_id
        ):
            raise ValueError("Draft AgentRun lifecycle has a different owner task")

    async def _ensure_open(self) -> None:
        if await self.owner.has_receipt("draft.closed") or await self.owner.has_receipt(
            "draft.attached"
        ):
            raise DraftConflictError("Draft session is already closed or attached to a Run")

    async def _release_owned_threads(self) -> None:
        records = await self.agent_runs.list_agent_runs_by_owner("draft", self.owner.draft_id)
        for record in records:
            await self.agent_checkpointer.adelete_thread(record.thread_id)
        await self.checkpointer.adelete_thread(self.thread_id)


def exploration_task_key(domain_path: str) -> str:
    return f"draft.explore:{_key_digest(_normalize_path(domain_path))}"


def coordinator_task_key(round_number: int = 1) -> str:
    if type(round_number) is not int or round_number < 1:
        raise ValueError("coordinator round number must be a positive integer")
    return f"draft.explore.coordinator:round:{round_number}"


def trial_translation_task_key(file_path: str, revision_id: str | UUID) -> str:
    return f"draft.trial:{revision_id}:{_key_digest(_normalize_path(file_path))}"


def _coordinator_task_round(logical_task_key: str) -> int | None:
    prefix = "draft.explore.coordinator:round:"
    if not logical_task_key.startswith(prefix):
        return None
    raw_round = logical_task_key.removeprefix(prefix)
    if not raw_round.isdecimal() or raw_round.startswith("0"):
        return None
    return int(raw_round)


def _normalize_trial_group(paths: Sequence[str] | None) -> tuple[str, ...]:
    if paths is None:
        raise DraftAgentResultInvalid("trial result is missing its selected file group")
    try:
        selected = tuple(select_trial_paths(paths))
    except (TypeError, ValueError) as exc:
        raise DraftAgentResultInvalid("trial result has an invalid selected file group") from exc
    if len(selected) not in {2, 3}:
        raise DraftAgentResultInvalid("trial group must contain exactly two or three files")
    return selected


def _revision_receipt_key(revision_number: int) -> str:
    return f"draft.revision:{revision_number}"


def _question_receipt_key(question_id: object) -> str:
    return f"draft.question:{question_id}"


def _answer_receipt_key(question_id: object) -> str:
    return f"draft.answer:{question_id}"


def _agent_result_reference_key(agent_run_id: AgentRunId) -> str:
    return f"agent-result:{agent_run_id}"


def _parse_draft_agent_result(logical_task_key: str, body: bytes) -> DraftAgentResult:
    if logical_task_key.startswith("draft.explore:"):
        models: tuple[type[DraftAgentResult], ...] = (ExplorationReport,)
    elif _coordinator_task_round(logical_task_key) is not None:
        models = (ExploreReassignment, ExplorationMerge)
    elif logical_task_key.startswith("draft.trial:"):
        models = (TrialTranslation,)
    else:
        raise DraftAgentResultInvalid("Draft AgentRun has an unsupported logical task")

    errors: list[ValidationError] = []
    for model in models:
        try:
            return model.model_validate_json(body)
        except ValidationError as exc:
            errors.append(exc)
    raise DraftAgentResultInvalid(
        "Draft AgentRun CAS body does not match a typed Draft result"
    ) from errors[-1]


def _agent_result_from_fact(
    receipt: DraftOwnerReceipt,
    fact: Mapping[str, object],
) -> tuple[DraftAgentResult, str, UUID]:
    agent_run_value = fact.get("agent_run_id")
    logical_task_value = fact.get("logical_task_key")
    result_digest = fact.get("result_sha256")
    result_type = fact.get("result_type")
    result_body = fact.get("result")
    if (
        receipt.category != "draft.agent.result"
        or not isinstance(agent_run_value, str)
        or not isinstance(logical_task_value, str)
        or not isinstance(result_digest, str)
        or not isinstance(result_type, str)
        or not isinstance(result_body, Mapping)
    ):
        raise StoreCommitError("stored Draft AgentRun result fact is invalid")
    result_types = {
        model.__name__: model
        for model in (
            ExplorationReport,
            ExploreReassignment,
            ExplorationMerge,
            TrialTranslation,
        )
    }
    model = result_types.get(result_type)
    if model is None:
        raise StoreCommitError("stored Draft AgentRun result has an unknown type")
    try:
        agent_run_id = UUID(agent_run_value)
        result = model.model_validate(result_body)
    except (TypeError, ValueError) as exc:
        raise StoreCommitError("stored Draft AgentRun result is invalid") from exc
    if len(result_digest) != 64 or any(c not in "0123456789abcdef" for c in result_digest):
        raise StoreCommitError("stored Draft AgentRun result digest is invalid")
    if receipt.receipt_key != f"draft.agent.result:{agent_run_id}":
        raise StoreCommitError("Draft AgentRun result receipt key is inconsistent")
    return result, logical_task_value, agent_run_id


def _agent_result_restore_key(
    item: tuple[DraftOwnerReceipt, dict[str, object]],
) -> tuple[int, int, str, str]:
    receipt, fact = item
    result_type = fact.get("result_type")
    result_body = fact.get("result")
    priorities = {
        "ExplorationReport": 0,
        "ExploreReassignment": 1,
        "ExplorationMerge": 2,
        "TrialTranslation": 4,
    }
    if not isinstance(result_body, Mapping):
        return (len(priorities), 0, "", receipt.receipt_key)
    sort_value = ""
    round_number = 0
    if result_type == "ExplorationReport":
        domain_path = result_body.get("domain_path")
        sort_value = domain_path if isinstance(domain_path, str) else ""
    elif result_type == "TrialTranslation":
        file_path = result_body.get("file_path")
        sort_value = file_path if isinstance(file_path, str) else ""
    elif result_type in {"ExploreReassignment", "ExplorationMerge"}:
        logical_task_key = fact.get("logical_task_key")
        if isinstance(logical_task_key, str):
            round_number = _coordinator_task_round(logical_task_key) or 0
    return (
        priorities.get(result_type, len(priorities))
        if isinstance(result_type, str)
        else len(priorities),
        round_number,
        sort_value,
        receipt.receipt_key,
    )


def _agent_terminal_contract(
    record: AgentRun, expected_completion_category: str
) -> tuple[SessionState, str, str]:
    task_base, separator, suffix = expected_completion_category.rpartition(".")
    if not separator or not task_base or suffix != "completed":
        raise ValueError("Draft AgentRun expected category must end in .completed")
    if record.exit is SessionExit.Completed:
        return SessionState.Closed, expected_completion_category, "completion"
    if record.exit in {SessionExit.Failed, SessionExit.BudgetExhausted}:
        return SessionState.Failed, f"{task_base}.failed", "failure"
    if record.exit is SessionExit.SegmentStopped:
        return SessionState.Closed, f"{task_base}.segment_stopped", "segment-stopped"
    if record.exit is SessionExit.Invalidated:
        return SessionState.Invalidated, f"{task_base}.invalidated", "invalidated"
    raise ValueError("Draft AgentRun terminal exit is unsupported")


def _key_digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Draft logical task identity must be non-empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_path(value: str) -> str:
    if value == ".":
        return value
    normalized = normalize_repo_relative_paths([value])
    if len(normalized) != 1:
        raise ValueError("Draft Agent domain must normalize to exactly one path")
    return normalized[0]


def _has_interrupt(snapshot: object) -> bool:
    tasks = getattr(snapshot, "tasks", ())
    return any(bool(getattr(task, "interrupts", ())) for task in tasks)


__all__ = [
    "DraftAgentCompletion",
    "DraftAgentExecutionTerminated",
    "DraftAgentExecutionFailed",
    "DraftAgentResultInvalid",
    "DraftAgentRecoveryError",
    "DraftAgentResultUnavailable",
    "DraftAgentRunnerPort",
    "DraftFlowOwner",
    "DraftOwnerPort",
    "MigrationSessionGraph",
    "coordinator_task_key",
    "exploration_task_key",
    "trial_translation_task_key",
]
