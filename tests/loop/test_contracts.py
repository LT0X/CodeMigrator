from __future__ import annotations

from uuid import uuid4

import pytest

from codemigrator.core import (
    ContextPack,
    ContextPackIdentity,
    ModelProfile,
    Phase,
    RunStatus,
    SessionBudgetProfile,
    SessionKind,
    SliceGenerationRef,
)
from codemigrator.runtime.binding import (
    BindingError,
    ContextOverflowError,
    LockedModelBinding,
    ensure_context_fits,
    validate_session_admission,
)
from codemigrator.runtime.loop_contracts import SessionIdentity, SessionSpec, SessionState


def _pack(
    *, phase: Phase, session: SessionKind, run_id, slice_ref=None, model_binding_sha256="2" * 64
) -> ContextPack:
    return ContextPack(
        identity=ContextPackIdentity(
            run_id=run_id,
            phase=phase,
            session=session,
            slice=slice_ref,
            spec_sha256="1" * 64,
            model_binding_sha256=model_binding_sha256,
            phase_policy_sha256="3" * 64,
            contract_refs_sha256="4" * 64,
        ),
        budget=SessionBudgetProfile(
            session=session,
            max_rounds=3,
            eviction_watermark_pct=80,
        ),
        assembled_tokens=10,
    )


def _binding(profile: ModelProfile) -> LockedModelBinding:
    return LockedModelBinding(
        provider_id="openai",
        model_id="test-model",
        profile=profile,
        config_revision="r1",
        context_window=1000,
        output_cap=200,
    )


def test_plan_admission_requires_reasoning_and_matching_frozen_context() -> None:
    run_id = uuid4()
    binding = _binding(ModelProfile.Reasoning)
    spec = SessionSpec(
        identity=SessionIdentity(
            run_id=run_id,
            phase=Phase.Plan,
            session_kind=SessionKind.PlanAuxiliary,
            slice_ref=None,
        ),
        run_status=RunStatus.Planning,
        binding=binding,
        context_pack=_pack(
            phase=Phase.Plan,
            session=SessionKind.PlanAuxiliary,
            run_id=run_id,
            model_binding_sha256=binding.digest,
        ),
    )

    validate_session_admission(spec)
    assert spec.state is SessionState.Created

    with pytest.raises(BindingError, match="profile"):
        validate_session_admission(
            spec.__class__(
                identity=spec.identity,
                run_status=spec.run_status,
                binding=_binding(ModelProfile.Code),
                context_pack=spec.context_pack,
            )
        )


def test_plan_admission_rejects_an_execute_only_session_kind() -> None:
    run_id = uuid4()
    binding = _binding(ModelProfile.Reasoning)
    spec = SessionSpec(
        identity=SessionIdentity(
            run_id=run_id,
            phase=Phase.Plan,
            session_kind=SessionKind.Implementation,
            slice_ref=None,
        ),
        run_status=RunStatus.Planning,
        binding=binding,
        context_pack=_pack(
            phase=Phase.Plan,
            session=SessionKind.Implementation,
            run_id=run_id,
            model_binding_sha256=binding.digest,
        ),
    )

    with pytest.raises(BindingError, match="session kind"):
        validate_session_admission(spec)


def test_execute_admission_rejects_candidate_identity_drift() -> None:
    run_id = uuid4()
    slice_id = uuid4()
    slice_ref = SliceGenerationRef(slice_id=slice_id, generation=1, baseline_candidate_oid="a" * 40)
    binding = _binding(ModelProfile.Code)
    spec = SessionSpec(
        identity=SessionIdentity(
            run_id=run_id,
            phase=Phase.Execute,
            session_kind=SessionKind.Implementation,
            slice_ref=slice_ref,
        ),
        run_status=RunStatus.Executing,
        binding=binding,
        context_pack=_pack(
            phase=Phase.Execute,
            session=SessionKind.Implementation,
            run_id=run_id,
            slice_ref=slice_ref,
            model_binding_sha256=binding.digest,
        ),
    )
    validate_session_admission(spec)

    drifted = spec.__class__(
        identity=SessionIdentity(
            run_id=run_id,
            phase=Phase.Execute,
            session_kind=SessionKind.Implementation,
            slice_ref=SliceGenerationRef(
                slice_id=slice_id,
                generation=2,
                baseline_candidate_oid="a" * 40,
            ),
        ),
        run_status=spec.run_status,
        binding=spec.binding,
        context_pack=spec.context_pack,
    )
    with pytest.raises(BindingError, match="slice identity"):
        validate_session_admission(drifted)


@pytest.mark.parametrize("phase", [Phase.Verify, Phase.Report])
def test_verify_and_report_never_admit_model_sessions(phase: Phase) -> None:
    run_id = uuid4()
    binding = _binding(ModelProfile.Code)
    spec = SessionSpec(
        identity=SessionIdentity(
            run_id=run_id,
            phase=phase,
            session_kind=SessionKind.Implementation,
            slice_ref=None,
        ),
        run_status=RunStatus.Verifying if phase is Phase.Verify else RunStatus.Reporting,
        binding=binding,
        context_pack=_pack(
            phase=phase,
            session=SessionKind.Implementation,
            run_id=run_id,
            model_binding_sha256=binding.digest,
        ),
    )
    with pytest.raises(BindingError, match="model session"):
        validate_session_admission(spec)


def test_binding_digest_changes_when_any_locked_field_changes() -> None:
    first = _binding(ModelProfile.Code)
    changed = LockedModelBinding(
        provider_id=first.provider_id,
        model_id=first.model_id,
        profile=first.profile,
        config_revision="r2",
        context_window=first.context_window,
        output_cap=first.output_cap,
    )
    assert first.digest != changed.digest


def test_context_cap_is_a_physical_guard_and_not_budget_usage() -> None:
    binding = LockedModelBinding(
        provider_id="openai",
        model_id="test-model",
        profile=ModelProfile.Code,
        config_revision="r1",
        context_window=100,
        output_cap=20,
    )

    ensure_context_fits("short input", binding)
    with pytest.raises(ContextOverflowError):
        ensure_context_fits("x" * 400, binding)
