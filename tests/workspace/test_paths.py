from __future__ import annotations

from pathlib import Path

import pytest

from codemigrator.workspace import PathSecurityError, SecureRoot


@pytest.mark.parametrize("path", ["/tmp/x", "~/x", "a/../x", ".git/config", "a\\b", "a//b"])
def test_path_gate_rejects_unsafe_shapes(tmp_path: Path, path: str) -> None:
    root = SecureRoot("workspace", tmp_path)
    with pytest.raises(PathSecurityError):
        root.validate(path)
    root.close()


def test_path_gate_does_not_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "link").symlink_to(outside)
    root = SecureRoot("workspace", workspace)
    with pytest.raises(PathSecurityError):
        root.read_bytes("link")
    root.close()


def test_atomic_write_has_no_temp_residue_and_preserves_target_on_failure(tmp_path: Path) -> None:
    root = SecureRoot("workspace", tmp_path)
    root.write_atomic("file.txt", b"old")
    assert root.read_bytes("file.txt") == b"old"
    root.write_atomic("file.txt", b"new")
    assert root.read_bytes("file.txt") == b"new"
    assert not list(tmp_path.glob(".codemigrator-tmp-*"))
    root.close()


def test_atomic_write_safely_creates_missing_parent_directories(tmp_path: Path) -> None:
    root = SecureRoot("workspace", tmp_path)

    root.write_atomic("nested/output/file.txt", b"content")

    assert root.read_bytes("nested/output/file.txt") == b"content"
    assert (tmp_path / "nested" / "output").is_dir()
    root.close()


def test_directory_is_not_a_readable_or_writable_file(tmp_path: Path) -> None:
    (tmp_path / "directory").mkdir()
    root = SecureRoot("workspace", tmp_path)

    with pytest.raises(PathSecurityError):
        root.read_bytes("directory")
    with pytest.raises(PathSecurityError):
        root.write_atomic("directory", b"content")
    root.close()


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="FIFO is unavailable")
def test_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    import os

    os.mkfifo(tmp_path / "pipe")
    root = SecureRoot("workspace", tmp_path)

    with pytest.raises(PathSecurityError):
        root.read_bytes("pipe")
    with pytest.raises(PathSecurityError):
        root.write_atomic("pipe", b"content")
    root.close()
