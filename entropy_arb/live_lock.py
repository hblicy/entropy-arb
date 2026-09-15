"""Cross-process guard preventing concurrent use of any live account."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .strategy import MarketIdentity


class LiveProcessLockError(RuntimeError):
    pass


def _default_lock_directory() -> Path:
    return Path("/tmp") / "entropy-arb-live-locks"


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
        use_windows_semaphores = directory is None and os.name == "nt"
        lock_dir = None if use_windows_semaphores else (
            Path(directory)
            if directory is not None
            else _default_lock_directory()
        )
        self._targets = () if lock_dir is None else tuple(
            (digest, lock_dir / f"{digest}.lock") for digest in digests)
        self.paths = tuple(path for _, path in self._targets)
        self.path = self.paths[0] if self.paths else None
        self._semaphore_names = tuple(
            f"Global\\entropy-arb-live-{digest}" for digest in digests
        ) if use_windows_semaphores else ()
        self._handles: list[BinaryIO] = []
        self._semaphore_handles: list[int] = []

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
        if self._handles or self._semaphore_handles:
            raise LiveProcessLockError("live process lock is already acquired")
        if self._semaphore_names:
            self._acquire_windows_semaphores()
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

    def _acquire_windows_semaphores(self) -> None:
        acquired = []
        try:
            for name in self._semaphore_names:
                handle = self._create_windows_semaphore(name)
                try:
                    locked = self._wait_windows_semaphore(handle)
                except BaseException:
                    self._close_windows_handle(handle)
                    raise
                if not locked:
                    self._close_windows_handle(handle)
                    raise LiveProcessLockError(
                        "a live engine is already running for one of these "
                        "accounts")
                acquired.append(handle)
        except BaseException as exc:
            cleanup_error = None
            for handle in reversed(acquired):
                try:
                    self._release_windows_semaphore(handle)
                except BaseException as release_exc:
                    if cleanup_error is None:
                        cleanup_error = release_exc
            if cleanup_error is not None:
                raise cleanup_error from exc
            raise
        self._semaphore_handles = acquired

    @staticmethod
    def _windows_kernel32():
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateSemaphoreW.argtypes = (
            wintypes.LPVOID, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR)
        kernel32.CreateSemaphoreW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (
            wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseSemaphore.argtypes = (
            wintypes.HANDLE, wintypes.LONG, wintypes.LPVOID)
        kernel32.ReleaseSemaphore.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return ctypes, kernel32

    @classmethod
    def _create_windows_semaphore(cls, name: str) -> int:
        ctypes, kernel32 = cls._windows_kernel32()
        handle = kernel32.CreateSemaphoreW(None, 1, 1, name)
        if not handle:
            error = ctypes.get_last_error()
            raise LiveProcessLockError(
                "cannot create machine-wide live account lock") from OSError(
                    error, os.strerror(error))
        return int(handle)

    @classmethod
    def _wait_windows_semaphore(cls, handle: int) -> bool:
        ctypes, kernel32 = cls._windows_kernel32()
        result = kernel32.WaitForSingleObject(handle, 0)
        if result == 0:
            return True
        if result == 0x102:
            return False
        error = ctypes.get_last_error()
        raise LiveProcessLockError(
            "cannot acquire machine-wide live account lock") from OSError(
                error, os.strerror(error))

    @classmethod
    def _close_windows_handle(cls, handle: int) -> None:
        ctypes, kernel32 = cls._windows_kernel32()
        if not kernel32.CloseHandle(handle):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error))

    @classmethod
    def _release_windows_semaphore(cls, handle: int) -> None:
        ctypes, kernel32 = cls._windows_kernel32()
        first_error = None
        if not kernel32.ReleaseSemaphore(handle, 1, None):
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
        semaphore_handles = self._semaphore_handles
        if semaphore_handles:
            self._semaphore_handles = []
            first_error = None
            for handle in reversed(semaphore_handles):
                try:
                    self._release_windows_semaphore(handle)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
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
