"""Deterministic Git primitives for candidate workspaces and output delivery.

The module deliberately keeps Git behind a small CLI boundary.  Higher-level
coordination and persistence remain outside this task; this file only owns
repository objects, refs, and their safety invariants.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import ConfigDict, field_validator

from codemigrator.core import (
    BranchPrefix,
    CandidateGeneration,
    DeliveryChannelStatus,
    GitOid,
    RepoRelativePath,
    RunId,
    SliceId,
    StableErrorCode,
    validate_branch_prefix,
    validate_candidate_generation,
)
from codemigrator.core._base import CoreModel

from .checkpoint import (
    CandidateRefConflict,
    CheckpointManifest,
    WorkspaceFileFact,
)
from .models import WorkspaceHandle

ZERO_OID = "0" * 40
EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _validate_oid(value: str, *, allow_zero: bool = True) -> str:
    if not isinstance(value, str) or _OID_PATTERN.fullmatch(value) is None:
        raise ValueError("Git OID must be a lowercase SHA-1")
    if not allow_zero and value == ZERO_OID:
        raise ValueError("zero Git OID is not a commit")
    return value


def _redact(value: str) -> str:
    return re.sub(r"(https?://[^/@\s:]+):[^/@\s]+@", r"\1:<redacted>@", value)


def _validate_ref_name(ref: str) -> str:
    if (
        not isinstance(ref, str)
        or not ref.startswith("refs/")
        or "\x00" in ref
        or "+" in ref
        or any(char.isspace() for char in ref)
        or ".." in ref
    ):
        raise ValueError("unsafe Git ref name")
    return ref


class GitCommandError(RuntimeError):
    """A failed non-interactive Git command with credentials removed."""

    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_used = tuple(_redact(str(item)) for item in args)
        self.returncode = returncode
        self.stderr = _redact(stderr).strip()
        command = " ".join(self.args_used)
        detail = f": {self.stderr}" if self.stderr else ""
        super().__init__(f"git command failed ({returncode}): {command}{detail}")


class GitIntegrityError(RuntimeError):
    """The object graph or workspace facts violate a Git-side invariant."""


class RemoteRefMoved(RuntimeError):
    """The remote delivery branch changed outside the guarded transaction."""

    code = StableErrorCode.REMOTE_REF_MOVED


class GitCommandPort(Protocol):
    def run(
        self,
        *args: str,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes: ...


class GitCli:
    """Small shell-free subprocess adapter with deterministic environment."""

    def __init__(self, binary: str = "git", *, default_timeout: float = 60.0) -> None:
        self.binary = binary
        self.default_timeout = default_timeout

    def run(
        self,
        *args: str,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> bytes:
        command = [self.binary, *args]
        command_env = os.environ.copy()
        command_env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        if env is not None:
            command_env.update(env)
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=command_env,
                input=input,
                capture_output=True,
                check=False,
                shell=False,
                timeout=self.default_timeout if timeout is None else timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(args, 124, "git command timed out") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise GitCommandError(args, completed.returncode, stderr)
        return completed.stdout

    @staticmethod
    def credential_helper_env(path: Path | str) -> dict[str, str]:
        """Return a Git askpass environment after validating its trust boundary."""

        helper = validate_credential_helper(path)
        return {"GIT_ASKPASS": str(helper), "GIT_TERMINAL_PROMPT": "0"}


@dataclass(frozen=True, slots=True)
class GitRefLayout:
    """Validated refs for one migration Run."""

    run_id: uuid.UUID

    def __init__(self, run_id: RunId | uuid.UUID | str) -> None:
        object.__setattr__(self, "run_id", uuid.UUID(str(run_id)))

    @property
    def _run(self) -> str:
        return str(self.run_id)

    @property
    def base(self) -> str:
        return f"refs/codemigrator/runs/{self._run}/base"

    @property
    def verified(self) -> str:
        return f"refs/codemigrator/runs/{self._run}/verified"

    def candidate(self, slice_id: SliceId | uuid.UUID | str, generation: int) -> str:
        return self._slice_ref("candidates", slice_id, generation)

    def integration(self, slice_id: SliceId | uuid.UUID | str, generation: int) -> str:
        return self._slice_ref("integration", slice_id, generation)

    def failed(self, slice_id: SliceId | uuid.UUID | str, generation: int) -> str:
        return (
            f"refs/codemigrator/failed/{self._run}/"
            f"{self._slice(slice_id)}/{self._generation(generation)}"
        )

    def abandoned(self, slice_id: SliceId | uuid.UUID | str, generation: int) -> str:
        return (
            f"refs/codemigrator/abandoned/{self._run}/"
            f"{self._slice(slice_id)}/{self._generation(generation)}"
        )

    def delivery(self, branch_prefix: BranchPrefix | str) -> str:
        prefix = validate_branch_prefix(branch_prefix)
        return f"refs/heads/{prefix}/{self._run}"

    def repair_candidate(self, repair_session_id: uuid.UUID | str, number: int) -> str:
        if type(number) is not int or number < 0:
            raise ValueError("repair candidate number must be non-negative")
        session = str(uuid.UUID(str(repair_session_id)))
        return f"refs/codemigrator/runs/{self._run}/repairs/{session}/candidates/{number}"

    def _slice_ref(self, family: str, slice_id: SliceId | uuid.UUID | str, generation: int) -> str:
        return (
            f"refs/codemigrator/runs/{self._run}/slices/"
            f"{self._slice(slice_id)}/{family}/{self._generation(generation)}"
        )

    @staticmethod
    def _slice(slice_id: SliceId | uuid.UUID | str) -> str:
        return str(uuid.UUID(str(slice_id)))

    @staticmethod
    def _generation(generation: int) -> int:
        return validate_candidate_generation(generation)


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    mode: str
    object_oid: GitOid
    path: RepoRelativePath


class FileSetApplication(CoreModel):
    """A prospective commit made by applying one checkpoint file delta."""

    model_config = ConfigDict(frozen=True)

    run_id: RunId
    slice_id: SliceId
    generation: CandidateGeneration
    source_candidate_oid: GitOid
    expected_verified_oid: GitOid
    applied_paths: list[RepoRelativePath]
    prospective_commit_oid: GitOid

    @field_validator("generation", mode="before")
    @classmethod
    def generation_is_supported(cls, value: object) -> int:
        return validate_candidate_generation(value)


class GitRunRepository:
    """Object and ref operations for one bare output repository."""

    def __init__(
        self,
        path: Path | str,
        run_id: RunId | uuid.UUID | str,
        *,
        cli: GitCommandPort | None = None,
        user_name: str = "CodeMigrator",
        user_email: str = "codemigrator@localhost",
    ) -> None:
        self.path = Path(path)
        self.run_id = uuid.UUID(str(run_id))
        self.refs = GitRefLayout(self.run_id)
        self.cli = cli or GitCli()
        self.user_name = user_name
        self.user_email = user_email
        self._source_snapshot_oid: GitOid | None = None

    @property
    def empty_tree(self) -> GitOid:
        return GitOid(EMPTY_TREE_OID)

    def _env(self, **extra: str) -> dict[str, str]:
        result = {"GIT_DIR": str(self.path)}
        result.update(extra)
        return result

    def initialize(self) -> GitRefLayout:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cli.run("init", "--bare", str(self.path))
        self.cli.run("config", "core.autocrlf", "false", env=self._env())
        self.cli.run("config", "user.name", self.user_name, env=self._env())
        self.cli.run("config", "user.email", self.user_email, env=self._env())
        try:
            existing_base = self.resolve(self.refs.base)
        except GitCommandError:
            existing_base = None
        if existing_base is not None:
            if self.resolve(self.refs.verified) != existing_base:
                raise GitIntegrityError("base and verified refs disagree")
            return self.refs
        root = GitOid(
            self.cli.run(
                "commit-tree",
                EMPTY_TREE_OID,
                "-m",
                "CodeMigrator empty output base",
                env=self._env(
                    GIT_AUTHOR_NAME=self.user_name,
                    GIT_AUTHOR_EMAIL=self.user_email,
                    GIT_COMMITTER_NAME=self.user_name,
                    GIT_COMMITTER_EMAIL=self.user_email,
                ),
            )
            .decode("ascii")
            .strip()
        )
        _validate_oid(root, allow_zero=False)
        self._create_immutable_ref(self.refs.base, root)
        self._create_immutable_ref(self.refs.verified, root)
        return self.refs

    def _create_immutable_ref(self, ref: str, oid: GitOid) -> None:
        try:
            current = self.resolve(ref)
        except GitCommandError:
            self.update_ref(ref, oid, GitOid(ZERO_OID))
            return
        if current != oid:
            raise GitIntegrityError(f"immutable ref already points to a different commit: {ref}")

    def resolve(self, ref: str) -> GitOid:
        if ref != "FETCH_HEAD":
            _validate_ref_name(ref)
        output = self.cli.run("rev-parse", "--verify", f"{ref}^{{commit}}", env=self._env())
        oid = output.decode("ascii").strip()
        return GitOid(_validate_oid(oid, allow_zero=False))

    def update_ref(self, ref: str, new_oid: GitOid, expected_oid: GitOid) -> None:
        _validate_ref_name(ref)
        _validate_oid(str(new_oid), allow_zero=False)
        _validate_oid(str(expected_oid))
        try:
            self.cli.run("update-ref", ref, str(new_oid), str(expected_oid), env=self._env())
        except GitCommandError as exc:
            raise CandidateRefConflict(f"Git ref CAS failed: {ref}") from exc

    def delete_ref(self, ref: str, expected_oid: GitOid) -> None:
        _validate_ref_name(ref)
        _validate_oid(str(expected_oid), allow_zero=False)
        try:
            self.cli.run("update-ref", "-d", ref, str(expected_oid), env=self._env())
        except GitCommandError as exc:
            raise CandidateRefConflict(f"Git ref delete CAS failed: {ref}") from exc

    def parents(self, commit_oid: GitOid | str) -> tuple[GitOid, ...]:
        oid = _validate_oid(str(commit_oid), allow_zero=False)
        fields = (
            self.cli.run("rev-list", "--parents", "-n", "1", oid, env=self._env())
            .decode()
            .split()
        )
        if not fields or fields[0] != oid:
            raise GitIntegrityError("Git commit parent listing is inconsistent")
        return tuple(GitOid(_validate_oid(value, allow_zero=False)) for value in fields[1:])

    def list_tree(self, commit_oid: GitOid | str) -> tuple[GitTreeEntry, ...]:
        oid = _validate_oid(str(commit_oid), allow_zero=False)
        raw = self.cli.run("ls-tree", "-r", "-z", oid, env=self._env())
        entries: list[GitTreeEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            header, path_bytes = record.split(b"\t", 1)
            mode, kind, object_bytes = header.split(b" ", 2)
            if kind != b"blob":
                raise GitIntegrityError("output history contains a non-blob file entry")
            path = path_bytes.decode("utf-8")
            entries.append(
                GitTreeEntry(
                    mode=mode.decode("ascii"),
                    object_oid=GitOid(
                        _validate_oid(object_bytes.decode("ascii"), allow_zero=False)
                    ),
                    path=RepoRelativePath(path),
                )
            )
        return tuple(sorted(entries, key=lambda item: str(item.path).encode("utf-8")))

    def read_blob(self, object_oid: GitOid | str) -> bytes:
        oid = _validate_oid(str(object_oid), allow_zero=False)
        return self.cli.run("cat-file", "blob", oid, env=self._env())

    def create_blob(self, data: bytes) -> GitOid:
        output = self.cli.run("hash-object", "-w", "--stdin", input=data, env=self._env())
        return GitOid(_validate_oid(output.decode("ascii").strip(), allow_zero=False))

    def create_commit(
        self,
        tree_oid: GitOid | str,
        *,
        parent: GitOid | str | None = None,
        message: str,
    ) -> GitOid:
        tree = _validate_oid(str(tree_oid), allow_zero=False)
        args = ["commit-tree", tree]
        if parent is not None:
            args.extend(("-p", _validate_oid(str(parent), allow_zero=False)))
        args.extend(("-m", message))
        output = self.cli.run(
            *args,
            env=self._env(
                GIT_AUTHOR_NAME=self.user_name,
                GIT_AUTHOR_EMAIL=self.user_email,
                GIT_COMMITTER_NAME=self.user_name,
                GIT_COMMITTER_EMAIL=self.user_email,
            ),
        )
        return GitOid(_validate_oid(output.decode("ascii").strip(), allow_zero=False))

    def materialize(self, commit_oid: GitOid | str, workspace: Path | str) -> None:
        destination = Path(workspace)
        destination.mkdir(parents=True, exist_ok=True)
        self.cli.run(
            "read-tree",
            "--reset",
            "-u",
            _validate_oid(str(commit_oid), allow_zero=False),
            env=self._env(GIT_WORK_TREE=str(destination)),
        )

    def fetch_source(self, source_url: str, base_ref: str) -> GitOid:
        if self._source_snapshot_oid is not None:
            return self._source_snapshot_oid
        if not source_url or not base_ref or "\x00" in source_url or "\x00" in base_ref:
            raise ValueError("source URL and base ref must be non-empty")
        self.cli.run("fetch", "--no-tags", source_url, base_ref, env=self._env())
        snapshot = self.resolve("FETCH_HEAD")
        self._source_snapshot_oid = snapshot
        return snapshot

    def create_candidate_ref(self, handle: WorkspaceHandle, initial_oid: GitOid | str) -> str:
        ref = self.refs.candidate(handle.slice_id, handle.generation)
        self.update_ref(ref, GitOid(str(initial_oid)), GitOid(ZERO_OID))
        return ref

    def candidate_ref(self, handle: WorkspaceHandle) -> str:
        return self.refs.candidate(handle.slice_id, handle.generation)

    def preserve_candidate_evidence(
        self,
        kind: str,
        slice_id: SliceId | uuid.UUID | str,
        generation: int,
        candidate_oid: GitOid,
    ) -> str:
        if kind not in {"failed", "abandoned"}:
            raise ValueError("candidate evidence kind must be failed or abandoned")
        ref = (
            self.refs.failed(slice_id, generation)
            if kind == "failed"
            else self.refs.abandoned(slice_id, generation)
        )
        self._create_immutable_ref(ref, candidate_oid)
        return ref

    def _workspace_file(self, handle: WorkspaceHandle, path: str) -> Path:
        root = Path(handle.path).resolve()
        candidate = root.joinpath(*path.split("/"))
        for parent in candidate.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise GitIntegrityError(f"workspace path traverses a symlink: {path}")
        return candidate

    def _build_workspace_tree(
        self, handle: WorkspaceHandle, files: Sequence[WorkspaceFileFact]
    ) -> GitOid:
        by_path: dict[str, WorkspaceFileFact] = {}
        for fact in files:
            path = str(fact.path)
            if path in by_path:
                raise GitIntegrityError(f"duplicate workspace file: {path}")
            self._validate_workspace_path(path)
            by_path[path] = fact

        with tempfile.TemporaryDirectory(prefix=".codemigrator-index-") as index_root:
            index = str(Path(index_root) / "index")
            env = self._env(GIT_INDEX_FILE=index)
            self.cli.run("read-tree", "--empty", env=env)
            for path in sorted(by_path, key=lambda item: item.encode("utf-8")):
                fact = by_path[path]
                filesystem_path = self._workspace_file(handle, path)
                if fact.symlink_target is not None:
                    if not filesystem_path.is_symlink():
                        raise GitIntegrityError(f"workspace fact is not a symlink: {path}")
                    actual = os.readlink(filesystem_path)
                    content = actual.encode("utf-8")
                    mode = "120000"
                    if actual != fact.symlink_target:
                        raise GitIntegrityError(f"workspace symlink fact drifted: {path}")
                else:
                    if filesystem_path.is_symlink() or not filesystem_path.is_file():
                        raise GitIntegrityError(f"workspace fact is not a regular file: {path}")
                    content = filesystem_path.read_bytes()
                    mode = "100644"
                if len(content) != fact.size:
                    raise GitIntegrityError(f"workspace file size drifted: {path}")
                import hashlib

                if hashlib.sha256(content).hexdigest() != fact.sha256:
                    raise GitIntegrityError(f"workspace file digest drifted: {path}")
                object_oid = self.create_blob(content)
                self.cli.run(
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode},{object_oid},{path}",
                    env=env,
                )
            output = self.cli.run("write-tree", env=env).decode("ascii").strip()
        return GitOid(_validate_oid(output, allow_zero=False))

    @staticmethod
    def _validate_workspace_path(path: str) -> None:
        if not path or "\x00" in path or path.startswith(("/", "~")) or "\\" in path:
            raise GitIntegrityError(f"unsafe workspace path: {path!r}")
        if any(part in {"", ".", "..", ".git"} for part in path.split("/")):
            raise GitIntegrityError(f"unsafe workspace path: {path!r}")

    def apply_file_set(
        self,
        source_candidate_oid: GitOid | str,
        expected_verified_oid: GitOid | str,
        slice_id: SliceId | uuid.UUID | str,
        generation: int,
    ) -> FileSetApplication:
        source = GitOid(_validate_oid(str(source_candidate_oid), allow_zero=False))
        expected = GitOid(_validate_oid(str(expected_verified_oid), allow_zero=False))
        slice_uuid = uuid.UUID(str(slice_id))
        generation_value = validate_candidate_generation(generation)
        source_parents = self.parents(source)
        if len(source_parents) != 1:
            raise GitIntegrityError("candidate checkpoint must have exactly one parent")
        before = {str(item.path): item for item in self.list_tree(source_parents[0])}
        after = {str(item.path): item for item in self.list_tree(source)}
        changes = [path for path in set(before) | set(after) if before.get(path) != after.get(path)]
        applied_paths = sorted(changes, key=lambda item: item.encode("utf-8"))

        with tempfile.TemporaryDirectory(prefix=".codemigrator-index-") as index_root:
            env = self._env(GIT_INDEX_FILE=str(Path(index_root) / "index"))
            self.cli.run("read-tree", expected, env=env)
            for path in applied_paths:
                entry = after.get(path)
                if entry is None:
                    self.cli.run(
                        "update-index",
                        "--index-info",
                        input=f"0 {ZERO_OID}\t{path}\n".encode(),
                        env=env,
                    )
                else:
                    self.cli.run(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"{entry.mode},{entry.object_oid},{path}",
                        env=env,
                    )
            tree = GitOid(
                _validate_oid(
                    self.cli.run("write-tree", env=env).decode().strip(), allow_zero=False
                )
            )
        prospective = self.create_commit(
            tree,
            parent=expected,
            message=f"Integrate slice {slice_uuid} generation {generation_value}",
        )
        scratch = self.refs.integration(slice_uuid, generation_value)
        self._create_immutable_ref(scratch, prospective)
        return FileSetApplication(
            run_id=RunId(self.run_id),
            slice_id=SliceId(slice_uuid),
            generation=CandidateGeneration(generation_value),
            source_candidate_oid=source,
            expected_verified_oid=expected,
            applied_paths=[RepoRelativePath(path) for path in applied_paths],
            prospective_commit_oid=prospective,
        )

    def export_verified(self, verified_oid: GitOid | str, destination: Path | str) -> None:
        oid = _validate_oid(str(verified_oid), allow_zero=False)
        target = Path(destination)
        if os.path.lexists(target):
            raise FileExistsError(target)
        parent = target.parent
        if not parent.exists():
            raise FileNotFoundError(parent)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(parent)))
        try:
            archive = self.cli.run("archive", "--format=tar", oid, env=self._env())
            with tarfile.open(fileobj=BytesIO(archive), mode="r:") as stream:
                for member in stream.getmembers():
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise GitIntegrityError("Git archive contains an unsafe path")
                    output = temporary.joinpath(*member_path.parts)
                    if not output.resolve().is_relative_to(temporary.resolve()):
                        raise GitIntegrityError("Git archive escapes export directory")
                    if member.isdir():
                        output.mkdir(parents=True, exist_ok=True)
                    elif member.issym():
                        output.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(member.linkname, output)
                    elif member.isfile():
                        source = stream.extractfile(member)
                        if source is None:
                            raise GitIntegrityError("Git archive file has no content")
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(source.read())
                    else:
                        raise GitIntegrityError("Git archive contains an unsupported entry")
            os.replace(temporary, target)
        except BaseException:
            _remove_tree(temporary)
            raise


class GitCandidateRefStore:
    """Git implementation of the workspace checkpoint candidate port."""

    def __init__(self, repository: GitRunRepository) -> None:
        self.repository = repository

    def create_checkpoint(
        self,
        handle: WorkspaceHandle,
        manifest: CheckpointManifest,
        files: Sequence[WorkspaceFileFact],
    ) -> GitOid:
        candidate = manifest.slice_candidate
        if candidate.run_id != handle.run_id or candidate.slice_id != handle.slice_id:
            raise GitIntegrityError("checkpoint manifest identity does not match workspace")
        if candidate.generation != handle.generation:
            raise GitIntegrityError("checkpoint generation does not match workspace")
        if candidate.base_verified_oid != handle.base_verified_oid:
            raise GitIntegrityError("checkpoint base verified OID does not match workspace")
        expected = GitOid(str(candidate.candidate_commit_oid))
        if self.current_candidate_oid(handle) != expected:
            raise CandidateRefConflict("candidate ref moved before checkpoint")
        tree = self.repository._build_workspace_tree(handle, files)
        return self.repository.create_commit(
            tree,
            parent=expected,
            message=f"Checkpoint slice {handle.slice_id} generation {handle.generation}",
        )

    def advance_candidate_ref(
        self, handle: WorkspaceHandle, expected_oid: GitOid, new_oid: GitOid
    ) -> None:
        if str(expected_oid) == str(new_oid):
            return
        try:
            self.repository.update_ref(self.repository.candidate_ref(handle), new_oid, expected_oid)
        except CandidateRefConflict as exc:
            raise CandidateRefConflict("candidate ref moved") from exc

    def current_candidate_oid(self, handle: WorkspaceHandle) -> GitOid:
        try:
            return self.repository.resolve(self.repository.candidate_ref(handle))
        except GitCommandError as exc:
            raise CandidateRefConflict("candidate ref does not exist") from exc


class RecoveryAction(str, Enum):
    RETRY_GIT = "RETRY_GIT"
    COMPLETE_RECEIPT = "COMPLETE_RECEIPT"
    INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    code: StableErrorCode | None


def classify_integration_recovery(
    expected_verified_oid: GitOid,
    prospective_commit_oid: GitOid,
    observed_verified_oid: GitOid,
    receipt_exists: bool,
) -> RecoveryDecision:
    expected = str(expected_verified_oid)
    prospective = str(prospective_commit_oid)
    observed = str(observed_verified_oid)
    if not receipt_exists and observed == prospective:
        return RecoveryDecision(RecoveryAction.COMPLETE_RECEIPT, None)
    if observed == expected:
        return RecoveryDecision(RecoveryAction.RETRY_GIT, None)
    return RecoveryDecision(
        RecoveryAction.INCONSISTENT,
        StableErrorCode.RECOVERY_LEDGER_INCONSISTENT,
    )


class RepairRefQueue:
    """FIFO bookkeeping for repair refs, independent of normal generations."""

    def __init__(self, repository: GitRunRepository) -> None:
        self.repository = repository
        self._pending: deque[str] = deque()

    def enqueue(
        self, repair_session_id: uuid.UUID | str, number: int, candidate_oid: GitOid
    ) -> str:
        ref = self.repository.refs.repair_candidate(repair_session_id, number)
        self.repository._create_immutable_ref(ref, candidate_oid)
        self._pending.append(ref)
        return ref

    def dequeue(self) -> str | None:
        return self._pending.popleft() if self._pending else None


class PushGuard:
    """Guard a fixed delivery branch against remote movement."""

    def __init__(
        self,
        repository: GitRunRepository,
        branch_prefix: BranchPrefix | str,
        *,
        cli: GitCommandPort | None = None,
    ) -> None:
        self.repository = repository
        self.branch = repository.refs.delivery(branch_prefix)
        self.cli = cli or repository.cli

    def observe(self, remote: str) -> GitOid | None:
        output = self.cli.run("ls-remote", "--heads", "--", remote, self.branch)
        line = output.decode("utf-8", errors="strict").strip()
        if not line:
            return None
        oid = line.split(None, 1)[0]
        return GitOid(_validate_oid(oid, allow_zero=False))

    def publish(
        self,
        remote: str,
        verified_oid: GitOid,
        *,
        frozen_last_pushed_oid: GitOid | None = None,
        expected_remote_oid: GitOid | None = None,
    ) -> DeliveryChannelStatus:
        requested = GitOid(_validate_oid(str(verified_oid), allow_zero=False))
        if self.repository.resolve(self.repository.refs.verified) != requested:
            raise GitIntegrityError("delivery OID is not the current verified commit")
        try:
            observed = self.observe(remote)
        except GitCommandError:
            return DeliveryChannelStatus.DeliveryFailed
        if frozen_last_pushed_oid is None and expected_remote_oid is None:
            if observed is not None:
                raise RemoteRefMoved("delivery branch already exists")
        elif (
            frozen_last_pushed_oid is None
            or expected_remote_oid is None
            or observed is None
            or str(frozen_last_pushed_oid) != str(expected_remote_oid)
            or str(expected_remote_oid) != str(observed)
        ):
            raise RemoteRefMoved("delivery remote ref moved")
        refspec = f"{self.repository.refs.verified}:{self.branch}"
        try:
            self.cli.run("push", "--", remote, refspec, env=self.repository._env())
        except GitCommandError:
            return DeliveryChannelStatus.DeliveryFailed
        return DeliveryChannelStatus.Ready


def validate_credential_helper(path: Path | str) -> Path:
    """Accept only a non-symlink helper with exact owner read/write mode."""

    helper = Path(path)
    if helper.is_symlink() or not helper.is_file():
        raise ValueError("credential helper must be a regular file")
    if stat.S_IMODE(helper.stat().st_mode) != 0o600:
        raise ValueError("credential helper must have mode 0600")
    return helper


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()
