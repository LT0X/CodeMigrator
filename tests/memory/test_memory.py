from __future__ import annotations

import json
from uuid import uuid4

import pytest

from codemigrator.core import (
    ArtifactRef,
    AttributionReliability,
    CheckAction,
    ContextPackIdentity,
    Phase,
    RepairEvidence,
    SessionKind,
    Sha256,
    SliceGenerationRef,
    SliceId,
    WriteScope,
    WriteScopeOut,
    load_session_budget,
)
from codemigrator.runtime.context import ContextEnvelope, ContextSegment, prompt_text
from codemigrator.runtime.memory import (
    ContextBudgetError,
    ContextManager,
    ContextPackCache,
    DataBlockKind,
    EvictionEngine,
    InMemoryEvolutionSegmentStore,
    NetInputCap,
    RecoveryBriefBuilder,
    RegenerationHistory,
    SessionBudgetCatalog,
    TokenCounter,
    govern_ast_matches,
    govern_complete_log,
    govern_exec_result,
    govern_read_file,
    govern_shell_output,
    repair_navigation_segments,
)
from codemigrator.runtime.repair import (
    RepairBrief,
    RepairConstraints,
    RepairFailureFacts,
    RepairHistoryEntry,
    RepairNavigationIndex,
)
from codemigrator.runtime.schema import RUNTIME_SCHEMA_SQL

RUN_ID = uuid4()
SLICE_ID = uuid4()
SLICE_REF = SliceGenerationRef(slice_id=SLICE_ID, generation=0, baseline_candidate_oid=None)


def identity(*, run_id=RUN_ID, session=SessionKind.Implementation) -> ContextPackIdentity:
    return ContextPackIdentity(
        run_id=run_id,
        phase=Phase.Execute,
        session=session,
        slice=(
            SLICE_REF
            if session not in {SessionKind.ExecuteSupervisor, SessionKind.ExploreCoordinator}
            else None
        ),
        spec_sha256="1" * 64,
        model_binding_sha256="2" * 64,
        phase_policy_sha256="3" * 64,
        contract_refs_sha256="4" * 64,
    )


class ExactCounter(TokenCounter):
    def count(self, messages):
        return sum(len(message.content) for message in messages)


class FormulaCap(NetInputCap):
    def compute(self, *, context_window, reserved_output, tool_schema_tokens, envelope_margin):
        return context_window - reserved_output - tool_schema_tokens - envelope_margin


def test_budget_catalog_matches_v1_and_is_immutable() -> None:
    catalog = SessionBudgetCatalog.from_core()
    assert catalog.profile(SessionKind.Implementation).max_rounds == 500
    assert catalog.profile("DRAFTING").max_rounds == 200
    assert catalog.resource_sha256
    with pytest.raises(TypeError):
        catalog.profiles["IMPLEMENTATION"] = object()
    assert catalog.to_mapping() == load_session_budget()


def test_context_manager_fails_closed_without_provider_capability() -> None:
    manager = ContextManager()
    with pytest.raises(ContextBudgetError) as error:
        manager.fit(
            identity=identity(),
            template="role",
            envelope=ContextEnvelope(),
            context_window=1000,
            reserved_output=1,
            tool_schema_tokens=1,
            envelope_margin=1,
        )
    assert error.value.code == "CONTEXT_CAPABILITY_INVALID"


def test_cache_key_covers_identity_and_never_reuses_another_run() -> None:
    cache = ContextPackCache()
    manager = ContextManager(token_counter=ExactCounter(), net_input_cap=FormulaCap())
    assembly = manager.fit(
        identity=identity(),
        template="role",
        envelope=ContextEnvelope(stable=(ContextSegment("stable", "facts"),)),
        context_window=1000,
        reserved_output=1,
        tool_schema_tokens=1,
        envelope_margin=1,
    )
    cache.put(assembly.pack.identity, assembly.pack)
    assert cache.get(assembly.pack.identity) == assembly.pack
    changed = assembly.pack.identity.model_copy(update={"spec_sha256": "9" * 64})
    assert cache.get(changed) is None
    other_run = assembly.pack.identity.model_copy(update={"run_id": uuid4()})
    assert cache.get(other_run) is None
    assert len(cache) == 1


