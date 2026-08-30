"""Unified, fact-only Context Manager for all CodeMigrator sessions.

This module owns context assembly and budget governance. Provider tokenizers,
tool execution, CAS persistence, and event schemas remain ports owned by their
respective modules.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, cast

from pydantic import BaseModel

from codemigrator.core import (
    ArtifactRef,
    CheckAction,
    CheckFeedbackSummary,
    CheckpointSummary,
    ContextPack,
    ContextPackIdentity,
    GitOid,
    RecoveryBrief,
    SegmentProgressSummary,
    SessionBudgetProfile,
    SessionKind,
    Sha256,
    SliceGenerationRef,
    canonical_json_bytes,
    load_resource,
    load_session_budget,
)

from .context import ContextEnvelope, ContextSegment, PromptMessage, render_prompt
from .templates import StaticTemplateCatalog

MAX_CONTEXT_BLOCK_BYTES = 256 * 1024
MAX_AST_MATCHES = 200
CAS_URI_PATTERN = re.compile(r"cas://[0-9a-fA-F]{64}\Z")


class ContextBudgetError(ValueError):
    """A context request was rejected without truncating required semantics."""

    def __init__(self, message: str, *, code: str = "CONTEXT_BUDGET_EXCEEDED") -> None:
        super().__init__(message)
        self.code = code


class TokenCounter(Protocol):
    """Provider-owned exact tokenizer port; character estimates are forbidden."""

    def count(self, messages: Sequence[PromptMessage]) -> int: ...


class NetInputCap(Protocol):
    """Provider binding port for the physical net-input window."""

    def compute(
        self,
        *,
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> int: ...


class FormulaNetInputCap:
    """The provider-independent physical-window formula."""

    def compute(
        self,
        *,
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> int:
        values = (context_window, reserved_output, tool_schema_tokens, envelope_margin)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("physical context values must be non-negative integers")
        cap = context_window - reserved_output - tool_schema_tokens - envelope_margin
        if cap <= 0:
            raise ValueError("physical net-input cap must be positive")
        return cap


@dataclass(frozen=True, slots=True)
class DraftingBudgetProfile:
    """The pre-CreateRun Drafting slot, which has no SessionKind identity."""

    session: str
    max_rounds: int
    eviction_watermark_pct: int


BudgetProfile = SessionBudgetProfile | DraftingBudgetProfile
CasRefValidator = Callable[[str, object], bool]
EvictionAuditSink = Callable[[tuple["EvictionAudit", ...]], None]


@dataclass(frozen=True, slots=True, init=False)
class SessionBudgetCatalog:
    """Immutable, exact projection of ``core://session-budget/v1``."""

    profiles: Mapping[str, BudgetProfile]
    resource_sha256: str

    def __init__(
        self,
        profiles: Mapping[str, BudgetProfile],
        resource_sha256: str = "",
    ) -> None:
        expected = {member.value for member in SessionKind} | {"DRAFTING"}
        if set(profiles) != expected:
            raise ValueError("session budget catalog must contain exactly ten slots")
        if any(
            type(profile.max_rounds) is not int
            or profile.max_rounds < 1
            or type(profile.eviction_watermark_pct) is not int
            or not 1 <= profile.eviction_watermark_pct <= 100
            for profile in profiles.values()
        ):
            raise ValueError("session budget values are invalid")
        object.__setattr__(self, "profiles", MappingProxyType(dict(profiles)))
        object.__setattr__(self, "resource_sha256", resource_sha256)

    @classmethod
    def from_core(cls) -> SessionBudgetCatalog:
        resource = load_resource("core://session-budget/v1")
        payload = load_session_budget()
        profiles: dict[str, BudgetProfile] = {}
        for name, values in payload.items():
            if name == "DRAFTING":
                profiles[name] = DraftingBudgetProfile(session=name, **values)
            else:
                profiles[name] = SessionBudgetProfile(session=SessionKind(name), **values)
        return cls(profiles, resource.sha256)

    def profile(self, session: SessionKind | str) -> BudgetProfile:
        key = session.value if isinstance(session, SessionKind) else session
        try:
            return self.profiles[key]
        except KeyError as exc:
            raise ValueError("unknown session budget slot") from exc

    def to_mapping(self) -> dict[str, dict[str, int]]:
        return {
            name: {
                "max_rounds": profile.max_rounds,
                "eviction_watermark_pct": profile.eviction_watermark_pct,
            }
            for name, profile in self.profiles.items()
        }


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    pack: ContextPack
    envelope: ContextEnvelope
    messages: tuple[PromptMessage, ...]
    snapshot: Mapping[str, object] | None = None


