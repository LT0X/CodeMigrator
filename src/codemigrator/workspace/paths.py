"""Directory-fd anchored path and atomic file primitives."""

from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path


class PathSecurityError(ValueError):
    """The requested path violates the workspace path safety boundary."""


class PathNotFound(FileNotFoundError):
    """The safe path is valid but does not exist in the bound root."""


def validate_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise PathSecurityError("path must be a non-empty NUL-free string")
    if len(path.encode("utf-8")) > 4096:
        raise PathSecurityError("path exceeds 4096 UTF-8 bytes")
    if path.startswith(("/", "~")) or "\\" in path:
        raise PathSecurityError("path must be relative POSIX syntax")
    parts = path.split("/")
    if any(part in {"", ".", "..", ".git"} for part in parts):
        raise PathSecurityError("path contains an unsafe segment")
    return path


class SecureRoot:
    """Bind a root directory once and resolve every path below its dirfd."""

    def __init__(self, name: str, path: Path | str) -> None:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("root name must be a safe identifier")
        self.name = name
        self.path = Path(path)
        self._fd = os.open(
            self.path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        self._device = os.fstat(self._fd).st_dev
        self.open_count = 0
        self._closed = False

    def validate(self, path: object) -> str:
        return validate_relative_path(path)

    def absolute_path(self, path: str | None = None) -> Path:
        if path is None:
            return self.path
        return self.path / validate_relative_path(path)

    def exists(self, path: str) -> bool:
        self.validate(path)
        parent_fd, leaf = self._open_parent(path)
        try:
            try:
                fd = self._open_leaf(parent_fd, leaf, os.O_RDONLY)
            except FileNotFoundError:
                return False
            try:
                self._assert_regular_file(fd)
                return True
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def read_bytes(self, path: str, *, max_bytes: int | None = None) -> bytes:
        parent_fd, leaf = self._open_parent(path)
        try:
            try:
                fd = self._open_leaf(parent_fd, leaf, os.O_RDONLY)
            except FileNotFoundError as exc:
                raise PathNotFound(path) from exc
            self.open_count += 1
            try:
                file_stat = os.fstat(fd)
                self._assert_regular_file(fd)
                if max_bytes is not None and file_stat.st_size > max_bytes:
                    raise ValueError("file exceeds read limit")
                chunks: list[bytes] = []
                while data := os.read(fd, 1024 * 1024):
                    chunks.append(data)
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)

    def write_atomic(self, path: str, content: bytes) -> bool:
        """Replace one file atomically; return whether it previously existed."""

        parent_fd, leaf = self._open_parent(path, create_missing=True)
        temp_name = f".codemigrator-tmp-{secrets.token_hex(12)}"
        existed = False
        temp_fd: int | None = None
        try:
            try:
                target_fd = self._open_leaf(parent_fd, leaf, os.O_RDONLY)
            except FileNotFoundError:
                target_fd = None
            if target_fd is not None:
                existed = True
                try:
                    self._assert_regular_file(target_fd)
                finally:
                    os.close(target_fd)
            temp_fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("short atomic write")
                view = view[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            os.replace(temp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
            return existed
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EXDEV, errno.ENOTDIR}:
                raise PathSecurityError("path resolution failed security checks") from exc
            raise
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _open_parent(self, path: str, *, create_missing: bool = False) -> tuple[int, str]:
        value = self.validate(path)
        parts = value.split("/")
        current = os.dup(self._fd)
        try:
            for part in parts[:-1]:
                try:
                    next_fd = self._open_directory(current, part)
                except PathNotFound:
                    if not create_missing:
                        raise PathNotFound(value)
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        if exc.errno == errno.ENOTDIR:
                            raise PathSecurityError("path parent is not a directory") from exc
                        raise
                    next_fd = self._open_directory(current, part)
                os.close(current)
                current = next_fd
                self._assert_device(current)
            return current, parts[-1]
        except BaseException:
            os.close(current)
            raise

    def _open_directory(self, parent_fd: int, name: str) -> int:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathSecurityError("symlink path segment is forbidden") from exc
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                try:
                    if stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode):
                        raise PathSecurityError("symlink path segment is forbidden") from exc
                except FileNotFoundError:
                    pass
                raise PathNotFound(name) from exc
            raise
        try:
            self._assert_device(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_leaf(self, parent_fd: int, leaf: str, flags: int) -> int:
        try:
            fd = os.open(
                leaf,
                flags | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PathSecurityError("symlink path segment is forbidden") from exc
            if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                raise FileNotFoundError(leaf) from exc
            raise
        try:
            self._assert_device(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _assert_device(self, fd: int) -> None:
        if os.fstat(fd).st_dev != self._device:
            raise PathSecurityError("path crosses a mount boundary")

    @staticmethod
    def _assert_regular_file(fd: int) -> None:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PathSecurityError("path does not identify a regular file")

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def __enter__(self) -> SecureRoot:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "PathNotFound",
    "PathSecurityError",
    "SecureRoot",
    "sha256_bytes",
    "validate_relative_path",
]
