"""Cross-process guard for one live engine per account and market."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Optional

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
        canonical = json.dumps(
            asdict(identity), sort_keys=True, separators=(",", ":"),
            ensure_ascii=True)
        self.digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        lock_dir = (Path(directory) if directory is not None else
                    Path(tempfile.gettempdir()) / "entropy-arb-live-locks")
        self.path = lock_dir / f"{self.digest}.lock"
        self._handle: Optional[BinaryIO] = None

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
        if self._handle is not None:
            raise LiveProcessLockError("live process lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    handle.write(b"\n")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LiveProcessLockError(
                "a live engine is already running for these accounts and "
                "market") from exc
        try:
            payload = json.dumps(
                {"digest": self.digest, "pid": os.getpid()},
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
        self._handle = handle

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
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            self._unlock(handle)
        finally:
            handle.close()
