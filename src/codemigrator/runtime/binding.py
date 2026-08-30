"""Frozen model binding and phase/session admission rules."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codemigrator.core import ModelProfile, Phase, RunStatus, SessionKind, canonical_json_bytes

if TYPE_CHECKING:
    from .loop_contracts import SessionSpec


class BindingError(ValueError):
    """A session cannot use the requested frozen model binding."""


class ContextOverflowError(ValueError):
    """The physical model context cannot hold input and output together."""


@dataclass(frozen=True, slots=True)
class LockedModelBinding:
    provider_id: str
    model_id: str
    profile: ModelProfile
    config_revision: str
    context_window: int
    output_cap: int

    def __post_init__(self) -> None:
        if not self.provider_id or not self.model_id or not self.config_revision:
            raise ValueError("provider, model, and config revision are required")
        if self.context_window <= 0 or self.output_cap <= 0:
            raise ValueError("context window and output cap must be positive")
        if self.output_cap >= self.context_window:
            raise ValueError("output cap must leave room for input")

    @property
    def digest(self) -> str:
        payload = {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "profile": self.profile.value,
            "config_revision": self.config_revision,
            "context_window": self.context_window,
            "output_cap": self.output_cap,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def validate_session_admission(spec: SessionSpec) -> None:
    """Fail closed before any provider or tool call is accepted."""

    phase = spec.identity.phase
    if phase is Phase.Plan:
        expected_status = RunStatus.Planning
        expected_profile = ModelProfile.Reasoning
    elif phase is Phase.Execute:
        expected_status = RunStatus.Executing
        expected_profile = ModelProfile.Code
    else:
        raise BindingError("VERIFY and REPORT cannot admit a model session")

    if spec.run_status is not expected_status:
        raise BindingError("phase and run status do not match")
    if spec.binding.profile is not expected_profile:
        raise BindingError("phase requires a different model profile")
    if phase is Phase.Plan and spec.identity.session_kind is not SessionKind.PlanAuxiliary:
        raise BindingError("session kind is not admitted in PLAN")
    if phase is Phase.Execute and spec.identity.session_kind.value in {
        "ANALYZE_AUXILIARY",
        "PLAN_AUXILIARY",
    }:
        raise BindingError("session kind is not admitted in EXECUTE")
    identity = spec.identity
    frozen = spec.context_pack.identity
    if frozen.run_id != identity.run_id or frozen.phase is not identity.phase:
        raise BindingError("context identity does not match session")
    if frozen.session is not identity.session_kind:
        raise BindingError("session kind does not match context")
    if frozen.model_binding_sha256 != spec.binding.digest:
        raise BindingError("model binding digest does not match context")
    context_slice = frozen.slice
    if context_slice != identity.slice_ref:
        raise BindingError("slice identity does not match context")


def estimate_net_input_tokens(text: str) -> int:
    """Conservatively estimate input size for the physical window guard only."""

    if not isinstance(text, str):
        raise TypeError("context text must be a string")
    return math.ceil(len(text.encode("utf-8")) / 4 * 1.2)


def ensure_context_fits(text: str, binding: LockedModelBinding) -> int:
    """Reject physical overflow without treating the estimate as wallet usage."""

    estimated = estimate_net_input_tokens(text)
    if estimated + binding.output_cap > binding.context_window:
        raise ContextOverflowError("context exceeds the locked model window")
    return estimated


__all__ = [
    "BindingError",
    "ContextOverflowError",
    "LockedModelBinding",
    "ensure_context_fits",
    "estimate_net_input_tokens",
    "validate_session_admission",
]