def test_context_manager_requires_exact_counter_and_rejects_physical_overflow() -> None:
    manager = ContextManager(token_counter=ExactCounter(), net_input_cap=FormulaCap())
    envelope = ContextEnvelope(stable=(ContextSegment("stable", "facts"),))
    result = manager.fit(
        identity=identity(),
        template="role",
        envelope=envelope,
        context_window=110,
        reserved_output=5,
        tool_schema_tokens=3,
        envelope_margin=2,
    )
    assert result.pack.assembled_tokens == 95  # exact counter includes the rendered role text
    with pytest.raises(ContextBudgetError) as error:
        manager.fit(
            identity=identity(),
            template="x" * 20,
            envelope=envelope,
            context_window=110,
            reserved_output=5,
            tool_schema_tokens=3,
            envelope_margin=2,
        )
    assert error.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_initial_pack_has_no_source_body_and_template_digest_is_frozen() -> None:
    manager = ContextManager(token_counter=ExactCounter(), net_input_cap=FormulaCap())
    with pytest.raises(ValueError, match="source body"):
        manager.fit(
            identity=identity(),
            template="role",
            envelope=ContextEnvelope(
                targeted=(ContextSegment("targeted", "source", source_body=True),)
            ),
            context_window=1000,
            reserved_output=1,
            tool_schema_tokens=1,
            envelope_margin=1,
        )
    assembly = manager.fit(
        identity=identity(),
        template="role",
        envelope=ContextEnvelope(stable=(ContextSegment("stable", "facts"),)),
        context_window=1000,
        reserved_output=1,
        tool_schema_tokens=1,
        envelope_margin=1,
    )
    assert assembly.pack.identity.template_sha256 != "0" * 64
    assert "facts" in prompt_text(assembly.messages)


def test_evolution_append_is_append_only_and_replay_is_byte_identical() -> None:
    store = InMemoryEvolutionSegmentStore()
    store.append(run_id=RUN_ID, slice_id=SLICE_ID, summary_text="slice A", template_sha256="a" * 64)
    store.append(run_id=RUN_ID, slice_id=uuid4(), summary_text="slice B", template_sha256="a" * 64)
    rendered = store.render(run_id=RUN_ID, template_sha256="a" * 64)
    assert rendered == store.render(run_id=RUN_ID, template_sha256="a" * 64)
    assert rendered == "[evolution:0] slice A\n[evolution:1] slice B"
    with pytest.raises(ValueError, match="append-only"):
        store.append(
            run_id=RUN_ID,
            slice_id=SLICE_ID,
            summary_text="changed",
            template_sha256="a" * 64,
            entry_index=0,
        )


def test_event_triggered_projection_does_not_render_machine_snapshot() -> None:
    manager = ContextManager(token_counter=ExactCounter(), net_input_cap=FormulaCap())
    assembly = manager.fit_triggered(
        identity=identity(session=SessionKind.ExecuteSupervisor),
        template="supervisor",
        stable=(ContextSegment("stable", "fixed"),),
        snapshot={"candidate_oid": "private-snapshot"},
        event_projection=("slice.failed", "repair.requested"),
        context_window=1000,
        reserved_output=1,
        tool_schema_tokens=1,
        envelope_margin=1,
    )
    text = prompt_text(assembly.messages)
    assert "private-snapshot" not in text
    assert "slice.failed" in text
    assert assembly.snapshot == {"candidate_oid": "private-snapshot"}


