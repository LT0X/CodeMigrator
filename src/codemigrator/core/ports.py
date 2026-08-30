"""Side-effect boundaries consumed by the Spec gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .enums import CheckAction
from .ids import SpecId

if TYPE_CHECKING:
    from .models.spec import RequiredCheckSelection
    from .spec import SpecArtifact, SpecRecord


@dataclass(frozen=True)
class DescriptorResolution:
    """A registry's already-resolved, side-effect-free resource facts."""

    source_language_id: str
    target_language_id: str
    descriptor_version: str
    source_descriptor_sha256: str
    target_descriptor_sha256: str
    toolchain_image_digest: str
    checks: tuple[RequiredCheckSelection, ...] = ()
    grammar_available: bool = True
    image_available: bool = True

    @property
    def supported_checks(self) -> frozenset[tuple[CheckAction, str]]:
        return frozenset((check.action, check.template_sha256) for check in self.checks)


class DescriptorRegistry(Protocol):
    """Runtime-owned lookup port; core never scans descriptors or images."""

    def resolve(
        self, source_language_id: str, target_language_id: str
    ) -> DescriptorResolution | None:
        """Return frozen resource facts or ``None`` when the language pair is absent."""


class SpecRepository(Protocol):
    """Runtime-owned persistence port for immutable Spec artifacts."""

    def insert_or_get(self, artifact: SpecArtifact) -> SpecRecord:
        """Insert canonical bytes once and return the existing/new identity."""

    def delete(self, spec_id: SpecId) -> None:
        """Delete only an unreferenced immutable Spec."""


class InMemoryDescriptorRegistry:
    """Deterministic test stub for the Wave 1 resource gate."""

    def __init__(
        self, resolutions: dict[tuple[str, str], DescriptorResolution] | None = None
    ) -> None:
        self._resolutions = dict(resolutions or {})

    def resolve(
        self, source_language_id: str, target_language_id: str
    ) -> DescriptorResolution | None:
        return self._resolutions.get((source_language_id, target_language_id))


__all__ = [
    "DescriptorRegistry",
    "DescriptorResolution",
    "InMemoryDescriptorRegistry",
    "SpecRepository",
]
