"""Closed command and bubblewrap policy contracts for sandbox execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from pathlib import PurePosixPath

from pydantic import ConfigDict, Field, field_validator, model_validator

from codemigrator.core import CheckAction, CheckCommandTemplate
from codemigrator.core._base import CoreModel

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

    executable: str = "/usr/bin/bwrap"
    rootfs: str
    validation_dir: str
    cache_dir: str | None = None
    seccomp_fd: int | None = Field(default=None, ge=0)
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
            if target in _PROTECTED_MOUNT_TARGETS:
                raise ValueError("forbidden mount shadows a sandbox-managed path")
            if "\x00" in source or "\x00" in target:
                raise ValueError("mount paths must not contain NUL bytes")
        return value

    @model_validator(mode="after")
    def network_policy_is_explicit(self) -> BwrapPolicy:
        if self.network_mode is NetworkMode.Deny and self.proxy_url is not None:
            raise ValueError("proxy_url is only valid for shell network mode")
        if self.network_mode is NetworkMode.Shell and not self.proxy_url:
            raise ValueError("shell network mode requires an explicit proxy_url")
        return self


def freeze_check_command(
    template: CheckCommandTemplate,
    *,
    template_sha256: str,
    toolchain_image_digest: str,
) -> FrozenCommand:
    """Copy a descriptor template into the only command shape the executor accepts."""

    return FrozenCommand(
        action=template.action,
        program=template.program,
        argv=tuple(template.argv),
        timeout_secs=template.timeout_secs,
        template_sha256=template_sha256,
        toolchain_image_digest=toolchain_image_digest,
    )


def build_bwrap_argv(policy: BwrapPolicy, command: FrozenCommand) -> list[str]:
    """Build a deterministic, non-shell bubblewrap invocation."""

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
    if policy.seccomp_fd is not None:
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
    return path.startswith("/") and ".." not in pure.parts and path != "/"