def test_data_block_governors_keep_boundaries_and_externalize_full_logs() -> None:
    read = govern_read_file("x" * (256 * 1024 + 10), path="src/a.py", total_lines=20)
    assert read.kind is DataBlockKind.SourceFile
    assert read.truncated is True
    assert "truncated=true" in read.text
    assert len(read.text.encode()) <= 256 * 1024
    shell = govern_shell_output(
        stdout="head\n" + "x" * (300 * 1024) + "\ntail",
        stderr="",
        exit_code=1,
        artifact_ref="cas://" + "a" * 64,
        run_id=RUN_ID,
        cas_ref_validator=lambda reference, run_id: run_id == RUN_ID,
    )
    assert shell.truncated is True
    assert "head" in shell.text and "tail" in shell.text
    assert "exit_code=1" in shell.text
    assert "\ntail\n[stderr]" in shell.text
    assert shell.artifact_ref == "cas://" + "a" * 64
    exec_block = govern_exec_result("aggregate summary", step_count=2)
    assert exec_block.kind is DataBlockKind.Exec
    assert "aggregate summary" in exec_block.text
    assert json.loads(exec_block.text)["step_count"] == 2
    with pytest.raises(ValueError, match="artifact"):
        govern_shell_output(stdout="x" * (300 * 1024), stderr="", exit_code=1)
    with pytest.raises(ValueError, match="artifact"):
        govern_exec_result("x" * (300 * 1024), step_count=2)
    assert len(govern_ast_matches(range(250)).facts) == 200
    complete = govern_complete_log(
        total_bytes=1024,
        artifact_ref=ArtifactRef(sha256=Sha256("b" * 64), size=1024, media_type="text/plain"),
        run_id=RUN_ID,
        cas_ref_validator=lambda reference, run_id: run_id == RUN_ID,
    )
    assert complete.text == '{"artifact_ref":"cas://' + "b" * 64 + '","bytes":1024}'


def test_eviction_only_replaces_old_targeted_results() -> None:
    envelope = ContextEnvelope(
        stable=(ContextSegment("stable", "system", required=True),),
        evolving=(ContextSegment("evolving", "verified", required=True),),
        targeted=(
            ContextSegment("targeted", "old tool output", source_ref="src/a.py:1", turn_index=1),
            ContextSegment("targeted", "keep me", required=True),
            ContextSegment("targeted", "current output", source_ref="src/b.py:2", turn_index=2),
        ),
    )
    audit_records = []
    result = EvictionEngine().evict(
        envelope,
        current_turn=2,
        current_tokens=180,
        net_input_cap=200,
        watermark_pct=80,
        measure=lambda candidate: sum(
            len(segment.content) for segment in candidate.targeted
        ),
        audit_sink=audit_records.append,
    )
    assert result.envelope.stable == envelope.stable
    assert result.envelope.evolving == envelope.evolving
    assert "old tool output" not in result.envelope.targeted[0].content
    assert "src/a.py:1" in result.envelope.targeted[0].content
    assert result.envelope.targeted[1] == envelope.targeted[1]
    assert result.envelope.targeted[2] == envelope.targeted[2]
    assert result.audit[0].segment_kind == "targeted"
    assert audit_records == [result.audit]


def test_recovery_brief_is_structured_and_distinguishes_continuation() -> None:
    brief = RecoveryBriefBuilder.from_facts(
        slice_ref=SLICE_REF,
        candidate_commit_oid="a" * 40,
        file_count=2,
        total_bytes=10,
        feedback=((CheckAction.Test, 1, "b" * 64),),
        discarded_turns=0,
        completed_items=("models/user.py",),
        remaining_task_hints=("finish tests",),
    )
    assert brief.discarded_turns == 0
    assert brief.segment_progress is not None
    assert not hasattr(brief, "narrative")
    assert brief.recent_check_feedback[0].output_digest == "b" * 64

    rebuilt = RecoveryBriefBuilder.from_events(
        slice_ref=SLICE_REF,
        events=(
            {
                "event_type": "checkpoint.completed",
                "run_id": RUN_ID,
                "slice_id": uuid4(),
                "generation": 0,
                "data": {"candidate_commit_oid": "z" * 40, "file_count": 99, "total_bytes": 99},
            },
            {
                "event_type": "checkpoint.completed",
                "run_id": RUN_ID,
                "slice_id": SLICE_ID,
                "generation": 0,
                "data": {"candidate_commit_oid": "c" * 40, "file_count": 3, "total_bytes": 20},
            },
            {
                "event_type": "check.feedback",
                "run_id": RUN_ID,
                "slice_id": SLICE_ID,
                "generation": 0,
                "data": {
                    "action": "TEST",
                    "exit_code": 1,
                    "output_digest": "d" * 64,
                },
            },
        ),
        run_id=RUN_ID,
        discarded_turns=4,
    )
    assert rebuilt.latest_checkpoint is not None
    assert rebuilt.latest_checkpoint.candidate_commit_oid == "c" * 40
    assert rebuilt.discarded_turns == 4


