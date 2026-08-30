from __future__ import annotations

import pytest

from codemigrator.core import ArtifactKind
from codemigrator.workspace import GeneratedActionError, GeneratedCodeAction


class Scaffold:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, source_path, target_path, workspace_root):
        self.calls.append((source_path, target_path, workspace_root))
        return ("generated/module.py",)


def test_generated_code_is_regenerated_from_source_and_never_translated() -> None:
    scaffold = Scaffold()
    action = GeneratedCodeAction(scaffold)
    receipt = action.run(
        artifact_kind=ArtifactKind.GeneratedCode,
        source_path="schema.proto",
        target_path="generated/module.py",
        workspace_root="/managed/workspace",
    )

    assert receipt.generated
    assert receipt.output_paths == ("generated/module.py",)
    assert scaffold.calls == [("schema.proto", "generated/module.py", "/managed/workspace")]


def test_generated_action_rejects_translation_kind() -> None:
    with pytest.raises(GeneratedActionError):
        GeneratedCodeAction(Scaffold()).run(
            artifact_kind=ArtifactKind.ResourceFile,
            source_path="a.proto",
            target_path="a.py",
            workspace_root="/managed/workspace",
        )
