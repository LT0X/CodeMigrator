from __future__ import annotations

from pathlib import Path

from codemigrator.sandbox import TemporaryValidationDirectory, pdeathsig_preexec


def test_validation_directory_is_removed_on_context_exit(tmp_path: Path) -> None:
    with TemporaryValidationDirectory(parent=tmp_path) as validation:
        path = validation.path
        (path / "generated.txt").write_text("untrusted", encoding="utf-8")
        assert path.is_dir()

    assert not path.exists()


def test_pdeathsig_preexec_is_a_callable_for_subprocess_creation() -> None:
    assert callable(pdeathsig_preexec)
