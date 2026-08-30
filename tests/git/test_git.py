from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

import pytest

from codemigrator.core import (
    CandidateGeneration,
    GitOid,
    SliceCandidate,
    StableErrorCode,
    canonical_json_bytes,
)
from codemigrator.workspace import WorkspaceFileFact, WorkspaceHandle, WorkspaceState
from codemigrator.workspace.checkpoint import CheckpointManifest
from codemigrator.workspace.git import (
    FileSetApplication,
    GitCandidateRefStore,
    GitCli,
    GitCommandError,
    GitIntegrityError,
    GitRefLayout,
    GitRunRepository,
    PushGuard,
    RecoveryAction,
    RecoveryDecision,
    RemoteRefMoved,
    RepairRefQueue,
    classify_integration_recovery,
    validate_credential_helper,
)

ZERO_OID = "0" * 40


def _run_id() -> uuid.UUID:
    return uuid.uuid4()


def _handle(run_id: uuid.UUID, slice_id: uuid.UUID, path: Path, base_oid: str) -> WorkspaceHandle:
    return WorkspaceHandle(
        run_id=run_id,
        slice_id=slice_id,
        generation=0,
        path=str(path),
        state=WorkspaceState.Iterating,
        base_verified_oid=base_oid,
    )


def _fact(path: str, data: bytes, *, symlink_target: str | None = None) -> WorkspaceFileFact:
    encoded = symlink_target.encode() if symlink_target is not None else data
    return WorkspaceFileFact(
        path=path,
        size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        symlink_target=symlink_target,
    )


def _manifest(
    handle: WorkspaceHandle, candidate_oid: str, files: list[WorkspaceFileFact]
) -> CheckpointManifest:
    file_map = {
        file.path: {
            "sha256": file.sha256,
            "size": file.size,
            "symlink_target": file.symlink_target,
        }
        for file in files
    }
    return CheckpointManifest(
        slice_candidate=SliceCandidate(
            run_id=handle.run_id,
            slice_id=handle.slice_id,
            generation=CandidateGeneration(handle.generation),
            base_verified_oid=GitOid(handle.base_verified_oid),
            candidate_commit_oid=GitOid(candidate_oid),
        ),
        file_count=len(files),
        total_bytes=sum(item.size for item in files),
        file_set_digest=hashlib.sha256(canonical_json_bytes(file_map)).hexdigest(),
        scope_check_passed=True,
    )


def test_cli_never_uses_shell_and_redacts_url_credentials() -> None:
    with pytest.raises(GitCommandError) as raised:
        GitCli().run("--git-dir", "/not-a-repository", "show", "https://alice:s3cret@example.test/repo")

    assert "s3cret" not in str(raised.value)
    assert "alice:s3cret" not in str(raised.value)
    assert raised.value.returncode != 0


def test_run_initializes_empty_output_history_and_external_worktree(tmp_path: Path) -> None:
    run_id = _run_id()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()

    base_oid = repo.resolve(refs.base)
    assert base_oid == repo.resolve(refs.verified)
    assert repo.parents(base_oid) == ()
    assert repo.list_tree(base_oid) == ()
    assert repo.initialize() == refs

    workspace = tmp_path / "workspace"
    repo.materialize(base_oid, workspace)
    assert workspace.is_dir()
    assert not (workspace / ".git").exists()
    assert not (repo.path / "index").exists()


def test_source_fetch_is_a_read_only_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    GitCli().run("init", "-q", str(source))
    GitCli().run("config", "user.name", "fixture", cwd=source)
    GitCli().run("config", "user.email", "fixture@example.test", cwd=source)
    (source / "source.txt").write_text("source", encoding="utf-8")
    GitCli().run("add", "source.txt", cwd=source)
    GitCli().run("commit", "-qm", "source", cwd=source)
    source_head = GitCli().run("rev-parse", "HEAD", cwd=source).decode().strip()
    source_stat = (source / ".git").stat()

    repo = GitRunRepository(tmp_path / "run.git", _run_id())
    repo.initialize()
    assert repo.fetch_source(str(source), "HEAD") == source_head
    assert (source / ".git").stat().st_mtime_ns == source_stat.st_mtime_ns
    restarted = GitRunRepository(repo.path, repo.run_id)
    assert restarted.fetch_source(str(source), "HEAD") == source_head


