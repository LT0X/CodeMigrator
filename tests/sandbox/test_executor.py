from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codemigrator.core import CheckAction, CheckCommandTemplate, CheckStatus
from codemigrator.sandbox import (
    BwrapPolicy,
    SandboxExecutor,
    TerminationCause,
    freeze_check_command,
)


def command(timeout_secs: int = 120):
    return freeze_check_command(
        CheckCommandTemplate(
            action=CheckAction.Test,
            program="python",
            argv=["-c", "print('ok')"],
            timeout_secs=timeout_secs,
        ),
        template_sha256="a" * 64,
        toolchain_image_digest="sha256:" + "b" * 64,
    )


def policy() -> BwrapPolicy:
    return BwrapPolicy(
        rootfs="/opt/toolchain",
        validation_dir="/tmp/validation",
        environment={"PATH": "/usr/bin"},
    )


class FakeProcess:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode = returncode
        self.pid = 12345

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_executor_uses_fixed_argv_and_returns_process_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"ok\n")
    seen: list[tuple[Any, ...]] = []

    async def create(*args: Any, **kwargs: Any) -> FakeProcess:
        seen.append(args)
        return process

    monkeypatch.setattr("codemigrator.sandbox.executor.asyncio.create_subprocess_exec", create)
    receipt = await SandboxExecutor(policy()).execute(command())

    assert receipt.status is CheckStatus.Passed
    assert receipt.cause is TerminationCause.ProcessExit
    assert receipt.stdout == b"ok\n"
    assert seen[0][-4:] == ("--", "python", "-c", "print('ok')")


@pytest.mark.asyncio
async def test_executor_rejects_image_mismatch_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def create(*args: Any, **kwargs: Any) -> FakeProcess:
        nonlocal called
        called = True
        return FakeProcess()

    monkeypatch.setattr("codemigrator.sandbox.executor.asyncio.create_subprocess_exec", create)
    receipt = await SandboxExecutor(
        policy(), expected_toolchain_image_digest="sha256:" + "c" * 64
    ).execute(command())

    assert receipt.status is CheckStatus.InfrastructureError
    assert receipt.cause is TerminationCause.Infrastructure
    assert called is False


@pytest.mark.asyncio
async def test_executor_reduces_output_limit_and_kills_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(stdout=b"0123456789")
    killed = False

    async def create(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    def kill(_process: object) -> None:
        nonlocal killed
        killed = True

    monkeypatch.setattr("codemigrator.sandbox.executor.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("codemigrator.sandbox.executor.terminate_process_group", kill)
    limits = {
        "stdout_bytes": 4,
        "stderr_bytes": 4,
    }
    from codemigrator.sandbox import ResourceLimits

    receipt = await SandboxExecutor(policy(), limits=ResourceLimits(**limits)).execute(command())

    assert receipt.status is CheckStatus.OutputLimitExceeded
    assert receipt.cause is TerminationCause.OutputLimit
    assert killed is True
