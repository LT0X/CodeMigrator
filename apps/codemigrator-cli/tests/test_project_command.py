from __future__ import annotations

import json
from pathlib import Path

from codemigrator_cli.__main__ import run_command

import codemigrator.runtime as runtime
from codemigrator.core import CreateRun
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


def test_project_command_uses_full_pipeline_only_when_explicit(tmp_path: Path, monkeypatch) -> None:
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
            "--workflow",
            "full",
            "--output",
            "json",
        ]
    )

    assert code == 0
    assert json.loads(output)["workflow"] == "V6_FULL_MIGRATION"


def test_project_command_without_workflow_does_not_start_compatibility_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
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
            "--output",
            "json",
        ]
    )

    assert code == 5
    payload = json.loads(output)
    assert payload["status"] == "UNKNOWN"
    assert "run create" in payload["errors"][0]


def test_run_create_posts_frozen_request_to_v7_control_boundary(tmp_path: Path) -> None:
    request = {
        "source": {
            "repository_url": "https://github.com/example/source.git",
            "base_ref": "main",
        },
        "branch_prefix": "team/port-py",
        "frozen_artifacts": {
            "spec": {"sha256": "a" * 64, "size": 10, "media_type": "application/json"},
            "understanding_dossier": {
                "sha256": "b" * 64,
                "size": 20,
                "media_type": "application/json",
            },
            "target_project_blueprint": {
                "sha256": "c" * 64,
                "size": 30,
                "media_type": "application/json",
            },
            "migration_rulebook": {
                "sha256": "d" * 64,
                "size": 40,
                "media_type": "application/json",
            },
        },
    }
    assert CreateRun.model_validate(request)
    request_file = tmp_path / "create-run.json"
    request_file.write_text(json.dumps(request), encoding="utf-8")

    class Creator:
        def create(self, payload: dict[str, object], idempotency_key: str) -> dict[str, object]:
            assert payload == request
            assert idempotency_key == "draft-confirmation-17"
            return {
                "run_id": "run-1",
                "status": "CREATED",
                "version": 1,
                "internal_receipt": "must not be projected",
            }

    code, output = run_command(
        [
            "run",
            "create",
            str(request_file),
            "--idempotency-key",
            "draft-confirmation-17",
            "--output",
            "json",
        ],
        control=Creator(),  # type: ignore[arg-type]
    )

    assert code == 0
    assert json.loads(output) == {"run_id": "run-1", "status": "CREATED", "version": 1}
    assert "internal_receipt" not in output


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
            "--workflow",
            "full",
            "--from-phase",
            "VERIFY",
            "--output",
            "json",
        ]
    )

    assert code == 5
    payload = json.loads(output)
    assert "only supported with --workflow legacy" in payload["errors"][0]