def test_cas_references_require_current_run_ownership() -> None:
    with pytest.raises(ValueError, match="ownership"):
        govern_complete_log(total_bytes=1, artifact_ref="cas://" + "a" * 64)
    with pytest.raises(ValueError, match="owned"):
        govern_complete_log(
            total_bytes=1,
            artifact_ref="cas://" + "a" * 64,
            run_id=RUN_ID,
            cas_ref_validator=lambda reference, run_id: False,
        )


def test_regeneration_history_has_exactly_two_fact_segments() -> None:
    history = RegenerationHistory(
        diagnostic_summary="one failed test",
        checkpoint_diff_summary="changed src/a.py",
    )
    segments = history.to_segments()
    assert len(segments) == 2
    assert [segment.kind for segment in segments] == ["targeted", "targeted"]
    assert [segment.source_ref for segment in segments] == [
        "history:failure-diagnostic",
        "history:checkpoint-diff",
    ]


def test_repair_navigation_keeps_required_facts_and_externalizes_only_index() -> None:
    slice_id = SliceId(uuid4())
    brief = RepairBrief(
        attribution=RepairEvidence(
            candidate_slice_set=[slice_id],
            reliability=AttributionReliability.Uncertain,
            strong_coupling=True,
            cross_generation_recurrence=False,
            conservation_signal_summary={"status": "ambiguous"},
        ),
        failure_facts=RepairFailureFacts(
            failed_test_refs=("test://suite/case",),
            diagnostic_summary={"code": "E001"},
            cas_refs=("cas://" + "a" * 64,),
        ),
        scope_index=RepairNavigationIndex(
            paths=("src/a.py", "src/b.py"), positions=("src/a.py:10",)
        ),
        repair_history=(RepairHistoryEntry("decision-1", "rejected"),),
        constraints=RepairConstraints(
            write_scope=WriteScope(out=WriteScopeOut(write_paths=["src/a.py"], create_roots=[])),
            verification_requirements=("local",),
        ),
    )
    required, index = repair_navigation_segments(
        brief=brief,
        max_index_bytes=1,
        index_artifact_ref="cas://" + "c" * 64,
        run_id=RUN_ID,
        cas_ref_validator=lambda reference, run_id: run_id == RUN_ID,
    )
    assert required.required is True
    assert "test://suite/case" in required.content
    assert "cas://" + "a" * 64 in required.content
    assert "decision-1" in required.content
    assert "src/a.py" in required.content
    assert index.content == '{"artifact_ref":"cas://' + "c" * 64 + '"}'
    assert index.evictable is True
    with pytest.raises(ValueError, match="max_index_bytes"):
        repair_navigation_segments(brief=brief, max_index_bytes=0)


def test_runtime_schema_declares_append_only_evolution_segments() -> None:
    assert "context_evolution_segments" in RUNTIME_SCHEMA_SQL
    assert "entry_index" in RUNTIME_SCHEMA_SQL
    assert "PRIMARY KEY (run_id, entry_index)" in RUNTIME_SCHEMA_SQL