class ContextPackCache:
    """A run-scoped cache keyed by every frozen identity input."""

    def __init__(self) -> None:
        self._packs: dict[str, ContextPack] = {}
        self._run_keys: dict[object, set[str]] = {}

    @staticmethod
    def key(identity: ContextPackIdentity, *, contract_refs_sha256: str | None = None) -> str:
        payload = identity.model_dump(mode="json", by_alias=True)
        payload["contract_refs_sha256"] = contract_refs_sha256 or identity.contract_refs_sha256
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def get(
        self, identity: ContextPackIdentity, *, contract_refs_sha256: str | None = None
    ) -> ContextPack | None:
        pack = self._packs.get(self.key(identity, contract_refs_sha256=contract_refs_sha256))
        return pack if pack is not None and pack.identity == identity else None

    def put(
        self,
        identity: ContextPackIdentity,
        pack: ContextPack,
        *,
        contract_refs_sha256: str | None = None,
    ) -> None:
        if pack.identity != identity:
            raise ValueError("context pack cache identity does not match pack")
        if pack.identity.run_id != identity.run_id:
            raise ValueError("context pack cache cannot cross Run boundaries")
        cache_key = self.key(identity, contract_refs_sha256=contract_refs_sha256)
        for old_key in self._run_keys.get(identity.run_id, set()):
            if old_key != cache_key:
                self._packs.pop(old_key, None)
        self._packs[cache_key] = pack
        self._run_keys.setdefault(identity.run_id, set()).clear()
        self._run_keys[identity.run_id].add(cache_key)

    def __len__(self) -> int:
        return len(self._packs)


