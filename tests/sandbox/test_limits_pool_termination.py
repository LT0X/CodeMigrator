from __future__ import annotations

import pytest

from codemigrator.core import CheckStatus
from codemigrator.sandbox import (
    DEFAULT_RESOURCE_LIMITS,
    SandboxExecutionPool,
    TerminationCause,
    calculate_pool_capacity,
    reduce_termination,
)


def test_pool_capacity_uses_the_frozen_three_pool_formula() -> None:
    assert calculate_pool_capacity(host_memory_gib=16, host_cpu_cores=8) == 4
    assert calculate_pool_capacity(host_memory_gib=3, host_cpu_cores=1) == 1
    assert DEFAULT_RESOURCE_LIMITS.memory_bytes == 4 * 1024**3
    assert DEFAULT_RESOURCE_LIMITS.cpu_cores == 2
    assert DEFAULT_RESOURCE_LIMITS.disk_bytes == 10 * 1024**3


@pytest.mark.asyncio
async def test_pool_slots_are_returned_after_async_context_exit() -> None:
    pool = SandboxExecutionPool(capacity=1)

    async with pool.slot():
        assert pool.in_use == 1

    assert pool.in_use == 0


def test_termination_reduction_has_one_priority_order_and_seccomp_is_failed() -> None:
    decision = reduce_termination(
        cancelled=False,
        output_limit=True,
        timed_out=True,
        infrastructure_failure=True,
        seccomp_denied=True,
        returncode=0,
    )
    assert decision.cause is TerminationCause.OutputLimit
    assert decision.status is CheckStatus.OutputLimitExceeded

    denied = reduce_termination(
        cancelled=False,
        output_limit=False,
        timed_out=False,
        infrastructure_failure=False,
        seccomp_denied=True,
        returncode=0,
    )
    assert denied.cause is TerminationCause.SeccompDenied
    assert denied.status is CheckStatus.Failed
