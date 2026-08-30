"""Process ownership and temporary validation directory lifecycle helpers."""

from __future__ import annotations

import ctypes
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from types import TracebackType

from .limits import ResourceLimits


class TemporaryValidationDirectory:
    """Create and remove a private validation directory around one check."""

    def __init__(self, *, parent: Path | str | None = None) -> None:
        self._parent = Path(parent) if parent is not None else None
        self.path = Path()

    def __enter__(self) -> TemporaryValidationDirectory:
        self.path = Path(tempfile.mkdtemp(prefix="codemigrator-validation-", dir=self._parent))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.path != Path() and self.path.exists():
            shutil.rmtree(self.path)


def pdeathsig_preexec() -> None:
    """Install Linux parent-death cleanup for a child before execve."""

    if os.name != "posix":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    pr_set_pdeathsig = 1
    if libc.prctl(pr_set_pdeathsig, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "prctl(PR_SET_PDEATHSIG) failed")


def terminate_process_group(process: object) -> None:
    """Terminate a process group without relying on shell process semantics."""

    pid = getattr(process, "pid", None)
    if not isinstance(pid, int):
        raise TypeError("process must expose an integer pid")
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class CgroupProcessDomain:
    """Best-effort handle for an already delegated cgroup-v2 subtree.

    Creation is explicit and never falls back to host-wide limits.  A caller
    without a delegated root must fail its preflight before creating a process.
    """

    def __init__(self, root: Path | str, name: str, limits: ResourceLimits) -> None:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("cgroup name is not a single safe path component")
        self.path = Path(root) / name
        self.limits = limits

    def create(self) -> None:
        self.path.mkdir(mode=0o700)
        try:
            self._write("memory.max", str(self.limits.memory_bytes))
            self._write("cpu.max", f"{self.limits.cpu_cores * 100_000} 100000")
        except OSError:
            self.path.rmdir()
            raise

    def attach(self, pid: int) -> None:
        if pid < 1:
            raise ValueError("pid must be positive")
        self._write("cgroup.procs", str(pid))

    def kill(self) -> None:
        kill_file = self.path / "cgroup.kill"
        if kill_file.exists():
            kill_file.write_text("1", encoding="ascii")

    def wait_empty(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        procs = self.path / "cgroup.procs"
        while time.monotonic() < deadline:
            if not procs.exists() or not procs.read_text(encoding="ascii").strip():
                return True
            time.sleep(0.01)
        return not procs.exists() or not procs.read_text(encoding="ascii").strip()

    def remove(self) -> None:
        self.path.rmdir()

    def _write(self, name: str, value: str) -> None:
        (self.path / name).write_text(value, encoding="ascii")
