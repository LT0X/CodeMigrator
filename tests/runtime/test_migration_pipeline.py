from __future__ import annotations

import json
from pathlib import Path

from codemigrator.runtime import (
    ProjectMigrationPipeline,
    ProjectMigrationPipelineRequest,
    TranslationResult,
    adopt_repaired_file,
    repair_generated_file,
)


class FakeTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, source_path: str, source_text: str) -> TranslationResult:
        del source_text
        self.calls.append(source_path)
        return TranslationResult(content=f"# migrated {source_path}\nvalue = 1\n")


class FakeVerification:
    def run(self, action: str, target: Path, *, timeout_secs: int) -> object:
        del action, target, timeout_secs
        return type(
            "VerificationResult",
            (),
            {"status": "PASSED", "exit_code": 0, "output_sha256": "a" * 64},
        )()


def make_source(root: Path) -> Path:
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "go.mod").write_text("module example.com/demo\n\ngo 1.22\n", encoding="utf-8")
    (root / "main.go").write_text(
        'package main\n\nfunc main() { println("hello") }\n', encoding="utf-8"
    )
    (root / "helper.go").write_text(
        "package main\n\nfunc Helper() int { return 1 }\n", encoding="utf-8"
    )
    (root / "pkg" / "value.go").write_text("package pkg\n\ntype Value struct{}\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return root


def test_pipeline_materializes_full_v6_stage_evidence(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    target = tmp_path / "target"
    state = tmp_path / "state"
    translator = FakeTranslator()

    report = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=translator,
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )

    assert report.status == "COMPLETED"
    assert report.stage == "COMPLETED"
    assert report.plan_hash and len(report.plan_hash) == 64
    assert tuple(report.completed_stages) == (
        "PREFLIGHT",
        "NAVIGATION",
        "DRAFT_ALIGNMENT",
        "PLANNING",
        "EXECUTE",
        "VERIFY_INTEGRATE",
        "REPORT",
    )
    expected = {
        "00-preflight/source-snapshot.json",
        "01-navigation/navigation-map.json",
        "02-draft-alignment/domain-skeleton.json",
        "02-draft-alignment/alignment.json",
        "02-draft-alignment/spec.json",
        "02-draft-alignment/understanding-dossier.json",
        "02-draft-alignment/target-project-blueprint.json",
        "02-draft-alignment/migration-rulebook.json",
        "02-draft-alignment/freeze-receipt.json",
        "03-planning/proposal.json",
        "03-planning/validation.json",
        "03-planning/frozen-plan.json",
        "04-execution/file-results.json",
        "05-verify-integrate/checks.json",
        "05-verify-integrate/integration-manifest.json",
        "06-report/report.json",
        "06-report/report.md",
    }
    actual = {
        path.relative_to(state / "stages").as_posix()
        for path in (state / "stages").rglob("*")
        if path.is_file()
    }
    assert expected <= actual

    frozen_plan = json.loads(
        (state / "stages/03-planning/frozen-plan.json").read_text(encoding="utf-8")
    )
    assert frozen_plan["validation"]["accepted"] is True
    assert len(frozen_plan["frozen_artifacts"]) == 4
    assert (
        json.loads(
            (state / "stages/02-draft-alignment/alignment.json").read_text(encoding="utf-8")
        )["auto_aligned"]
        is True
    )
    assert all(
        "package main" not in path.read_text(encoding="utf-8") for path in state.rglob("*.json")
    )
    assert all(
        "api_key" not in path.read_text(encoding="utf-8")
        for path in state.rglob("*")
        if path.is_file()
    )
    assert (target / "main.py").exists()
    assert (target / "pkg" / "__init__.py").exists()
    assert "def __getattr__(name: str)" in (target / "pkg" / "__init__.py").read_text(
        encoding="utf-8"
    )
    execution_summary = json.loads(
        (state / "stages/04-execution/execution-summary.json").read_text(encoding="utf-8")
    )
    assert execution_summary["generated_package_scaffolds"] == ["pkg/__init__.py"]

    adopt_repaired_file(state, target, "pkg/value.py")
    state_payload = json.loads((state / "state.json").read_text(encoding="utf-8"))
    adopted = next(item for item in state_payload["files"] if item["target_path"] == "pkg/value.py")
    assert adopted["status"] == "SUCCEEDED"
    checkpoint = json.loads((state / "pipeline.json").read_text(encoding="utf-8"))
    assert checkpoint["stages"]["VERIFY_INTEGRATE"] == "PENDING"
    assert checkpoint["stages"]["REPORT"] == "PENDING"


def test_repair_generated_file_is_scoped_and_validated(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pkg").mkdir()
    (source / "pkg" / "value.go").write_text(
        "package pkg\n\nfunc Value() int { return 1 }\n", encoding="utf-8"
    )
    target = tmp_path / "target"
    (target / "pkg").mkdir(parents=True)
    (target / "pkg" / "value.py").write_text("def Value():\n    return None\n", encoding="utf-8")

    class RepairTranslator:
        def repair(
            self,
            source_path: str,
            source_text: str,
            target_text: str,
            verification_feedback: str,
        ) -> TranslationResult:
            assert source_path == "pkg/value.go"
            assert "func Value" in source_text
            assert "return None" in target_text
            assert "verification failed" in verification_feedback
            return TranslationResult("def Value():\n    return 1\n")

    result = repair_generated_file(
        source,
        target,
        "pkg/value.go",
        RepairTranslator(),  # type: ignore[arg-type]
        "verification failed",
    )

    assert result.content == "def Value():\n    return 1\n"
    assert (target / "pkg" / "value.py").read_text(encoding="utf-8") == result.content


def test_pipeline_resume_skips_completed_model_work(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    target = tmp_path / "target"
    state = tmp_path / "state"
    first_translator = FakeTranslator()
    first = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=first_translator,
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )
    assert first.status == "COMPLETED"

    class NoCallTranslator(FakeTranslator):
        def translate(self, source_path: str, source_text: str) -> TranslationResult:
            raise AssertionError(f"resume unexpectedly translated {source_path}")

    resumed = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            translator=NoCallTranslator(),
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )
    assert resumed.status == "COMPLETED"
    assert resumed.plan_hash == first.plan_hash


def test_pipeline_resume_reexecutes_only_missing_file_after_execute_complete(
    tmp_path: Path,
) -> None:
    source = make_source(tmp_path / "source")
    target = tmp_path / "target"
    state = tmp_path / "state"
    first = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=FakeTranslator(),
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )
    assert first.status == "COMPLETED"
    (target / "pkg" / "value.py").unlink()

    class RecordingResumeTranslator(FakeTranslator):
        pass

    translator = RecordingResumeTranslator()
    resumed = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            translator=translator,
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )

    assert resumed.status == "COMPLETED"
    assert translator.calls == ["pkg/value.go"]
    assert (target / "pkg" / "value.py").exists()


