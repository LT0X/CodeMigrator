"""Fixed sandbox resource defaults and concurrency formula."""

from __future__ import annotations

import math

from pydantic import ConfigDict, Field

from codemigrator.core._base import CoreModel


class ResourceLimits(CoreModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_bytes: int = Field(default=4 * 1024**3, gt=0)
    cpu_cores: int = Field(default=2, gt=0)
    disk_bytes: int = Field(default=10 * 1024**3, gt=0)
    stdout_bytes: int = Field(default=256 * 1024**2, gt=0)
    stderr_bytes: int = Field(default=256 * 1024**2, gt=0)
    file_bytes: int = Field(default=64 * 1024**2, gt=0)


DEFAULT_RESOURCE_LIMITS = ResourceLimits()


def calculate_pool_capacity(host_memory_gib: int | float, host_cpu_cores: int | float) -> int:
    """Return max(1, min(4, floor(memory/4), floor(cpu/2)))."""

    if host_memory_gib <= 0 or host_cpu_cores <= 0:
        raise ValueError("host resources must be positive")
    return max(1, min(4, math.floor(host_memory_gib / 4), math.floor(host_cpu_cores / 2)))
