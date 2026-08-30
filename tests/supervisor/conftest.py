from __future__ import annotations

from uuid import uuid4

from codemigrator.core import (
    ContextPack,
    ContextPackIdentity,
    ModelProfile,
    Phase,
    RunStatus,
    SessionBudgetProfile,
    SessionKind,
    SliceGenerationRef,
    new_uuid7,
)
from codemigrator.runtime.binding import LockedModelBinding
from codemigrator.runtime.context import ContextEnvelope
from codemigrator.runtime.loop_contracts import SessionIdentity, SessionSpec


def make_binding() -> LockedModelBinding:
    return LockedModelBinding(
        provider_id="openai",
        model_id="supervisor-test-model",
        profile=ModelProfile.Code,
        config_revision="test-r1",
        context_window=4000,
        output_cap=500,
    )


def make_spec(
    *,
    phase: Phase = Phase.Execute,
    session: SessionKind = SessionKind.ExecuteSupervisor,
) -> SessionSpec:
    run_id = new_uuid7()
    binding = make_binding()
    slice_ref = SliceGenerationRef(slice_id=uuid4(), generation=0, baseline_candidate_oid="a" * 40)
    identity = SessionIdentity(run_id, phase, session, slice_ref)
    status = RunStatus.Executing if phase is Phase.Execute else RunStatus.Verifying
    return SessionSpec(
        identity=identity,
        run_status=status,
        binding=binding,
        context_pack=ContextPack(
            identity=ContextPackIdentity(
                run_id=run_id,
                phase=phase,
                session=session,
                slice=slice_ref,
                spec_sha256="1" * 64,
                model_binding_sha256=binding.digest,
                phase_policy_sha256="2" * 64,
                contract_refs_sha256="3" * 64,
            ),
            budget=SessionBudgetProfile(session=session, max_rounds=1, eviction_watermark_pct=80),
            assembled_tokens=10,
        ),
        context=ContextEnvelope(),
        template="supervisor test role",
    )
