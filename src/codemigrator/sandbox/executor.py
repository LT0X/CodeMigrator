"""Direct asyncio subprocess adapter for frozen bubblewrap commands."""

from __future__ import annotations

import asyncio
import os
from typing import Final, Protocol

from pydantic import ConfigDict

from codemigrator.core import CheckStatus
from codemigrator.core._base import CoreModel

from .command import BwrapPolicy, FrozenCommand, NetworkMode, build_bwrap_argv
from .lifecycle import CgroupProcessDomain, pdeathsig_preexec, terminate_process_group
from .limits import (
    DEFAULT_RESOURCE_LIMITS,
    ResourceLimits,
    validation_directory_exceeds_quota,
)
from .pool import SandboxExecutionPool
from .preflight import PreflightResult
from .termination import TerminationCause, TerminationDecision, reduce_termination

_CHUNK_SIZE: Final = 64 * 1024


class NetworkAttachment(Protocol):
    """App-owned veth/firewall attachment for a newly-created Shell netns."""

    async def attach(self, pid: int) -> None: ...

    async def detach(self, pid: int) -> None: ...


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


class _QuotaLimit(Exception):
    pass


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


async def _watch_validation_quota(root: str, limits: ResourceLimits) -> None:
    while True:
        if validation_directory_exceeds_quota(root, limits):
            raise _QuotaLimit("validation directory quota exceeded")
        await asyncio.sleep(0.05)


class SandboxExecutor:
    """Run one frozen command through a validated policy and execution slot."""

    def __init__(
        self,
        policy: BwrapPolicy,
        *,
        preflight: PreflightResult,
        cgroup: CgroupProcessDomain,
        limits: ResourceLimits = DEFAULT_RESOURCE_LIMITS,
        pool: SandboxExecutionPool | None = None,
        expected_toolchain_image_digest: str | None = None,
        expected_template_sha256: str | None = None,
        network_attachment: NetworkAttachment | None = None,
    ) -> None:
        self.policy = policy
        self.preflight = preflight
        self.limits = limits
        self.pool = pool
        self.expected_toolchain_image_digest = expected_toolchain_image_digest
        self.expected_template_sha256 = expected_template_sha256
        self.cgroup = cgroup
        self.network_attachment = network_attachment

    async def execute(self, command: FrozenCommand) -> ExecutionReceipt:
        if (
            not self.preflight.ready
            or self.preflight.toolchain_image_digest != self.policy.toolchain_image_digest
            or self.preflight.seccomp_sha256 != self.policy.seccomp_sha256
            or command.toolchain_image_digest != self.policy.toolchain_image_digest
            or (
                self.policy.network_mode is NetworkMode.Shell
                and self.network_attachment is None
            )
        ):
            return self._receipt(
                TerminationDecision(
                    cause=TerminationCause.Infrastructure,
                    status=CheckStatus.InfrastructureError,
                    returncode=None,
                ),
                cleanup_complete=True,
            )
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
        try:
            self.cgroup.create()
            cgroup_created = True
        except (OSError, RuntimeError, ValueError):
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
                pass_fds=(self.policy.seccomp_fd,),
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
        try:
            self.cgroup.attach(process.pid)
        except (OSError, RuntimeError, ValueError):
            self._terminate(process)
            await process.wait()
            cleanup_complete = await self._cleanup_cgroup()
            return self._receipt(
                TerminationDecision(
                    cause=TerminationCause.Infrastructure,
                    status=CheckStatus.InfrastructureError,
                    returncode=process.returncode,
                ),
                cleanup_complete=cleanup_complete,
            )

        network_attached = False
        if self.network_attachment is not None:
            try:
                await self.network_attachment.attach(process.pid)
                network_attached = True
            except (OSError, RuntimeError, ValueError):
                self._terminate(process)
                await process.wait()
                cleanup_complete = await self._cleanup_cgroup()
                return self._receipt(
                    TerminationDecision(
                        cause=TerminationCause.Infrastructure,
                        status=CheckStatus.InfrastructureError,
                        returncode=process.returncode,
                    ),
                    cleanup_complete=cleanup_complete,
                )

        stdout_task = asyncio.ensure_future(
            _read_limited(process.stdout, self.limits.stdout_bytes, "stdout")
        )
        stderr_task = asyncio.ensure_future(
            _read_limited(process.stderr, self.limits.stderr_bytes, "stderr")
        )
        wait_task = asyncio.ensure_future(process.wait())
        quota_task = asyncio.ensure_future(
            _watch_validation_quota(self.policy.validation_dir, self.limits)
        )
        output_limit = False
        timed_out = False
        infrastructure_failure = False
        cancelled = False
        stdout = b""
        stderr = b""
        stdout_truncated = False
        stderr_truncated = False
        try:
            pending = {wait_task, stdout_task, stderr_task, quota_task}
            deadline = asyncio.get_running_loop().time() + command.timeout_secs
            while pending:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timed_out = True
                    self._terminate(process)
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    timed_out = True
                    self._terminate(process)
                    break
                for task in done:
                    error = task.exception()
                    if task is quota_task and isinstance(error, _QuotaLimit):
                        output_limit = True
                    elif isinstance(error, _OutputLimit):
                        output_limit = True
                        if error.stream == "stdout":
                            stdout, stdout_truncated = error.data, True
                        else:
                            stderr, stderr_truncated = error.data, True
                    elif error is not None:
                        infrastructure_failure = True
                if output_limit or infrastructure_failure:
                    self._terminate(process)
                    break
                if not pending - {quota_task}:
                    # The watcher is scoped to this execution, not a background task.
                    if not quota_task.done():
                        quota_task.cancel()
                    break
        except asyncio.CancelledError:
            cancelled = True
            self._terminate(process)
        finally:
            self._terminate_if_needed(
                process, output_limit or timed_out or infrastructure_failure or cancelled
            )
            if not quota_task.done():
                quota_task.cancel()
            await asyncio.gather(
                wait_task, stdout_task, stderr_task, quota_task, return_exceptions=True
            )
            stdout, stderr, stdout_truncated, stderr_truncated = self._collect_streams(
                stdout_task,
                stderr_task,
                stdout,
                stderr,
                stdout_truncated,
                stderr_truncated,
            )
            cleanup_complete = await self._cleanup_cgroup()
            if network_attached and self.network_attachment is not None:
                try:
                    await self.network_attachment.detach(process.pid)
                except (OSError, RuntimeError, ValueError):
                    cleanup_complete = False

        decision = reduce_termination(
            cancelled=cancelled,
            output_limit=output_limit,
            timed_out=timed_out,
            infrastructure_failure=infrastructure_failure or not cleanup_complete,
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
        try:
            terminate_process_group(process)
        except (OSError, RuntimeError, ValueError):
            pass
        try:
            self.cgroup.kill()
        except (OSError, RuntimeError, ValueError):
            pass

    def _terminate_if_needed(self, process: object, needed: bool) -> None:
        if needed:
            self._terminate(process)

    async def _cleanup_cgroup(self) -> bool:
        complete = True
        try:
            self.cgroup.kill()
        except (OSError, RuntimeError, ValueError):
            complete = False
        try:
            complete = await self.cgroup.wait_empty_async() and complete
        except (OSError, RuntimeError, ValueError):
            complete = False
        try:
            self.cgroup.remove()
        except (OSError, RuntimeError, ValueError):
            complete = False
        return complete

    def _remove_cgroup(self) -> None:
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