def test_source_fetch_rejects_embedded_credentials_and_unsafe_refs(tmp_path: Path) -> None:
    repo = GitRunRepository(tmp_path / "run.git", _run_id())
    repo.initialize()
    with pytest.raises(ValueError):
        repo.fetch_source("https://alice:secret@example.test/repo", "main")
    with pytest.raises(ValueError):
        repo.fetch_source("https://example.test/repo", "--upload-pack=evil")


def test_candidate_checkpoint_writes_exact_files_and_uses_cas(tmp_path: Path) -> None:
    run_id, slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src/a.py").write_bytes(b"print('a')\n")
    (workspace / "link").symlink_to("src/a.py")
    base_oid = repo.resolve(refs.base)
    handle = _handle(run_id, slice_id, workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    files = [_fact("src/a.py", b"print('a')\n"), _fact("link", b"", symlink_target="src/a.py")]
    store = GitCandidateRefStore(repo)

    commit = store.create_checkpoint(handle, _manifest(handle, base_oid, files), files)
    store.advance_candidate_ref(handle, GitOid(base_oid), commit)

    assert [entry.path for entry in repo.list_tree(commit)] == ["link", "src/a.py"]
    assert repo.parents(commit) == (base_oid,)
    assert store.current_candidate_oid(handle) == commit

    other = repo.create_commit(repo.empty_tree, parent=base_oid, message="other")
    repo.update_ref(repo.refs.candidate(slice_id, 0), other, commit)
    with pytest.raises(Exception) as raised:
        store.advance_candidate_ref(handle, commit, other)
    assert raised.type.__name__ == "CandidateRefConflict"

    with pytest.raises(Exception) as raised:
        store.advance_candidate_ref(handle, GitOid(base_oid), GitOid(base_oid))
    assert raised.type.__name__ == "CandidateRefConflict"


def test_candidate_rejects_file_fact_drift_without_creating_commit(tmp_path: Path) -> None:
    run_id, slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "a.py").write_text("actual", encoding="utf-8")
    base_oid = repo.resolve(refs.base)
    handle = _handle(run_id, slice_id, workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    store = GitCandidateRefStore(repo)

    with pytest.raises(GitIntegrityError):
        store.create_checkpoint(
            handle,
            _manifest(handle, base_oid, [_fact("a.py", b"claimed")]),
            [_fact("a.py", b"claimed")],
        )
    assert store.current_candidate_oid(handle) == base_oid


def test_candidate_rejects_forged_manifest_without_creating_commit(tmp_path: Path) -> None:
    run_id, slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "a.py").write_bytes(b"actual")
    base_oid = repo.resolve(refs.base)
    handle = _handle(run_id, slice_id, workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    store = GitCandidateRefStore(repo)
    files = [_fact("a.py", b"actual")]
    forged = _manifest(handle, base_oid, files).model_copy(
        update={"file_count": 99, "scope_check_passed": False}
    )

    with pytest.raises(GitIntegrityError):
        store.create_checkpoint(handle, forged, files)
    assert store.current_candidate_oid(handle) == base_oid


def test_file_set_application_is_exact_and_utf8_sorted(tmp_path: Path) -> None:
    run_id, slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "z.txt").write_bytes(b"z")
    (workspace / "é.txt").write_bytes(b"accent")
    base_oid = repo.resolve(refs.base)
    handle = _handle(run_id, slice_id, workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    files = [_fact("z.txt", b"z"), _fact("é.txt", b"accent")]
    candidate = GitCandidateRefStore(repo).create_checkpoint(
        handle, _manifest(handle, base_oid, files), files
    )
    GitCandidateRefStore(repo).advance_candidate_ref(handle, GitOid(base_oid), candidate)

    application = repo.apply_file_set(
        candidate,
        base_oid,
        slice_id,
        0,
        base_verified_oid=base_oid,
    )
    assert isinstance(application, FileSetApplication)
    assert application.applied_paths == ["z.txt", "é.txt"]
    assert [entry.path for entry in repo.list_tree(application.prospective_commit_oid)] == [
        "z.txt",
        "é.txt",
    ]
    assert application.expected_verified_oid == base_oid


def test_file_set_application_preserves_deletion_as_an_exact_change(tmp_path: Path) -> None:
    run_id, first_slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    base_oid = repo.resolve(refs.base)
    seed_workspace = tmp_path / "seed"
    seed_workspace.mkdir()
    (seed_workspace / "remove.txt").write_bytes(b"remove")
    seed_handle = _handle(run_id, first_slice_id, seed_workspace, base_oid)
    repo.create_candidate_ref(seed_handle, base_oid)
    store = GitCandidateRefStore(repo)
    seed_files = [_fact("remove.txt", b"remove")]
    seed = store.create_checkpoint(
        seed_handle,
        _manifest(seed_handle, base_oid, seed_files),
        seed_files,
    )
    store.advance_candidate_ref(seed_handle, GitOid(base_oid), seed)
    repo.update_ref(refs.verified, seed, base_oid)

    slice_id = uuid.uuid4()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "keep.txt").write_bytes(b"keep")
    handle = _handle(run_id, slice_id, workspace, seed)
    repo.create_candidate_ref(handle, seed)
    files = [_fact("keep.txt", b"keep")]
    candidate = store.create_checkpoint(handle, _manifest(handle, seed, files), files)
    store.advance_candidate_ref(handle, seed, candidate)
    application = repo.apply_file_set(
        candidate,
        seed,
        slice_id,
        0,
        base_verified_oid=seed,
    )

    assert application.applied_paths == ["keep.txt", "remove.txt"]
    assert [entry.path for entry in repo.list_tree(application.prospective_commit_oid)] == [
        "keep.txt"
    ]


