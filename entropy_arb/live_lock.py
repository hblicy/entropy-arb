"""Cross-process guard preventing concurrent use of any live account."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .strategy import MarketIdentity


class LiveProcessLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveLockIdentity:
    market: MarketIdentity
    entropy_account: str
    hedge_account: str

    def __post_init__(self) -> None:
        if not isinstance(self.market, MarketIdentity):
            raise LiveProcessLockError("market identity is invalid")
        for name, value in (
                ("entropy_account", self.entropy_account),
                ("hedge_account", self.hedge_account)):
            if not isinstance(value, str) or not value:
                raise LiveProcessLockError(f"{name} must not be empty")


class LiveProcessLock:
    def __init__(self, identity: LiveLockIdentity, *, directory=None) -> None:
        if not isinstance(identity, LiveLockIdentity):
            raise LiveProcessLockError("identity must be LiveLockIdentity")
        accounts = sorted({identity.entropy_account, identity.hedge_account})
        digests = tuple(
            hashlib.sha256(account.encode("utf-8")).hexdigest()
            for account in accounts)
        if (directory is None and os.name != "nt"
                and not sys.platform.startswith("linux")):
            raise LiveProcessLockError(
                "default live account locking supports only Linux and "
                "Windows")
        lock_dir = Path(directory) if directory is not None else None
        self._targets = () if lock_dir is None else tuple(
            (digest, lock_dir / f"{digest}.lock") for digest in digests)
        self.paths = tuple(path for _, path in self._targets)
        self.path = self.paths[0] if self.paths else None
        self._mutex_names = tuple(
            f"Global\\entropy-arb-live-{digest}" for digest in digests
        ) if directory is None and os.name == "nt" else ()
        self._socket_names = tuple(
            b"\0entropy-arb-live-" + digest.encode("ascii")
            for digest in digests
        ) if (directory is None and sys.platform.startswith("linux")) else ()
        self._handles: list[BinaryIO] = []
        self._mutex_handles: list[int] = []
        self._socket_handles: list[socket.socket] = []

    @classmethod
    def from_market(cls, market: MarketIdentity, entropy_account: str,
                    hedge_account: str, *, directory=None):
        return cls(
            LiveLockIdentity(
                market=market,
                entropy_account=entropy_account,
                hedge_account=hedge_account,
            ),
            directory=directory,
        )

    def acquire(self) -> None:
        if self._handles or self._mutex_handles or self._socket_handles:
            raise LiveProcessLockError("live process lock is already acquired")
        if self._mutex_names:
            self._acquire_windows_mutexes()
            return
        if self._socket_names:
            self._acquire_linux_sockets()
            return
        acquired = []
        try:
            for digest, path in self._targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
                try:
                    self._lock(handle, path)
                except OSError as exc:
                    handle.close()
                    raise LiveProcessLockError(
                        "a live engine is already running for one of these "
                        "accounts") from exc
                try:
                    payload = json.dumps(
                        {"digest": digest, "pid": os.getpid()},
                        sort_keys=True, separators=(",", ":"))
                    handle.seek(0)
                    handle.truncate()
                    handle.write(payload.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                except BaseException:
                    self._unlock(handle)
                    handle.close()
                    raise
                acquired.append(handle)
        except BaseException:
            for handle in reversed(acquired):
                try:
                    self._unlock(handle)
                finally:
                    handle.close()
            raise
        self._handles = acquired

    def _acquire_linux_sockets(self) -> None:
        acquired = []
        try:
            for name in self._socket_names:
                handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    handle.bind(name)
                except OSError as exc:
                    handle.close()
                    if exc.errno == errno.EADDRINUSE:
                        raise LiveProcessLockError(
                            "a live engine is already running for one of "
                            "these accounts") from exc
                    raise LiveProcessLockError(
                        "cannot create machine-wide live account lock") \
                        from exc
                acquired.append(handle)
        except BaseException:
            for handle in reversed(acquired):
                handle.close()
            raise
        self._socket_handles = acquired

    def _acquire_windows_mutexes(self) -> None:
        acquired = []
        try:
            for name in self._mutex_names:
                handle = self._create_windows_mutex(name)
                acquired.append(handle)
        except BaseException as exc:
            cleanup_error = None
            for handle in reversed(acquired):
                try:
                    self._release_windows_mutex(handle)
                except BaseException as release_exc:
                    if cleanup_error is None:
                        cleanup_error = release_exc
            if cleanup_error is not None:
                raise cleanup_error from exc
            raise
        self._mutex_handles = acquired

    @staticmethod
    def _windows_kernel32():
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return ctypes, kernel32

    @classmethod
    def _create_windows_mutex(cls, name: str) -> int:
        ctypes, kernel32 = cls._windows_kernel32()
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, True, name)
        error = ctypes.get_last_error()
        if not handle:
            raise LiveProcessLockError(
                "cannot create machine-wide live account lock") from OSError(
                    error, os.strerror(error))
        handle = int(handle)
        if error == 183:  # ERROR_ALREADY_EXISTS
            try:
                cls._close_windows_handle(handle)
            except OSError as exc:
                raise LiveProcessLockError(
                    "cannot close contended live account lock") from exc
            raise LiveProcessLockError(
                "a live engine is already running for one of these accounts")
        return handle

    @classmethod
    def _close_windows_handle(cls, handle: int) -> None:
        ctypes, kernel32 = cls._windows_kernel32()
        if not kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))

    @classmethod
    def _release_windows_mutex(cls, handle: int) -> None:
        ctypes, kernel32 = cls._windows_kernel32()
        first_error = None
        if not kernel32.ReleaseMutex(handle):
            error = ctypes.get_last_error()
            first_error = OSError(error, os.strerror(error))
        try:
            cls._close_windows_handle(handle)
        except BaseException as close_error:
            if first_error is None:
                first_error = close_error
        if first_error is not None:
            raise first_error

    @staticmethod
    def _lock(handle: BinaryIO, path: Path) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"\n")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def release(self) -> None:
        mutex_handles = self._mutex_handles
        if mutex_handles:
            self._mutex_handles = []
            first_error = None
            for handle in reversed(mutex_handles):
                try:
                    self._release_windows_mutex(handle)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
            return
        socket_handles = self._socket_handles
        if socket_handles:
            self._socket_handles = []
            for handle in reversed(socket_handles):
                handle.close()
            return
        handles = self._handles
        if not handles:
            return
        self._handles = []
        first_error = None
        for handle in reversed(handles):
            try:
                self._unlock(handle)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                handle.close()
        if first_error is not None:
            raise first_error
