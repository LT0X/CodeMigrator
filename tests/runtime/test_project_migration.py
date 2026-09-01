from __future__ import annotations

import hashlib
import json
from pathlib import Path

from codemigrator.runtime.project_migration import (
    OpenAIProjectTranslator,
    ProjectMigrationRequest,
    ProjectMigrationRunner,
    TranslationResult,
)
from codemigrator.runtime.provider import ProviderResponse, TokenUsage


class RecordingTranslator:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = set(fail or ())
        self.calls: list[str] = []

    def translate(self, source_path: str, source_text: str) -> TranslationResult:
        self.calls.append(source_path)
        if source_path in self.fail:
            raise RuntimeError("translation failed")
        return TranslationResult(
            content=(
                f"# migrated from {source_path}\n\n"
                f"# source bytes: {len(source_text.encode())}\n"
            )
        )


def make_source(root: Path) -> None:
    (root / "cmd").mkdir(parents=True)
    (root / "frontend").mkdir()
    (root / "cmd" / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (root / "internal.go").write_text("package main\nfunc helper() {}\n", encoding="utf-8")
    (root / "frontend" / "App.jsx").write_text("export default 1", encoding="utf-8")
    (root / "go.mod").write_text("module example.test/clickvideo\n\ngo 1.20\n", encoding="utf-8")


def test_project_migration_resumes_only_failed_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    state = tmp_path / "state"
    source.mkdir()
    make_source(source)

    first = RecordingTranslator(fail={"internal.go"})
    first_report = ProjectMigrationRunner().run(
        ProjectMigrationRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=first,
        )
    )

    assert first_report.status == "FAILED"
    assert first_report.failed_files == ("internal.go",)
    assert first.calls == ["cmd/main.go", "internal.go"]
    assert (target / "cmd" / "main.py").is_file()
    assert not (target / "frontend").exists()
    source_digest = first_report.source_digest
    source_before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    second = RecordingTranslator()
    second_report = ProjectMigrationRunner().run(
        ProjectMigrationRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            translator=second,
        )
    )

    assert second_report.status == "COMPLETED"
    assert second_report.source_digest == source_digest
    assert second.calls == ["internal.go"]
    assert second_report.failed_files == ()
    assert (target / "internal.py").is_file()
    assert {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    } == source_before


def test_project_migration_persists_completed_file_before_interruption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    state = tmp_path / "state"
    source.mkdir()
    make_source(source)

    class InterruptingTranslator(RecordingTranslator):
        def translate(self, source_path: str, source_text: str) -> TranslationResult:
            if source_path == "internal.go":
                raise KeyboardInterrupt
            return super().translate(source_path, source_text)

    try:
        ProjectMigrationRunner().run(
            ProjectMigrationRequest(
                source=source,
                target=target,
                state_dir=state,
                translator=InterruptingTranslator(),
            )
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the interruption should escape the migration call")

    checkpoint = (state / "state.json").read_text(encoding="utf-8")
    assert '"source_path": "cmd/main.go"' in checkpoint
    assert '"status": "SUCCEEDED"' in checkpoint

    resumed = RecordingTranslator()
    report = ProjectMigrationRunner().run(
        ProjectMigrationRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            translator=resumed,
        )
    )

    assert report.status == "COMPLETED"
    assert resumed.calls == ["internal.go"]


def test_project_migration_rejects_target_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.go").write_text("package main", encoding="utf-8")

    report = ProjectMigrationRunner().run(
        ProjectMigrationRequest(source=source, target=source / "migrated")
    )

    assert report.status == "FAILED"
    assert report.phase == "PREFLIGHT"
    assert report.failed_files == ()


def test_verify_refreshes_checkpoint_hashes_for_repaired_target_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    state = tmp_path / "state"
    source.mkdir()
    make_source(source)

    initial = ProjectMigrationRunner().run(
        ProjectMigrationRequest(
            source=source,
            target=target,
            state_dir=state,
            translator=RecordingTranslator(),
        )
    )
    assert initial.status == "COMPLETED"

    repaired = target / "internal.py"
    repaired.write_text("# repaired target\n", encoding="utf-8")
    resumed = ProjectMigrationRunner().run(
        ProjectMigrationRequest(
            source=source,
            target=target,
            state_dir=state,
            resume=True,
            from_phase="VERIFY",
        )
    )

    assert resumed.status == "COMPLETED"
    checkpoint = json.loads((state / "state.json").read_text(encoding="utf-8"))
    item = next(item for item in checkpoint["files"] if item["source_path"] == "internal.go")
    assert item["target_sha256"] == hashlib.sha256(repaired.read_bytes()).hexdigest()


def test_openai_project_translator_retries_empty_and_invalid_content(monkeypatch) -> None:
    responses = iter(
        (
            ProviderResponse(
                content="",
                tool_calls=(),
                finish_reason="stop",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            ),
            ProviderResponse(
                content="def broken(:\n    pass",
                tool_calls=(),
                finish_reason="stop",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            ),
            ProviderResponse(
                content="def migrated():\n    return True",
                tool_calls=(),
                finish_reason="stop",
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            ),
        )
    )

    requests = []

    class FakeProvider:
        def __init__(self, **kwargs) -> None:
            del kwargs

        async def complete(self, request) -> ProviderResponse:
            requests.append(request)
            return next(responses)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        "codemigrator.runtime.project_migration.OpenAICompatibleProvider", FakeProvider
    )
    translator = OpenAIProjectTranslator(
        endpoint="https://provider.invalid/v1",
        api_key="secret",
        model="test-model",
    )

    result = translator.translate("package/cache/video_test.go", "package cache")

    assert result.content == "def migrated():\n    return True\n"
    assert all("test file" in request.messages[1].content for request in requests)