def test_later_checkpoint_applies_unchanged_files_from_frozen_base(tmp_path: Path) -> None:
    run_id, slice_id = _run_id(), uuid.uuid4()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    refs = repo.initialize()
    base_oid = repo.resolve(refs.base)
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"a")
    (workspace / "b.txt").write_bytes(b"b1")
    handle = _handle(run_id, slice_id, workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    store = GitCandidateRefStore(repo)
    first_files = [_fact("a.txt", b"a"), _fact("b.txt", b"b1")]
    first = store.create_checkpoint(handle, _manifest(handle, base_oid, first_files), first_files)
    store.advance_candidate_ref(handle, GitOid(base_oid), first)

    (workspace / "b.txt").write_bytes(b"b2")
    second_files = [_fact("a.txt", b"a"), _fact("b.txt", b"b2")]
    second = store.create_checkpoint(handle, _manifest(handle, first, second_files), second_files)
    store.advance_candidate_ref(handle, first, second)
    application = repo.apply_file_set(
        second,
        base_oid,
        slice_id,
        0,
        base_verified_oid=base_oid,
    )

    assert application.applied_paths == ["a.txt", "b.txt"]
    assert [entry.path for entry in repo.list_tree(application.prospective_commit_oid)] == [
        "a.txt",
        "b.txt",
    ]


def test_ref_layout_rejects_generation_and_branch_injection() -> None:
    refs = GitRefLayout(_run_id())
    slice_id = uuid.uuid4()
    assert refs.candidate(slice_id, 2).endswith("/candidates/2")
    assert refs.repair_candidate(uuid.uuid4(), 0).endswith("/candidates/0")
    with pytest.raises(ValueError):
        refs.candidate(slice_id, 3)
    with pytest.raises(ValueError):
        refs.delivery("safe/+force")


def test_recovery_has_only_three_finite_outcomes() -> None:
    expected, prospective = GitOid("1" * 40), GitOid("2" * 40)
    assert classify_integration_recovery(
        expected, prospective, prospective, False
    ) == RecoveryDecision(
        action=RecoveryAction.COMPLETE_RECEIPT,
        code=None,
    )
    assert (
        classify_integration_recovery(expected, prospective, expected, False).action
        is RecoveryAction.RETRY_GIT
    )
    inconsistent = classify_integration_recovery(expected, prospective, GitOid("3" * 40), False)
    assert inconsistent.action is RecoveryAction.INCONSISTENT
    assert inconsistent.code is StableErrorCode.RECOVERY_LEDGER_INCONSISTENT


def test_repair_refs_are_separate_and_fifo(tmp_path: Path) -> None:
    repo = GitRunRepository(tmp_path / "run.git", _run_id())
    repo.initialize()
    queue = RepairRefQueue(repo)
    base_oid = repo.resolve(repo.refs.base)
    first = queue.enqueue(uuid.uuid4(), 0, base_oid)
    second = queue.enqueue(uuid.uuid4(), 0, base_oid)

    restarted = RepairRefQueue(repo)
    assert restarted.dequeue() == first
    assert restarted.dequeue() == second
    assert restarted.dequeue() is None
    assert "/repairs/" in first
    evidence = repo.preserve_candidate_evidence("failed", uuid.uuid4(), 2, base_oid)
    assert evidence.startswith("refs/codemigrator/failed/")
    with pytest.raises(ValueError):
        repo.refs.candidate(uuid.uuid4(), 3)


class _PushCli:
    def __init__(self, observed: str | None = None, *, fail_push: bool = False) -> None:
        self.observed = observed
        self.fail_push = fail_push
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **_: object) -> bytes:
        self.calls.append(args)
        if args[0] == "ls-remote":
            return b"" if self.observed is None else f"{self.observed}\trefs/heads/x\n".encode()
        if self.fail_push:
            raise GitCommandError(args, 1, "push failed")
        return b""


