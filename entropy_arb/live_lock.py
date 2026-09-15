"""Cross-process guard preventing concurrent use of any live account."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
        lock_dir = (Path(directory) if directory is not None else
                    Path(tempfile.gettempdir()) / "entropy-arb-live-locks")
        accounts = sorted({identity.entropy_account, identity.hedge_account})
        digests = tuple(
            hashlib.sha256(account.encode("utf-8")).hexdigest()
            for account in accounts)
        self._targets = tuple(
            (digest, lock_dir / f"{digest}.lock") for digest in digests)
        self.paths = tuple(path for _, path in self._targets)
        self.path = self.paths[0]
        self._handles: list[BinaryIO] = []

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
        if self._handles:
            raise LiveProcessLockError("live process lock is already acquired")
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
