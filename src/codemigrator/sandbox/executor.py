"""Direct asyncio subprocess adapter for frozen bubblewrap commands."""

from __future__ import annotations

import asyncio
import os
from typing import Final

from pydantic import ConfigDict

from codemigrator.core import CheckStatus
from codemigrator.core._base import CoreModel

from .command import BwrapPolicy, FrozenCommand, build_bwrap_argv
from .lifecycle import CgroupProcessDomain, pdeathsig_preexec, terminate_process_group
from .limits import DEFAULT_RESOURCE_LIMITS, ResourceLimits
from .pool import SandboxExecutionPool
from .termination import TerminationCause, TerminationDecision, reduce_termination

_CHUNK_SIZE: Final = 64 * 1024


class TerminationReceipt(CoreModel):
    """Cleanup fact kept separate from a CheckResult, especially on cancellation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: TerminationCause
    returncode: int | None
    cleanup_complete: bool


class ExecutionReceipt(CoreModel):
    """Execution facts returned to the caller; acceptance belongs to runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CheckStatus | None
    cause: TerminationCause
    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    termination_receipt: TerminationReceipt


class _OutputLimit(Exception):
    def __init__(self, stream: str, data: bytes) -> None:
        super().__init__("sandbox output limit exceeded")
        self.stream = stream
        self.data = data