class _RacePushCli(_PushCli):
    def __init__(self, observed_after_failure: str) -> None:
        super().__init__()
        self.observed_after_failure = observed_after_failure
        self._observe_count = 0

    def run(self, *args: str, **kwargs: object) -> bytes:
        if args[0] == "ls-remote":
            self._observe_count += 1
            if self._observe_count == 1:
                return b""
            return f"{self.observed_after_failure}\trefs/heads/x\n".encode()
        if args[0] == "push":
            raise GitCommandError(args, 1, "non-fast-forward")
        return super().run(*args, **kwargs)


def test_push_guard_requires_stable_remote_and_never_force_pushes(tmp_path: Path) -> None:
    run_id = _run_id()
    repo = GitRunRepository(tmp_path / "run.git", run_id)
    repo.initialize()
    oid = repo.resolve(repo.refs.verified)
    cli = _PushCli()
    guard = PushGuard(repo, "codemigrator", cli=cli)

    assert guard.publish("origin", oid) == "READY"
    push_call = cli.calls[-1]
    assert all(not item.startswith("+") for item in push_call)
    assert "--force" not in push_call

    moved = PushGuard(repo, "codemigrator", cli=_PushCli("e" * 40))
    with pytest.raises(RemoteRefMoved) as raised:
        moved.publish("origin", oid, frozen_last_pushed_oid=oid, expected_remote_oid=oid)
    assert raised.value.code is StableErrorCode.REMOTE_REF_MOVED

    failed = PushGuard(repo, "codemigrator", cli=_PushCli(fail_push=True))
    assert failed.publish("origin", oid) == "DELIVERY_FAILED"

    raced = PushGuard(repo, "codemigrator", cli=_RacePushCli("e" * 40))
    with pytest.raises(RemoteRefMoved):
        raced.publish("origin", oid)


def test_push_guard_delivers_to_a_real_local_remote(tmp_path: Path) -> None:
    repo = GitRunRepository(tmp_path / "run.git", _run_id())
    repo.initialize()
    remote = tmp_path / "remote.git"
    GitCli().run("init", "--bare", str(remote))
    oid = repo.resolve(repo.refs.verified)

    guard = PushGuard(repo, "codemigrator")
    assert guard.publish(str(remote), oid) == "READY"
    delivered = GitCli().run(
        "--git-dir", str(remote), "rev-parse", "refs/heads/codemigrator/" + str(repo.run_id)
    ).decode().strip()
    assert delivered == oid


def test_export_verified_is_atomic_and_does_not_create_git_metadata(tmp_path: Path) -> None:
    repo = GitRunRepository(tmp_path / "run.git", _run_id())
    refs = repo.initialize()
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "README.md").write_text("output", encoding="utf-8")
    base_oid = repo.resolve(refs.base)
    handle = _handle(repo.run_id, uuid.uuid4(), workspace, base_oid)
    repo.create_candidate_ref(handle, base_oid)
    files = [_fact("README.md", b"output")]
    candidate = GitCandidateRefStore(repo).create_checkpoint(
        handle, _manifest(handle, base_oid, files), files
    )
    repo.update_ref(refs.verified, candidate, base_oid)
    destination = tmp_path / "export"

    repo.export_verified(candidate, destination)
    assert (destination / "README.md").read_text(encoding="utf-8") == "output"
    assert not (destination / ".git").exists()
    with pytest.raises(FileExistsError):
        repo.export_verified(candidate, destination)


def test_credential_helper_must_be_private_regular_file(tmp_path: Path) -> None:
    helper = tmp_path / "credential-helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(helper, stat.S_IRUSR | stat.S_IWUSR)
    assert validate_credential_helper(helper) == helper
    assert GitCli.credential_helper_env(helper)["GIT_ASKPASS"] == str(helper)
    os.chmod(helper, 0o644)
    with pytest.raises(ValueError):
        validate_credential_helper(helper)