def test_pipeline_resumes_only_the_failed_execution_file(tmp_path: Path) -> None:
    source = make_source(tmp_path / "source")
    (source / "other.go").write_text(
        "package main\n\nfunc Other() int { return 2 }\n", encoding="utf-8"
    )
    target = tmp_path / "target"
    state = tmp_path / "state"

    class FailOneTranslator(FakeTranslator):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        def translate(self, source_path: str, source_text: str) -> TranslationResult:
            if source_path == "other.go" and self.fail:
                self.calls.append(source_path)
                raise ValueError("simulated file failure")
            return super().translate(source_path, source_text)

    failing = FailOneTranslator()
    first = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=failing,
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )
    assert first.status == "FAILED"
    checkpoint = json.loads((state / "pipeline.json").read_text(encoding="utf-8"))
    assert checkpoint["stages"]["NAVIGATION"] == "COMPLETE"
    assert checkpoint["stages"]["DRAFT_ALIGNMENT"] == "COMPLETE"
    assert checkpoint["stages"]["PLANNING"] == "COMPLETE"
    assert checkpoint["stages"]["EXECUTE"] == "FAILED"

    recovered = FakeTranslator()
    second = ProjectMigrationPipeline().run(
        ProjectMigrationPipelineRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            translator=recovered,
            verification_runner=FakeVerification(),
            max_parallelism=1,
        )
    )
    assert second.status == "COMPLETED"
    assert recovered.calls == ["other.go"]
