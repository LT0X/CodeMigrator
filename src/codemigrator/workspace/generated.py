"""Trusted source regeneration for GeneratedCode artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import ConfigDict

from codemigrator.core import ArtifactKind
from codemigrator.core._base import CoreModel

from .paths import validate_relative_path


class GeneratedActionError(ValueError):
    """A generated artifact was sent through an invalid action path."""


class ScaffoldPort(Protocol):
    def generate(
        self, source_path: str, target_path: str, workspace_root: str
    ) -> Sequence[str]: ...


class GeneratedActionReceipt(CoreModel):
    model_config = ConfigDict(frozen=True)

    generated: bool = True
    source_path: str
    output_paths: tuple[str, ...]
    scaffold_invoked: bool = True


class GeneratedCodeAction:
    """Invoke a trusted target-toolchain scaffold, never translate generated text."""

    def __init__(self, scaffold: ScaffoldPort) -> None:
        self.scaffold = scaffold

    def run(
        self,
        *,
        artifact_kind: ArtifactKind,
        source_path: str,
        target_path: str,
        workspace_root: str,
    ) -> GeneratedActionReceipt:
        if artifact_kind is not ArtifactKind.GeneratedCode:
            raise GeneratedActionError("only GeneratedCode may use the generation action")
        source = validate_relative_path(source_path)
        target = validate_relative_path(target_path)
        outputs = tuple(
            validate_relative_path(path)
            for path in self.scaffold.generate(source, target, workspace_root)
        )
        if not outputs:
            raise GeneratedActionError("scaffold must report at least one generated output")
        return GeneratedActionReceipt(source_path=source, output_paths=outputs)


__all__ = ["GeneratedActionError", "GeneratedActionReceipt", "GeneratedCodeAction", "ScaffoldPort"]
