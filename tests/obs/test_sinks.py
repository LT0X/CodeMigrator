from __future__ import annotations

from pathlib import Path

from codemigrator.core import SecretRegistry
from codemigrator.runtime.observability import JsonlSegmentWriter


def test_jsonl_failure_falls_back_to_stdout_without_writing_unredacted_data(
    tmp_path: Path,
) -> None:
    output: list[str] = []
    registry = SecretRegistry()
    registry.register("private-value")
    writer = JsonlSegmentWriter(
        tmp_path / "nested" / "log",
        secret_registry=registry,
        stdout_write=output.append,
    )

    assert writer.write({"summary": "private-value"}) is False
    assert output == []
    writer.close()


def test_jsonl_writer_uses_stdout_when_log_directory_cannot_be_created(tmp_path: Path) -> None:
    output: list[str] = []
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    writer = JsonlSegmentWriter(
        blocked / "log",
        secret_registry=SecretRegistry(),
        stdout_write=output.append,
    )

    assert writer.write({"summary": "safe"}) is True
    assert len(output) == 1
    assert '"summary": "safe"' in output[0]