class ContextManager:
    """Assemble and govern every session type through one implementation."""

    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        net_input_cap: NetInputCap | None = None,
        budget_catalog: SessionBudgetCatalog | None = None,
        template_catalog: StaticTemplateCatalog | None = None,
        cache: ContextPackCache | None = None,
    ) -> None:
        self.token_counter = token_counter
        self.net_input_cap = net_input_cap
        self.budget_catalog = budget_catalog or SessionBudgetCatalog.from_core()
        self.template_catalog = template_catalog or StaticTemplateCatalog.from_core()
        self.cache = cache or ContextPackCache()

    def fit(
        self,
        *,
        identity: ContextPackIdentity,
        template: str | None = None,
        envelope: ContextEnvelope,
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> ContextAssembly:
        if any(segment.source_body for segment in self._segments(envelope)):
            raise ValueError("initial context pack must not contain source body")
        template_text = (
            template
            if template is not None
            else self.template_catalog.template(identity.session.value)
        )
        if not template_text:
            raise ValueError("session template must not be empty")
        frozen_identity = self._freeze_template(identity, template_text)
        budget = self.budget_catalog.profile(frozen_identity.session)
        if not isinstance(budget, SessionBudgetProfile):
            raise ValueError("DRAFTING is not a Run ContextPack session")
        messages = render_prompt(template_text, envelope)
        count, cap = self._measure(
            messages,
            context_window=context_window,
            reserved_output=reserved_output,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=envelope_margin,
        )
        if count > cap:
            raise ContextBudgetError("context exceeds the locked physical net-input cap")
        pack = ContextPack(identity=frozen_identity, budget=budget, assembled_tokens=count)
        self.cache.put(frozen_identity, pack)
        return ContextAssembly(pack=pack, envelope=envelope, messages=messages)

    assemble = fit

    def fit_triggered(
        self,
        *,
        identity: ContextPackIdentity,
        template: str | None = None,
        stable: Sequence[ContextSegment] = (),
        evolving: Sequence[ContextSegment] = (),
        snapshot: Mapping[str, object],
        event_projection: Sequence[str],
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> ContextAssembly:
        if any(not isinstance(item, str) or not item.strip() for item in event_projection):
            raise ValueError("event projection must contain non-empty text")
        envelope = ContextEnvelope(
            stable=tuple(stable),
            evolving=tuple(evolving),
            targeted=(
                ContextSegment(
                    "targeted",
                    "\n".join(event_projection),
                    required=True,
                    evictable=False,
                    source_ref="run_events:targeted-projection",
                ),
            ),
        )
        assembly = self.fit(
            identity=identity,
            template=template,
            envelope=envelope,
            context_window=context_window,
            reserved_output=reserved_output,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=envelope_margin,
        )
        return ContextAssembly(
            pack=assembly.pack,
            envelope=assembly.envelope,
            messages=assembly.messages,
            snapshot=MappingProxyType(dict(snapshot)),
        )

    assemble_triggered = fit_triggered

    @staticmethod
    def append_evolution(
        envelope: ContextEnvelope, summary_text: str, *, slice_ref: str
    ) -> ContextEnvelope:
        if not isinstance(summary_text, str) or not summary_text.strip():
            raise ValueError("evolution summary must be non-empty text")
        return ContextEnvelope(
            stable=envelope.stable,
            evolving=(
                *envelope.evolving,
                ContextSegment(
                    "evolving",
                    summary_text,
                    required=True,
                    evictable=False,
                    source_ref=f"slice:{slice_ref}",
                ),
            ),
            targeted=envelope.targeted,
        )

    @staticmethod
    def append_targeted(envelope: ContextEnvelope, segment: ContextSegment) -> ContextEnvelope:
        if segment.kind != "targeted":
            raise ValueError("only targeted segments can be appended at runtime")
        return ContextEnvelope(
            stable=envelope.stable,
            evolving=envelope.evolving,
            targeted=(*envelope.targeted, segment),
        )

    @staticmethod
    def segment_from_block(
        block: ContextDataBlock, *, required: bool = False, turn_index: int | None = None
    ) -> ContextSegment:
        """Convert one governed tool block into a runtime-targeted segment."""

        source_body = block.kind is DataBlockKind.SourceFile
        source_ref = block.range_hint or (
            str(block.artifact_ref) if block.artifact_ref is not None else None
        )
        return ContextSegment(
            "targeted",
            block.text,
            required=required,
            evictable=not required,
            source_body=source_body,
            source_ref=source_ref,
            turn_index=turn_index,
        )

    def fit_messages(
        self,
        messages: Sequence[PromptMessage],
        *,
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> int:
        """Measure an already-rendered prompt through the exact provider port."""

        count, cap = self._measure(
            tuple(messages),
            context_window=context_window,
            reserved_output=reserved_output,
            tool_schema_tokens=tool_schema_tokens,
            envelope_margin=envelope_margin,
        )
        if count > cap:
            raise ContextBudgetError("context exceeds the locked physical net-input cap")
        return count

    @staticmethod
    def _segments(envelope: ContextEnvelope) -> tuple[ContextSegment, ...]:
        return (*envelope.stable, *envelope.evolving, *envelope.targeted)

    def _freeze_template(
        self, identity: ContextPackIdentity, template_text: str
    ) -> ContextPackIdentity:
        digest = hashlib.sha256(
            canonical_json_bytes(
                {"session": identity.session.value, "template": template_text}
            )
        ).hexdigest()
        if identity.template_sha256 == "0" * 64:
            return identity.model_copy(update={"template_sha256": digest})
        if identity.template_sha256 != digest:
            raise ValueError("context identity template digest is not frozen catalog digest")
        return identity

    def _measure(
        self,
        messages: tuple[PromptMessage, ...],
        *,
        context_window: int,
        reserved_output: int,
        tool_schema_tokens: int,
        envelope_margin: int,
    ) -> tuple[int, int]:
        if self.token_counter is None or self.net_input_cap is None:
            raise ContextBudgetError(
                "exact provider tokenizer and net-input cap are required",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        try:
            count = self.token_counter.count(messages)
            cap = self.net_input_cap.compute(
                context_window=context_window,
                reserved_output=reserved_output,
                tool_schema_tokens=tool_schema_tokens,
                envelope_margin=envelope_margin,
            )
        except (TypeError, ValueError) as exc:
            raise ContextBudgetError(
                "locked provider context capability is invalid",
                code="CONTEXT_CAPABILITY_INVALID",
            ) from exc
        if type(count) is not int or count < 0 or type(cap) is not int or cap <= 0:
            raise ContextBudgetError(
                "locked provider context capability is invalid",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        return count, cap


@dataclass(frozen=True, slots=True)
class EvolutionSegment:
    run_id: object
    entry_index: int
    slice_id: object
    summary_text: str
    template_sha256: str


@dataclass(frozen=True, slots=True)
class EvolutionSegmentDraft:
    """An evolution entry staged with the state/events transaction."""

    run_id: object
    slice_id: object
    summary_text: str
    template_sha256: str


class InMemoryEvolutionSegmentStore:
    """Append-only test adapter mirroring the runtime SQL contract."""

    def __init__(self) -> None:
        self._entries: dict[object, list[EvolutionSegment]] = {}

    def append(
        self,
        *,
        run_id: object,
        slice_id: object,
        summary_text: str,
        template_sha256: str,
        entry_index: int | None = None,
    ) -> EvolutionSegment:
        if not summary_text.strip():
            raise ValueError("evolution summary must be non-empty")
        if not re.fullmatch(r"[0-9a-f]{64}", template_sha256):
            raise ValueError("template digest must be SHA-256")
        entries = self._entries.setdefault(run_id, [])
        if entries and entries[0].template_sha256 != template_sha256:
            raise ValueError("evolution template is frozen per Run")
        if any(entry.slice_id == slice_id for entry in entries):
            raise ValueError("evolution append-only slice has already been appended")
        expected = len(entries) if entry_index is None else entry_index
        if type(expected) is not int or expected != len(entries):
            raise ValueError("evolution entries are append-only")
        entry = EvolutionSegment(run_id, expected, slice_id, summary_text, template_sha256)
        entries.append(entry)
        return entry

    def entries(self, *, run_id: object) -> tuple[EvolutionSegment, ...]:
        return tuple(self._entries.get(run_id, ()))

    def render(self, *, run_id: object, template_sha256: str) -> str:
        entries = self.entries(run_id=run_id)
        if any(entry.template_sha256 != template_sha256 for entry in entries):
            raise ValueError("evolution template digest is not frozen")
        return "\n".join(
            f"[evolution:{entry.entry_index}] {entry.summary_text}" for entry in entries
        )


class DataBlockKind(str, Enum):
    SourceFile = "source_file"
    SourceAst = "source_ast"
    Shell = "shell"
    Exec = "exec"
    CompleteLog = "complete_log"
    ToolError = "tool_error"
    Contract = "contract"


@dataclass(frozen=True, slots=True)
class ContextDataBlock:
    kind: DataBlockKind
    text: str
    total_bytes: int
    truncated: bool = False
    artifact_ref: str | ArtifactRef | None = None
    range_hint: str | None = None
    facts: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if type(self.total_bytes) is not int or self.total_bytes < 0:
            raise ValueError("data block byte count must not be negative")
        if len(self.text.encode("utf-8")) > MAX_CONTEXT_BLOCK_BYTES:
            raise ValueError("context data block exceeds 256 KiB")
        if self.artifact_ref is not None:
            object.__setattr__(self, "artifact_ref", _cas_ref(self.artifact_ref))
        object.__setattr__(self, "facts", tuple(self.facts))


def _byte_prefix(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _cas_ref(value: str | ArtifactRef | None) -> str | None:
    if value is None:
        return None
    result = f"cas://{value.sha256}" if isinstance(value, ArtifactRef) else str(value)
    if CAS_URI_PATTERN.fullmatch(result) is None:
        raise ValueError("artifact_ref must be a cas:// SHA-256 URI")
    return result.lower()


def _owned_cas_ref(
    value: str | ArtifactRef | None,
    *,
    run_id: object | None,
    cas_ref_validator: CasRefValidator | None,
) -> str | None:
    reference = _cas_ref(value)
    if reference is None:
        return None
    if run_id is None or cas_ref_validator is None:
        raise ValueError("CAS ownership validation requires the current run identity")
    try:
        owned = cas_ref_validator(reference, run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("CAS ownership validation failed") from exc
    if owned is not True:
        raise ValueError("CAS reference is not owned by the current run")
    return reference


def govern_read_file(
    body: str,
    *,
    path: str,
    total_lines: int,
    start_line: int = 1,
    truncated: bool = False,
) -> ContextDataBlock:
    if (
        not isinstance(body, str)
        or not path
        or type(total_lines) is not int
        or total_lines < 0
        or type(start_line) is not int
        or start_line < 1
        or type(truncated) is not bool
    ):
        raise ValueError("ReadFile context facts are invalid")
    total_bytes = len(body.encode("utf-8"))
    truncated = truncated or total_bytes > MAX_CONTEXT_BLOCK_BYTES
    path_hint = _byte_prefix(path, 4096)
    if truncated:
        marker = (
            f"\n[truncated=true; range={path_hint}:{start_line}-{total_lines}; "
            "reread_next_range=true]"
        )
        text = _byte_prefix(body, max(0, MAX_CONTEXT_BLOCK_BYTES - len(marker.encode("utf-8"))))
        text = f"{text}{marker}"
    else:
        text = body
    return ContextDataBlock(
        DataBlockKind.SourceFile,
        text,
        total_bytes,
        truncated=truncated,
        range_hint=f"{path_hint}:{start_line}-{total_lines}",
    )


def govern_shell_output(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    artifact_ref: str | ArtifactRef | None = None,
    run_id: object | None = None,
    cas_ref_validator: CasRefValidator | None = None,
) -> ContextDataBlock:
    if not isinstance(stdout, str) or not isinstance(stderr, str) or type(exit_code) is not int:
        raise ValueError("Shell context facts are invalid")
    combined = f"[exit_code={exit_code}]\n[stdout]\n{stdout}\n[stderr]\n{stderr}".strip()
    total_bytes = len(combined.encode("utf-8"))
    truncated = total_bytes > MAX_CONTEXT_BLOCK_BYTES
    reference = _owned_cas_ref(
        artifact_ref, run_id=run_id, cas_ref_validator=cas_ref_validator
    )
    if truncated and reference is None:
        raise ValueError("oversized Shell output requires a CAS artifact reference")
    if truncated:
        prefix = "[truncated=true]\n[head]\n"
        separator = "\n[tail]\n"
        payload_limit = MAX_CONTEXT_BLOCK_BYTES - len(
            (prefix + separator).encode("utf-8")
        )
        head_limit = payload_limit // 2
        tail_limit = payload_limit - head_limit
        text = (
            prefix
            + _byte_prefix(combined, head_limit)
            + separator
            + combined.encode("utf-8")[-tail_limit:].decode("utf-8", errors="ignore")
        )
    else:
        text = combined
    return ContextDataBlock(
        DataBlockKind.Shell,
        text,
        total_bytes,
        truncated=truncated,
        artifact_ref=reference,
        range_hint=f"exit_code={exit_code}",
    )


def govern_exec_result(
    summary: str,
    *,
    step_count: int,
    error_message: str | None = None,
    artifact_ref: str | ArtifactRef | None = None,
    run_id: object | None = None,
    cas_ref_validator: CasRefValidator | None = None,
) -> ContextDataBlock:
    if not isinstance(summary, str):
        raise ValueError("Exec summary must be text")
    if error_message is not None and not isinstance(error_message, str):
        raise ValueError("Exec error message must be text")
    if type(step_count) is not int or step_count < 0:
        raise ValueError("Exec step count is invalid")
    total_bytes = len(summary.encode("utf-8")) + (
        len(error_message.encode("utf-8")) if error_message is not None else 0
    )
    truncated = total_bytes > MAX_CONTEXT_BLOCK_BYTES
    reference = _owned_cas_ref(
        artifact_ref, run_id=run_id, cas_ref_validator=cas_ref_validator
    )
    if truncated and reference is None:
        raise ValueError("oversized Exec summary requires a CAS artifact reference")
    # The full per-receipt result is audit-only. Only this bounded structural
    # receipt summary is allowed into the provider context.
    summary_limit = MAX_CONTEXT_BLOCK_BYTES - 8192
    summary_text = summary
    if len(summary.encode("utf-8")) > summary_limit:
        truncated = True
        if reference is None:
            raise ValueError("oversized Exec summary requires a CAS artifact reference")
        half = summary_limit // 2
        summary_text = (
            "[head]\n"
            + _byte_prefix(summary, half)
            + "\n[tail]\n"
            + summary.encode("utf-8")[-(summary_limit - half) :].decode(
                "utf-8", errors="ignore"
            )
        )
    payload: dict[str, object] = {
        "step_count": step_count,
        "outcome": "FAILED" if error_message else "OK",
        "summary": summary_text,
        "truncated": truncated,
    }
    if error_message:
        payload["error"] = _byte_prefix(error_message, 4096)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ContextDataBlock(
        DataBlockKind.Exec,
        text,
        total_bytes,
        truncated=truncated,
        artifact_ref=reference,
    )


def govern_complete_log(
    *,
    total_bytes: int,
    artifact_ref: str | ArtifactRef,
    run_id: object | None = None,
    cas_ref_validator: CasRefValidator | None = None,
) -> ContextDataBlock:
    """Represent a complete log only by its CAS identity and size."""

    if type(total_bytes) is not int or total_bytes < 0:
        raise ValueError("complete log byte count is invalid")
    reference = _owned_cas_ref(
        artifact_ref, run_id=run_id, cas_ref_validator=cas_ref_validator
    )
    assert reference is not None
    text = json.dumps(
        {"artifact_ref": reference, "bytes": total_bytes},
        sort_keys=True,
        separators=(",", ":"),
    )
    return ContextDataBlock(
        DataBlockKind.CompleteLog,
        text,
        total_bytes,
        truncated=True,
        artifact_ref=reference,
    )


def govern_ast_matches(matches: Sequence[object]) -> ContextDataBlock:
    if isinstance(matches, (str, bytes)):
        raise TypeError("AST matches must be a sequence")
    values = tuple(matches)
    truncated = len(values) > MAX_AST_MATCHES
    facts = values[:MAX_AST_MATCHES]
    while True:
        text = json.dumps(
            {"matches": [_jsonable(item) for item in facts], "truncated": truncated},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(text.encode("utf-8")) <= MAX_CONTEXT_BLOCK_BYTES or not facts:
            break
        facts = facts[:-1]
        truncated = True
    return ContextDataBlock(
        DataBlockKind.SourceAst,
        text,
        len(text.encode("utf-8")),
        truncated=truncated,
        facts=facts,
    )


def govern_tool_error(code: str, facts: Mapping[str, object]) -> ContextDataBlock:
    if not isinstance(code, str) or not code or not isinstance(facts, Mapping):
        raise ValueError("ToolError context facts are invalid")
    text = json.dumps(
        {"code": code, "facts": _jsonable(facts)}, sort_keys=True, separators=(",", ":")
    )
    return ContextDataBlock(
        DataBlockKind.ToolError,
        text,
        len(text.encode("utf-8")),
        facts=tuple(facts.items()),
    )


def govern_contract_reference(refs: Sequence[str]) -> ContextDataBlock:
    if (
        isinstance(refs, (str, bytes))
        or not isinstance(refs, Sequence)
        or any(not isinstance(ref, str) or not ref for ref in refs)
    ):
        raise ValueError("contract references must be non-empty text")
    text = json.dumps({"contract_refs": list(refs)}, sort_keys=True, separators=(",", ":"))
    return ContextDataBlock(DataBlockKind.Contract, text, len(text.encode("utf-8")))


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _jsonable(asdict(cast(Any, value)))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class EvictionAudit:
    segment_kind: str
    source_ref: str | None
    replacement: str
    turn_index: int


@dataclass(frozen=True, slots=True)
class EvictionResult:
    envelope: ContextEnvelope
    audit: tuple[EvictionAudit, ...]


class EvictionEngine:
    """Replace only old, non-required targeted results with rereadable facts."""

    def evict(
        self,
        envelope: ContextEnvelope,
        *,
        current_turn: int,
        current_tokens: int,
        net_input_cap: int,
        watermark_pct: int,
        measure: Callable[[ContextEnvelope], int] | None = None,
        audit_sink: EvictionAuditSink | None = None,
    ) -> EvictionResult:
        if (
            type(current_turn) is not int
            or current_turn < 0
            or type(current_tokens) is not int
            or type(net_input_cap) is not int
        ):
            raise TypeError("eviction measurements must be integers")
        if net_input_cap <= 0 or not 1 <= watermark_pct <= 100:
            raise ValueError("eviction measurements are invalid")
        if current_tokens * 100 < net_input_cap * watermark_pct:
            return EvictionResult(envelope, ())
        targeted: list[ContextSegment] = []
        audit: list[EvictionAudit] = []
        for segment in envelope.targeted:
            if (
                not segment.evictable
                or segment.required
                or segment.turn_index is None
                or segment.turn_index >= current_turn
            ):
                targeted.append(segment)
                continue
            replacement = json.dumps(
                {
                    "evicted": True,
                    "source_ref": segment.source_ref,
                    "conclusion": "content externalized; reread explicitly",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            targeted.append(
                ContextSegment(
                    "targeted",
                    replacement,
                    required=False,
                    evictable=False,
                    source_ref=segment.source_ref,
                    turn_index=segment.turn_index,
                )
            )
            audit.append(
                EvictionAudit("targeted", segment.source_ref, replacement, segment.turn_index)
            )
        if not audit:
            return EvictionResult(envelope, ())
        if measure is None or audit_sink is None:
            raise ContextBudgetError(
                "eviction requires exact remeasurement and durable audit",
                code="CONTEXT_CAPABILITY_INVALID",
            )
        result = EvictionResult(
            ContextEnvelope(
                stable=envelope.stable,
                evolving=envelope.evolving,
                targeted=tuple(targeted),
            ),
            tuple(audit),
        )
        measured = measure(result.envelope)
        if type(measured) is not int or measured < 0:
            raise ContextBudgetError(
                "eviction remeasurement is invalid", code="CONTEXT_CAPABILITY_INVALID"
            )
        if measured > net_input_cap:
            raise ContextBudgetError("context remains above the locked net-input cap")
        audit_sink(result.audit)
        return result


class RecoveryBriefBuilder:
    """Build fact-only recovery material; no model narrative is accepted."""

    @staticmethod
    def from_facts(
        *,
        slice_ref: SliceGenerationRef,
        candidate_commit_oid: str | None,
        file_count: int = 0,
        total_bytes: int = 0,
        feedback: Sequence[tuple[CheckAction, int, str]] = (),
        discarded_turns: int,
        completed_items: Sequence[str] = (),
        remaining_task_hints: Sequence[str] = (),
    ) -> RecoveryBrief:
        checkpoint = (
            CheckpointSummary(
                candidate_commit_oid=GitOid(candidate_commit_oid),
                file_count=file_count,
                total_bytes=total_bytes,
            )
            if candidate_commit_oid is not None
            else None
        )
        summaries = tuple(
            CheckFeedbackSummary(
                action=action,
                exit_code=exit_code,
                output_digest=Sha256(digest),
            )
            for action, exit_code, digest in feedback
        )
        progress = (
            SegmentProgressSummary(
                completed_items=tuple(completed_items),
                remaining_task_hints=tuple(remaining_task_hints),
            )
            if completed_items or remaining_task_hints
            else None
        )
        return RecoveryBrief(
            slice=slice_ref,
            latest_checkpoint=checkpoint,
            recent_check_feedback=summaries,
            discarded_turns=discarded_turns,
            segment_progress=progress,
        )

    @classmethod
    def from_events(
        cls,
        *,
        slice_ref: SliceGenerationRef,
        run_id: object,
        events: Sequence[Mapping[str, object]],
        discarded_turns: int,
        completed_items: Sequence[str] = (),
        remaining_task_hints: Sequence[str] = (),
    ) -> RecoveryBrief:
        """Derive a brief from typed audit facts without replaying dialogue."""

        if run_id is None:
            raise ValueError("recovery event projection requires a Run identity")
        checkpoint: tuple[str, int, int] | None = None
        feedback: list[tuple[CheckAction, int, str]] = []
        for event in events:
            event_type = event.get("event_type", event.get("type"))
            raw_data = event.get("data", event)
            if not isinstance(event_type, str) or not isinstance(raw_data, Mapping):
                continue
            scope = {**event, **raw_data}
            generation = scope.get("generation")
            if (
                str(scope.get("run_id")) != str(run_id)
                or str(scope.get("slice_id")) != str(slice_ref.slice_id)
                or (generation is not None and generation != slice_ref.generation)
            ):
                continue
            if event_type in {"checkpoint.committed", "checkpoint.completed"}:
                candidate = raw_data.get("candidate_commit_oid")
                file_count = raw_data.get("file_count")
                total_bytes = raw_data.get("total_bytes")
                if (
                    not isinstance(candidate, str)
                    or type(file_count) is not int
                    or type(total_bytes) is not int
                ):
                    raise ValueError("checkpoint event lacks typed recovery facts")
                checkpoint = (candidate, file_count, total_bytes)
            elif event_type in {"check.feedback", "verification.check"}:
                action = raw_data.get("action")
                exit_code = raw_data.get("exit_code")
                digest = raw_data.get("output_digest")
                if (
                    not isinstance(action, str)
                    or type(exit_code) is not int
                    or not isinstance(digest, str)
                ):
                    raise ValueError("check event lacks typed recovery facts")
                feedback.append((CheckAction(action), exit_code, digest))
        return cls.from_facts(
            slice_ref=slice_ref,
            candidate_commit_oid=checkpoint[0] if checkpoint else None,
            file_count=checkpoint[1] if checkpoint else 0,
            total_bytes=checkpoint[2] if checkpoint else 0,
            feedback=tuple(feedback),
            discarded_turns=discarded_turns,
            completed_items=completed_items,
            remaining_task_hints=remaining_task_hints,
        )


@dataclass(frozen=True, slots=True)
class RegenerationHistory:
    diagnostic_summary: str
    checkpoint_diff_summary: str

    def __post_init__(self) -> None:
        if not self.diagnostic_summary.strip() or not self.checkpoint_diff_summary.strip():
            raise ValueError("regeneration history requires exactly two non-empty summaries")

    def to_segments(
        self,
        *,
        max_inline_bytes: int | None = None,
        checkpoint_diff_artifact_ref: str | ArtifactRef | None = None,
        run_id: object | None = None,
        cas_ref_validator: CasRefValidator | None = None,
    ) -> tuple[ContextSegment, ContextSegment]:
        diagnostic = self.diagnostic_summary
        diff = self.checkpoint_diff_summary
        if max_inline_bytes is not None and max_inline_bytes < 1:
            raise ValueError("max_inline_bytes must be positive")
        if max_inline_bytes is not None and len(diff.encode("utf-8")) > max_inline_bytes:
            reference = _owned_cas_ref(
                checkpoint_diff_artifact_ref,
                run_id=run_id,
                cas_ref_validator=cas_ref_validator,
            )
            if reference is None:
                raise ValueError("oversized checkpoint diff requires a CAS artifact")
            diff = json.dumps({"artifact_ref": reference}, sort_keys=True, separators=(",", ":"))
        if max_inline_bytes is not None and len(diagnostic.encode("utf-8")) > max_inline_bytes:
            raise ContextBudgetError(
                "failure diagnostic summary is required and cannot be truncated"
            )
        return (
            ContextSegment(
                "targeted",
                diagnostic,
                required=True,
                evictable=False,
                source_ref="history:failure-diagnostic",
            ),
            ContextSegment(
                "targeted",
                diff,
                required=True,
                evictable=False,
                source_ref="history:checkpoint-diff",
            ),
        )


def repair_navigation_segments(
    *,
    brief: object,
    max_index_bytes: int = MAX_CONTEXT_BLOCK_BYTES,
    index_artifact_ref: str | ArtifactRef | None = None,
    run_id: object | None = None,
    cas_ref_validator: CasRefValidator | None = None,
) -> tuple[ContextSegment, ContextSegment]:
    """Render required repair facts plus an on-demand navigation index."""

    failure_facts = getattr(brief, "failure_facts", None)
    scope_index = getattr(brief, "scope_index", None)
    if failure_facts is None or scope_index is None:
        raise TypeError("repair brief must expose failure_facts and scope_index")
    if type(max_index_bytes) is not int or not 1 <= max_index_bytes <= MAX_CONTEXT_BLOCK_BYTES:
        raise ValueError("max_index_bytes must be within the context block limit")
    required = json.dumps(
        {
            "failure_facts": {
                "failed_test_refs": list(getattr(failure_facts, "failed_test_refs")),
                "diagnostic_summary": _jsonable(
                    getattr(failure_facts, "diagnostic_summary")
                ),
                "cas_refs": list(getattr(failure_facts, "cas_refs")),
            },
            "attribution": _jsonable(getattr(brief, "attribution")),
            "repair_history": _jsonable(getattr(brief, "repair_history")),
            "constraints": _jsonable(getattr(brief, "constraints")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(required.encode("utf-8")) > MAX_CONTEXT_BLOCK_BYTES:
        raise ContextBudgetError("repair brief required facts cannot be truncated")
    index = json.dumps(
        {
            "paths": list(getattr(scope_index, "paths")),
            "positions": list(getattr(scope_index, "positions")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(index.encode("utf-8")) > max_index_bytes:
        reference = _owned_cas_ref(
            index_artifact_ref, run_id=run_id, cas_ref_validator=cas_ref_validator
        )
        if reference is None:
            raise ValueError("oversized repair navigation index requires a CAS artifact")
        index = json.dumps({"artifact_ref": reference}, sort_keys=True, separators=(",", ":"))
    return (
        ContextSegment(
            "targeted", required, required=True, evictable=False, source_ref="repair:brief"
        ),
        ContextSegment(
            "targeted",
            index,
            required=False,
            evictable=True,
            source_ref="repair:navigation-index",
        ),
    )


__all__ = [
    "CAS_URI_PATTERN",
    "MAX_AST_MATCHES",
    "MAX_CONTEXT_BLOCK_BYTES",
    "BudgetProfile",
    "ContextAssembly",
    "ContextBudgetError",
    "ContextDataBlock",
    "ContextManager",
    "ContextPackCache",
    "DataBlockKind",
    "DraftingBudgetProfile",
    "EvictionAudit",
    "EvictionEngine",
    "EvictionResult",
    "EvolutionSegment",
    "EvolutionSegmentDraft",
    "FormulaNetInputCap",
    "InMemoryEvolutionSegmentStore",
    "NetInputCap",
    "RecoveryBriefBuilder",
    "RegenerationHistory",
    "SessionBudgetCatalog",
    "TokenCounter",
    "govern_ast_matches",
    "govern_contract_reference",
    "govern_complete_log",
    "govern_exec_result",
    "govern_read_file",
    "govern_shell_output",
    "govern_tool_error",
    "repair_navigation_segments",
]


UnifiedContextManager = ContextManager
ContextPackAssembler = ContextManager
__all__ += ["ContextPackAssembler", "UnifiedContextManager"]