async def _read_limited(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while data := await stream.read(_CHUNK_SIZE):
        size += len(data)
        if size > limit:
            remaining = limit - (size - len(data))
            chunks.append(data[: max(0, remaining)])
            raise _OutputLimit(name, b"".join(chunks))
        chunks.append(data)
    return b"".join(chunks)


class SandboxExecutor:
    """Run one frozen command through a validated policy and execution slot."""

    def __init__(
        self,
        policy: BwrapPolicy,
        *,
        limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
        pool: SandboxExecutionPool | None = None,
        expected_toolchain_image_digest: str | None = None,
        expected_template_sha256: str | None = None,
        cgroup: CgroupProcessDomain | None = None,
    ) -> None:
        self.policy = policy
        self.limits = limits
        self.pool = pool
        self.expected_toolchain_image_digest = expected_toolchain_image_digest
        self.expected_template_sha256 = expected_template_sha256
        self.cgroup = cgroup

    async def execute(self, command: FrozenCommand) -> ExecutionReceipt:
        if (
            self.expected_toolchain_image_digest is not None
            and command.toolchain_image_digest != self.expected_toolchain_image_digest
        ) or (
            self.expected_template_sha256 is not None
            and command.template_sha256 != self.expected_template_sha256
        ):
            return self._receipt(
                TerminationDecision(
                    cause=TerminationCause.Infrastructure,
                    status=CheckStatus.InfrastructureError,
                    returncode=None,
                ),
                cleanup_complete=True,
            )

        if self.pool is None:
            return await self._execute(command)
        async with self.pool.slot():
            return await self._execute(command)

    async def _execute(self, command: FrozenCommand) -> ExecutionReceipt:
        cgroup_created = False
        if self.cgroup is not None:
            try:
                self.cgroup.create()
                cgroup_created = True
            except OSError:
                self._remove_cgroup()
                return self._receipt(
                    TerminationDecision(
                        cause=TerminationCause.Infrastructure,
                        status=CheckStatus.InfrastructureError,
                        returncode=None,
                    ),
                    cleanup_complete=False,
                )
        try:
            process = await asyncio.create_subprocess_exec(
                *build_bwrap_argv(self.policy, command),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
                preexec_fn=pdeathsig_preexec if os.name == "posix" else None,
            )
        except (OSError, ValueError):
            if cgroup_created:
                self._remove_cgroup()
            return self._receipt(
                TerminationDecision(
                    cause=TerminationCause.Infrastructure,
                    status=CheckStatus.InfrastructureError,
                    returncode=None,
                ),
                cleanup_complete=not cgroup_created,
            )

        assert process.stdout is not None
        assert process.stderr is not None
        if self.cgroup is not None:
            try:
                self.cgroup.attach(process.pid)
            except OSError:
                self._terminate(process)
                await process.wait()
                cleanup_complete = self._cleanup_cgroup()
                return self._receipt(
                    TerminationDecision(
                        cause=TerminationCause.Infrastructure,
                        status=CheckStatus.InfrastructureError,
                        returncode=process.returncode,
                    ),
                    cleanup_complete=cleanup_complete,
                )

        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, self.limits.stdout_bytes, "stdout")
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, self.limits.stderr_bytes, "stderr")
        )
        wait_task = asyncio.create_task(process.wait())
        output_limit = False
        timed_out = False
        infrastructure_failure = False
        cancelled = False
        stdout = b""
        stderr = b""
        stdout_truncated = False
        stderr_truncated = False
        try:
            done, pending = await asyncio.wait(
                {wait_task, stdout_task, stderr_task},
                timeout=command.timeout_secs,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                error = task.exception()
                if isinstance(error, _OutputLimit):
                    output_limit = True
                    if error.stream == "stdout":
                        stdout, stdout_truncated = error.data, True
                    else:
                        stderr, stderr_truncated = error.data, True
                elif error is not None:
                    infrastructure_failure = True
            if pending and not output_limit:
                if infrastructure_failure:
                    self._terminate(process)
                else:
                    timed_out = True
                    self._terminate(process)
        except asyncio.CancelledError:
            cancelled = True
            self._terminate(process)
        finally:
            self._terminate_if_needed(
                process, output_limit or timed_out or infrastructure_failure or cancelled
            )
            await asyncio.gather(
                wait_task, stdout_task, stderr_task, return_exceptions=True
            )
            stdout, stderr, stdout_truncated, stderr_truncated = self._collect_streams(
                stdout_task,
                stderr_task,
                stdout,
                stderr,
                stdout_truncated,
                stderr_truncated,
            )
            cleanup_complete = self._cleanup_cgroup()

        decision = reduce_termination(
            cancelled=cancelled,
            output_limit=output_limit,
            timed_out=timed_out,
            infrastructure_failure=infrastructure_failure,
            seccomp_denied=False,
            returncode=process.returncode,
        )
        return self._receipt(
            decision,
            cleanup_complete=cleanup_complete,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _terminate(self, process: object) -> None:
        terminate_process_group(process)
        if self.cgroup is not None:
            self.cgroup.kill()

    def _terminate_if_needed(self, process: object, needed: bool) -> None:
        if needed:
            self._terminate(process)

    def _cleanup_cgroup(self) -> bool:
        if self.cgroup is None:
            return True
        self.cgroup.kill()
        complete = self.cgroup.wait_empty()
        self._remove_cgroup()
        return complete

    def _remove_cgroup(self) -> None:
        if self.cgroup is not None:
            try:
                self.cgroup.remove()
            except OSError:
                pass

    @staticmethod
    def _collect_streams(
        stdout_task: asyncio.Task[bytes],
        stderr_task: asyncio.Task[bytes],
        stdout: bytes,
        stderr: bytes,
        stdout_truncated: bool,
        stderr_truncated: bool,
    ) -> tuple[bytes, bytes, bool, bool]:
        for task, name in ((stdout_task, "stdout"), (stderr_task, "stderr")):
            if task.cancelled() or not task.done():
                continue
            error = task.exception()
            if isinstance(error, _OutputLimit):
                if name == "stdout":
                    stdout, stdout_truncated = error.data, True
                else:
                    stderr, stderr_truncated = error.data, True
            elif error is None:
                if name == "stdout":
                    stdout = task.result()
                else:
                    stderr = task.result()
        return stdout, stderr, stdout_truncated, stderr_truncated

    @staticmethod
    def _receipt(
        decision: TerminationDecision,
        *,
        cleanup_complete: bool,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> ExecutionReceipt:
        return ExecutionReceipt(
            status=decision.status,
            cause=decision.cause,
            returncode=decision.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            termination_receipt=TerminationReceipt(
                cause=decision.cause,
                returncode=decision.returncode,
                cleanup_complete=cleanup_complete,
            ),
        )
