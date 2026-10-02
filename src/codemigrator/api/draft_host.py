"""Validated Draft host inputs supplied by registered project infrastructure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from codemigrator.analysis import SnapshotSource
from codemigrator.core import RegisteredProject


@dataclass(frozen=True, slots=True)
class RegisteredSnapshot:
    """A resolved immutable source snapshot for one registered project selection."""

    project: RegisteredProject
    source: SnapshotSource
    module_files: Mapping[str, tuple[str, ...]]


class RegisteredSnapshotResolver(Protocol):
    """Resolve only server-registered project/snapshot identities for a principal."""

    async def resolve_snapshot(
        self, principal_id: str, project: RegisteredProject
    ) -> RegisteredSnapshot | None: ...


__all__ = ["RegisteredSnapshot", "RegisteredSnapshotResolver"]
