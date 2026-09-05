from __future__ import annotations

import json
from pathlib import Path

from codemigrator_cli.__main__ import run_command

import codemigrator.runtime as runtime
from codemigrator.runtime import ProjectMigrationPipelineReport, ProjectMigrationReport


def test_project_command_delegates_to_local_runner(tmp_path: Path, monkeypatch) -> None:
    class FakeTranslator:
        @classmethod
        def from_key_file(cls, path: Path) -> FakeTranslator:
            assert path.name == "model.json"
            return cls()

        def close(self) -> None:
            return None

    class FakeRunner:
        def run(self, request: object) -> ProjectMigrationReport:
            del request
            return ProjectMigrationReport(
                status="COMPLETED",
                phase="REPORT",
                source_digest="a" * 64,
                target="target",
                state_dir="state",
                included_files=2,
                translated_files=1,
                copied_files=1,
            )

    monkeypatch.setattr(runtime, "OpenAIProjectTranslator", FakeTranslator)
    monkeypatch.setattr(runtime, "ProjectMigrationRunner", FakeRunner)

    code, output = run_command(
        [
            "migrate",
            "project",
            str(tmp_path / "source"),
            "--target",
            str(tmp_path / "target"),
            "--api-key-file",
            str(tmp_path / "model.json"),
            "--workflow",
            "legacy",
            "--output",
            "json",
        ]
    )

    assert code == 0
    assert json.loads(output)["status"] == "COMPLETED"


def test_project_command_uses_full_pipeline_by_default(tmp_path: Path, monkeypatch) -> None:
    class FakeTranslator:
        @classmethod
        def from_key_file(cls, path: Path) -> FakeTranslator:
            assert path.name == "model.json"
            return cls()

        def close(self) -> None:
            return None

    class FakePipeline:
        def run(self, request: object) -> ProjectMigrationPipelineReport:
            del request
            return ProjectMigrationPipelineReport(
                status="COMPLETED",
                stage="COMPLETED",
                source_digest="a" * 64,
                target="target",
                state_dir="state",
                stage_dir="state/stages",
                plan_hash="b" * 64,
                included_files=1,
                translated_files=1,
                copied_files=0,
            )

    monkeypatch.setattr(runtime, "OpenAIProjectTranslator", FakeTranslator)
    monkeypatch.setattr(runtime, "ProjectMigrationPipeline", FakePipeline)

    code, output = run_command(
        [
            "migrate",
            "project",
            str(tmp_path / "source"),
            "--target",
            str(tmp_path / "target"),
            "--api-key-file",
            str(tmp_path / "model.json"),
            "--output",
            "json",
        ]
    )

    assert code == 0
    assert json.loads(output)["workflow"] == "V6_FULL_MIGRATION"


def test_full_project_command_rejects_legacy_from_phase(tmp_path: Path, monkeypatch) -> None:
    class UnexpectedTranslator:
        @classmethod
        def from_key_file(cls, path: Path) -> UnexpectedTranslator:
            raise AssertionError(f"translator should not load: {path}")

    monkeypatch.setattr(runtime, "OpenAIProjectTranslator", UnexpectedTranslator)
    code, output = run_command(
        [
            "migrate",
            "project",
            str(tmp_path / "source"),
            "--target",
            str(tmp_path / "target"),
            "--api-key-file",
            str(tmp_path / "model.json"),
            "--from-phase",
            "VERIFY",
            "--output",
            "json",
        ]
    )

    assert code == 5
    payload = json.loads(output)
    assert "only supported with --workflow legacy" in payload["errors"][0]
