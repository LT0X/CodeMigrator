"""Closed command and bubblewrap policy contracts for sandbox execution."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from codemigrator.core import CheckAction, CheckCommandTemplate
from codemigrator.core._base import CoreModel
from codemigrator.core.paths import canonical_json_bytes

_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_SAFE_ENVIRONMENT = frozenset(
    {"PATH", "HOME", "LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
)
_FORBIDDEN_MOUNT_NAMES = frozenset(
    {
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/var/run/postgresql",
        "/run/postgresql",
        "/run/ssh-agent.sock",
        "/tmp/ssh-agent.sock",
    }
)
_PROTECTED_MOUNT_TARGETS = frozenset({"/dev", "/proc", "/sys", "/tmp", "/workspace", "/cache"})


class NetworkMode(str, Enum):
    """The two network profiles defined by M-09."""

    Deny = "DENY"
    Shell = "SHELL"


class FrozenCommand(CoreModel):
    """Descriptor-owned command; no caller-controlled shell or bwrap flags."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: CheckAction
    program: str
    argv: tuple[str, ...]
    timeout_secs: int = Field(gt=0)
    template_sha256: str
    toolchain_image_digest: str

    @field_validator("program")
    @classmethod
    def program_is_basename(cls, value: str) -> str:
        if not value or "\x00" in value or "/" in value:
            raise ValueError("program must be a non-empty executable name")
        return value

    @field_validator("argv")
    @classmethod
    def argv_contains_no_nul(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in item for item in value):
            raise ValueError("argv must not contain NUL bytes")
        return value

    @field_validator("template_sha256")
    @classmethod
    def template_hash_is_sha256(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("template_sha256 must be 64 hexadecimal characters")
        return value.lower()

    @field_validator("toolchain_image_digest")
    @classmethod
    def image_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("toolchain_image_digest must be a sha256 digest")
        return value.lower()

    @model_validator(mode="after")
    def template_digest_matches_payload(self) -> FrozenCommand:
        payload = {
            "action": self.action.value,
            "program": self.program,
            "argv": list(self.argv),
            "timeout_secs": self.timeout_secs,
        }
        expected = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if self.template_sha256 != expected:
            raise ValueError("template_sha256 does not match the frozen command payload")
        return self


class ShellCommand(CoreModel):
    """Free Shell/Exec feedback command, deliberately separate from FrozenCommand."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    program: str
    argv: tuple[str, ...] = ()

    @field_validator("program")
    @classmethod
    def program_is_nonempty(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("shell program must be non-empty and NUL-free")
        return value

    @field_validator("argv")
    @classmethod
    def argv_contains_no_nul(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in item for item in value):
            raise ValueError("shell argv must not contain NUL bytes")
        return value


class BwrapPolicy(CoreModel):
    """Validated inputs used to construct the complete fixed bwrap argv."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: Literal["/usr/bin/bwrap"] = "/usr/bin/bwrap"
    rootfs: str
    validation_dir: str
    toolchain_image_digest: str
    cache_dir: str | None = None
    seccomp_fd: int = Field(ge=0)
    seccomp_sha256: str
    network_mode: NetworkMode = NetworkMode.Deny
    proxy_url: str | None = None
    environment: Mapping[str, str] = Field(default_factory=dict)
    extra_read_only_mounts: tuple[tuple[str, str], ...] = ()

    @field_validator("executable", "rootfs", "validation_dir", "cache_dir")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None and not is_safe_workspace_path(value):
            raise ValueError("sandbox paths must be private absolute paths without traversal")
        return value

    @field_validator("environment")
    @classmethod
    def environment_is_allowlisted(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        unknown = set(value) - _SAFE_ENVIRONMENT
        if unknown:
            raise ValueError(f"environment contains non-allowlisted names: {sorted(unknown)}")
        if any("\x00" in key or "\x00" in item for key, item in value.items()):
            raise ValueError("environment must not contain NUL bytes")
        return dict(value)

    @field_validator("extra_read_only_mounts")
    @classmethod
    def mounts_are_safe(cls, value: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        for source, target in value:
            if not is_safe_workspace_path(source) or not is_safe_workspace_path(target):
                raise ValueError("mount paths must be private absolute paths without traversal")
            if source in _FORBIDDEN_MOUNT_NAMES or target in _FORBIDDEN_MOUNT_NAMES:
                raise ValueError("forbidden mount exposes a host control socket")
            if target in _PROTECTED_MOUNT_TARGETS or any(
                target.startswith(protected + "/") for protected in _PROTECTED_MOUNT_TARGETS
            ):
                raise ValueError("forbidden mount shadows a sandbox-managed path")
            if not (
                _is_managed_path(source, ("/opt/toolchain", "/opt/toolchains", "/opt/cache"))
            ):
                raise ValueError("forbidden mount source is outside managed roots")
            if target.startswith("/workspace/"):
                raise ValueError("forbidden mount target shadows validation contents")
            if "\x00" in source or "\x00" in target:
                raise ValueError("mount paths must not contain NUL bytes")
        return value

    @model_validator(mode="after")
    def network_policy_is_explicit(self) -> BwrapPolicy:
        if not _is_managed_path(self.rootfs, ("/opt/toolchain", "/opt/toolchains")):
            raise ValueError("rootfs must be under the managed toolchain root")
        if not _is_validation_path(self.validation_dir):
            raise ValueError("validation_dir must be an app-managed validation path")
        if self.cache_dir is not None and not _is_managed_path(self.cache_dir, ("/opt/cache",)):
            raise ValueError("cache_dir must be under the managed dependency cache root")
        if self.network_mode is NetworkMode.Deny and self.proxy_url is not None:
            raise ValueError("proxy_url is only valid for shell network mode")
        if self.network_mode is NetworkMode.Shell and not self.proxy_url:
            raise ValueError("shell network mode requires an explicit proxy_url")
        if self.proxy_url is not None:
            parsed = urlsplit(self.proxy_url)
            try:
                parsed_port = parsed.port
            except ValueError as exc:
                raise ValueError("proxy_url must contain a valid port") from exc
            if (
                parsed.scheme != "http"
                or not parsed.hostname
                or parsed_port is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("proxy_url must be an explicit HTTP endpoint")
            if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("proxy_url must address the veth proxy endpoint")
            if any(
                self.environment.get(name) not in {None, self.proxy_url}
                for name in ("HTTP_PROXY", "HTTPS_PROXY")
            ):
                raise ValueError("proxy environment must point to proxy_url")
        return self

    @field_validator("toolchain_image_digest")
    @classmethod
    def image_digest_is_sha256(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("toolchain_image_digest must be a sha256 digest")
        return value.lower()

    @field_validator("seccomp_sha256")
    @classmethod
    def seccomp_digest_is_sha256(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("seccomp_sha256 must be 64 hexadecimal characters")
        return value.lower()


def freeze_check_command(
    template: CheckCommandTemplate,
    *,
    template_sha256: str | None = None,
    toolchain_image_digest: str,
) -> FrozenCommand:
    """Hash the descriptor template and copy it into the executor command shape."""

    computed_hash = hashlib.sha256(canonical_json_bytes(template)).hexdigest()
    if template_sha256 is not None and template_sha256.lower() != computed_hash:
        raise ValueError("template_sha256 does not match the canonical command template")

    return FrozenCommand(
        action=template.action,
        program=template.program,
        argv=tuple(template.argv),
        timeout_secs=template.timeout_secs,
        template_sha256=computed_hash,
        toolchain_image_digest=toolchain_image_digest,
    )


def build_bwrap_argv(policy: BwrapPolicy, command: FrozenCommand) -> list[str]:
    """Build a deterministic, non-shell bubblewrap invocation."""

    if command.toolchain_image_digest != policy.toolchain_image_digest:
        raise ValueError("command image digest does not match the policy rootfs digest")
    argv = _build_bwrap_prefix(policy)
    environment = _build_environment(policy)
    for name, value in sorted(environment.items()):
        argv.extend(("--setenv", name, value))
    argv.extend(("--chdir", "/workspace", "--", command.program, *command.argv))
    return argv


def build_shell_bwrap_argv(policy: BwrapPolicy, command: ShellCommand) -> list[str]:
    """Build the separate free-command Shell profile without ``shell=True``."""

    if policy.network_mode is not NetworkMode.Shell:
        raise ValueError("Shell commands require the explicit shell network profile")
    argv = _build_bwrap_prefix(policy)
    for name, value in sorted(_build_environment(policy).items()):
        argv.extend(("--setenv", name, value))
    argv.extend(("--chdir", "/workspace", "--", command.program, *command.argv))
    return argv


def _build_bwrap_prefix(policy: BwrapPolicy) -> list[str]:
    argv = [
        policy.executable,
        "--unshare-all",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--cap-drop",
        "ALL",
        "--ro-bind",
        policy.rootfs,
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--bind",
        policy.validation_dir,
        "/workspace",
    ]
    if policy.cache_dir is not None:
        argv.extend(("--ro-bind", policy.cache_dir, "/cache"))
    for source, target in policy.extra_read_only_mounts:
        argv.extend(("--ro-bind", source, target))
    argv.extend(("--seccomp", str(policy.seccomp_fd)))
    return argv


def _build_environment(policy: BwrapPolicy) -> dict[str, str]:
    environment = dict(policy.environment)
    if policy.network_mode is NetworkMode.Shell and policy.proxy_url is not None:
        environment.setdefault("HTTP_PROXY", policy.proxy_url)
        environment.setdefault("HTTPS_PROXY", policy.proxy_url)
    return environment


def is_safe_workspace_path(path: str) -> bool:
    """Return whether a path is a non-root absolute path without traversal."""

    pure = PurePosixPath(path)
    return (
        path.startswith("/")
        and ".." not in pure.parts
        and path != "/"
        and "\\" not in path
        and all(ord(char) >= 0x20 for char in path)
    )


def _is_under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def _is_managed_path(path: str, roots: tuple[str, ...]) -> bool:
    try:
        resolved = str(Path(path).resolve(strict=False))
    except OSError:
        return False
    return _is_under(resolved, roots)


def _is_validation_path(path: str) -> bool:
    try:
        resolved = str(Path(path).resolve(strict=False))
    except OSError:
        return False
    return resolved == "/tmp/validation" or resolved.startswith(
        ("/tmp/validation-", "/tmp/codemigrator-validation-")
    )
