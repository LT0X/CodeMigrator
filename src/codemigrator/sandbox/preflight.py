"""Fail-closed host capability checks for inline bubblewrap execution."""

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path

from pydantic import ConfigDict, Field, field_validator

from codemigrator.core._base import CoreModel


class PreflightFacts(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kernel_release: str
    cgroup_v2: bool
    bubblewrap_version: str
    user_namespace: bool
    architecture: str
    disk_free_bytes: int = Field(ge=0)
    toolchain_image_digest: str
    seccomp_sha256: str

    @field_validator("toolchain_image_digest")
    @classmethod
    def image_digest_is_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value):
            raise ValueError("toolchain_image_digest must be a sha256 digest")
        return value.lower()

    @field_validator("seccomp_sha256")
    @classmethod
    def seccomp_digest_is_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("seccomp_sha256 must be 64 hexadecimal characters")
        return value.lower()


class PreflightRequirements(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_kernel: str = "5.15"
    min_bubblewrap: str = "0.8"
    architecture: str = "x86_64"
    min_disk_free_bytes: int = Field(default=10 * 1024**3, ge=0)
    toolchain_image_digest: str
    seccomp_sha256: str

    @field_validator("toolchain_image_digest")
    @classmethod
    def image_digest_is_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value):
            raise ValueError("toolchain_image_digest must be a sha256 digest")
        return value.lower()

    @field_validator("seccomp_sha256")
    @classmethod
    def seccomp_digest_is_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("seccomp_sha256 must be 64 hexadecimal characters")
        return value.lower()


class PreflightResult(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    reasons: tuple[str, ...] = ()


def _version(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if match is None:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def check_preflight(facts: PreflightFacts, requirements: PreflightRequirements) -> PreflightResult:
    """Check all required capabilities; any unknown or mismatch blocks execution."""

    reasons: list[str] = []
    if _version(facts.kernel_release) < _version(requirements.min_kernel):
        reasons.append("kernel_release")
    if not facts.cgroup_v2:
        reasons.append("cgroup_v2")
    if _version(facts.bubblewrap_version) < _version(requirements.min_bubblewrap):
        reasons.append("bubblewrap_version")
    if not facts.user_namespace:
        reasons.append("user_namespace")
    if facts.architecture != requirements.architecture:
        reasons.append("architecture")
    if facts.disk_free_bytes < requirements.min_disk_free_bytes:
        reasons.append("disk_free_bytes")
    if facts.toolchain_image_digest != requirements.toolchain_image_digest:
        reasons.append("toolchain_image_digest")
    if facts.seccomp_sha256 != requirements.seccomp_sha256:
        reasons.append("seccomp_sha256")
    return PreflightResult(ready=not reasons, reasons=tuple(reasons))


def read_preflight_facts(
    *,
    bwrap_executable: str = "/usr/bin/bwrap",
    disk_path: str = "/tmp",
    toolchain_image_digest: str,
    seccomp_sha256: str,
) -> PreflightFacts:
    """Collect only local, read-only host facts for a later explicit policy check."""

    try:
        bwrap = subprocess.run(
            [bwrap_executable, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        bwrap = "0.0.0"
    stat = os.statvfs(disk_path)
    cgroup = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    try:
        user_namespace = (
            Path("/proc/sys/user/max_user_namespaces").read_text(encoding="ascii").strip() != "0"
        )
    except (OSError, UnicodeError):
        user_namespace = False
    return PreflightFacts(
        kernel_release=platform.release(),
        cgroup_v2=cgroup,
        bubblewrap_version=bwrap.removeprefix("bubblewrap "),
        user_namespace=user_namespace,
        architecture=platform.machine(),
        disk_free_bytes=stat.f_frsize * stat.f_bavail,
        toolchain_image_digest=toolchain_image_digest,
        seccomp_sha256=seccomp_sha256,
    )
