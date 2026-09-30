"""Persistent pre-Run Draft graph and Draft-owned receipt boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict
from uuid import UUID, uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt

from codemigrator.core import CreateRun, RunId, canonical_json_bytes
from codemigrator.core.paths import normalize_repo_relative_paths

from .agent_runs import AgentRun, AgentRunId, AgentRunReceipt
from .contracts import DraftOwnerReceipt, RunCreatedReceipt
from .create_run import CreateRunService
from .draft import DraftConflictError, DraftFlow
from .draft_models import AskUserAnswer, AskUserQuestion, DraftFreezeReceipt
from .loop_contracts import SessionExit, SessionState
from .store import RuntimeStore, StoreCommitError


class _DraftGraphState(TypedDict, total=False):
    """Only owner IDs, graph cursor and committed receipt references belong here."""

    draft_id: str
    cursor: str
    question_id: str
    question_receipt_key: str
    answer_receipt_key: str


class DraftOwnerPort(Protocol):
    draft_id: UUID
    freeze_receipt: DraftFreezeReceipt | None

    async def load_fact(
        self, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None: ...

    async def commit_question(self, question: AskUserQuestion) -> DraftOwnerReceipt: ...

    async def load_question(self, question_id: str) -> AskUserQuestion | None: ...

    async def load_answer_receipt(self, answer: AskUserAnswer) -> DraftOwnerReceipt | None: ...

    async def commit_answer(self, answer: AskUserAnswer) -> DraftOwnerReceipt: ...

    async def has_receipt(self, receipt_key: str) -> bool: ...

    async def commit_lifecycle_fact(
        self, receipt_key: str, category: str, fact: Mapping[str, object]
    ) -> DraftOwnerReceipt: ...


class DraftFlowOwner:
    """Commit DraftFlow questions and answers behind durable Draft receipts."""

    def __init__(self, *, draft_id: UUID, flow: DraftFlow, store: RuntimeStore) -> None:
        self.draft_id = draft_id
        self.flow = flow
        self.store = store

    @property
    def freeze_receipt(self) -> DraftFreezeReceipt | None:
        return self.flow.ledger.freeze_receipt

    async def load_fact(
        self, receipt_key: str
    ) -> tuple[DraftOwnerReceipt, dict[str, object]] | None:
        return await self.store.load_draft_owner_fact(self.draft_id, receipt_key)

    async def commit_question(self, question: AskUserQuestion) -> DraftOwnerReceipt:
        if self.flow.ledger.current_revision is None or (
            self.flow.ledger.current_revision.revision_id != question.revision_id
        ):
            raise DraftConflictError("AskUser question must target the current Draft revision")
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
        return await self.store.commit_draft_owner_fact(
            self.draft_id, receipt_key, "draft.ask_user.question", body
        )

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
        return await self.store.commit_draft_owner_fact(
            self.draft_id,
            _answer_receipt_key(answer.question_id),
            "draft.ask_user.answer",
            answer.model_dump(mode="json"),
        )

    async def has_receipt(self, receipt_key: str) -> bool:
        return await self.store.load_draft_owner_fact(self.draft_id, receipt_key) is not None

    async def commit_lifecycle_fact(
        self, receipt_key: str, category: str, fact: Mapping[str, object]
    ) -> DraftOwnerReceipt:
        return await self.store.commit_draft_owner_fact(self.draft_id, receipt_key, category, fact)


class DraftAgentRunStore(Protocol):
    async def create_or_get_agent_run(self, record: AgentRun) -> AgentRun: ...

    async def load_agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None: ...

    async def load_agent_run_receipt(self, agent_run_id: AgentRunId) -> AgentRunReceipt | None: ...

    async def list_agent_runs_by_owner(
        self, owner_kind: str, owner_id: UUID
    ) -> tuple[AgentRun, ...]: ...


@dataclass(frozen=True, slots=True)
class DraftAgentCompletion:
    """A model result whose AgentRun and completion receipt are already durable."""

    record: AgentRun
    receipt: AgentRunReceipt
    result: object


class DraftAgentRunnerPort(Protocol):
    async def run(
        self, draft_id: UUID, logical_task_key: str, task: str
    ) -> DraftAgentCompletion: ...


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
        builder = StateGraph(_DraftGraphState)
        builder.add_node("ask_user", self._ask_user)
        builder.set_entry_point("ask_user")
        builder.add_edge("ask_user", END)
        self._graph = builder.compile(checkpointer=checkpointer)

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.thread_id}}

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
        return await self._run_agent(
            exploration_task_key(domain_path), task, "draft.exploration.completed"
        )

    async def coordinate_exploration(self, task: str) -> DraftAgentCompletion:
        return await self._run_agent(coordinator_task_key(), task, "draft.coordinator.completed")

    async def trial_translate(self, file_path: str, task: str) -> DraftAgentCompletion:
        return await self._run_agent(
            trial_translation_task_key(file_path), task, "draft.trial.completed"
        )

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

    async def _run_agent(
        self, logical_task_key: str, task: str, expected_category: str
    ) -> DraftAgentCompletion:
        await self._ensure_open()
        if self.agent_runner is None:
            raise RuntimeError("Draft graph has no AgentRun runner")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Draft Agent task must be non-empty text")
        completion = await self.agent_runner.run(self.owner.draft_id, logical_task_key, task)
        record = completion.record
        if (
            record.owner_kind != "draft"
            or record.owner_id != self.owner.draft_id
            or record.logical_task_key != logical_task_key
            or record.thread_id == self.thread_id
            or record.state is not SessionState.Closed
            or record.exit is not SessionExit.Completed
            or completion.receipt.agent_run_id != record.agent_run_id
            or completion.receipt.category != expected_category
        ):
            raise ValueError("Draft AgentRun completion does not match its owner task")
        persisted_record = await self.agent_runs.load_agent_run(record.agent_run_id)
        persisted_receipt = await self.agent_runs.load_agent_run_receipt(record.agent_run_id)
        if persisted_record != record or persisted_receipt != completion.receipt:
            raise ValueError("Draft AgentRun cannot advance without its durable receipt")
        return completion

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


def coordinator_task_key() -> str:
    return "draft.explore.coordinator"


def trial_translation_task_key(file_path: str) -> str:
    return f"draft.trial:{_key_digest(_normalize_path(file_path))}"


def _question_receipt_key(question_id: object) -> str:
    return f"draft.question:{question_id}"


def _answer_receipt_key(question_id: object) -> str:
    return f"draft.answer:{question_id}"


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
    "DraftAgentRunnerPort",
    "DraftFlowOwner",
    "DraftOwnerPort",
    "MigrationSessionGraph",
    "coordinator_task_key",
    "exploration_task_key",
    "trial_translation_task_key",
]
