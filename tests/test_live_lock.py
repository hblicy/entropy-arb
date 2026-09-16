from dataclasses import replace
import hashlib
import os
import sys
import tempfile

import pytest

from entropy_arb.live_lock import (
    LiveLockIdentity,
    LiveProcessLock,
    LiveProcessLockError,
)
from entropy_arb.strategy import MarketIdentity


def lock_identity(*, account="account-1", symbol="ANTH"):
    return LiveLockIdentity(
        market=MarketIdentity(
            entropy_symbol=symbol,
            entropy_dex="io",
            hedge_symbol="ANTHROPIC",
            hedge_venue="lighter-rh",
        ),
        entropy_account=account,
        hedge_account="466324:7",
    )


def test_default_lock_namespace_does_not_follow_process_temp_root(tmp_path):
    original = tempfile.tempdir
    try:
        tempfile.tempdir = str(tmp_path / "user-a")
        first = LiveProcessLock(lock_identity())
        tempfile.tempdir = str(tmp_path / "user-b")
        second = LiveProcessLock(lock_identity())
    finally:
        tempfile.tempdir = original

    assert first.paths == second.paths == ()
    if os.name == "nt":
        assert first._mutex_names == second._mutex_names
        assert all(
            name.startswith("Global\\entropy-arb-live-")
            for name in first._mutex_names)
    elif sys.platform.startswith("linux"):
        assert first._socket_names == second._socket_names
        assert all(
            name.startswith(b"\0entropy-arb-live-")
            for name in first._socket_names)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel object behavior")
def test_default_lock_rejects_precreated_count_two_semaphores(tmp_path):
    import ctypes
    from ctypes import wintypes

    identity = replace(
        lock_identity(account=f"entropy-{tmp_path}"),
        hedge_account=f"hedge-{tmp_path}",
    )
    names = [
        "Global\\entropy-arb-live-" + hashlib.sha256(
            account.encode("utf-8")).hexdigest()
        for account in sorted(
            {identity.entropy_account, identity.hedge_account})
    ]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateSemaphoreW.argtypes = (
        wintypes.LPVOID, wintypes.LONG, wintypes.LONG, wintypes.LPCWSTR)
    kernel32.CreateSemaphoreW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    seeded = [
        kernel32.CreateSemaphoreW(None, 2, 2, name) for name in names]
    assert all(seeded)
    lock = LiveProcessLock(identity)
    try:
        with pytest.raises(LiveProcessLockError, match="lock|running"):
            lock.acquire()
    finally:
        lock.release()
        for handle in seeded:
            assert kernel32.CloseHandle(handle)


def test_second_default_lock_for_same_identity_is_rejected(tmp_path):
    identity = replace(
        lock_identity(account=f"entropy-{tmp_path}"),
        hedge_account=f"hedge-{tmp_path}",
    )
    first = LiveProcessLock(identity)
    second = LiveProcessLock(identity)
    first.acquire()
    try:
        with pytest.raises(LiveProcessLockError, match="already running"):
            second.acquire()
    finally:
        second.release()
        first.release()


def test_second_live_lock_for_same_identity_is_rejected(tmp_path):
    first = LiveProcessLock(lock_identity(), directory=tmp_path)
    second = LiveProcessLock(lock_identity(), directory=tmp_path)
    first.acquire()
    try:
        with pytest.raises(LiveProcessLockError, match="already running"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_disjoint_accounts_have_different_locks(tmp_path):
    base = lock_identity()
    locks = [
        LiveProcessLock(base, directory=tmp_path),
        LiveProcessLock(
            replace(
                base,
                entropy_account="account-2",
                hedge_account="hedge-account-2"),
            directory=tmp_path,
        ),
    ]
    for lock in locks:
        lock.acquire()
    for lock in reversed(locks):
        lock.release()


def test_same_accounts_on_different_markets_conflict(tmp_path):
    first = LiveProcessLock(lock_identity(symbol="ANTH"), directory=tmp_path)
    second = LiveProcessLock(lock_identity(symbol="SNDK"), directory=tmp_path)
    first.acquire()
    try:
        with pytest.raises(LiveProcessLockError, match="already running"):
            second.acquire()
    finally:
        second.release()
        first.release()


def test_overlapping_account_sets_conflict(tmp_path):
    first = LiveProcessLock(
        replace(
            lock_identity(), entropy_account="shared", hedge_account="first"),
        directory=tmp_path,
    )
    second = LiveProcessLock(
        replace(
            lock_identity(), entropy_account="shared", hedge_account="second"),
        directory=tmp_path,
    )
    first.acquire()
    try:
        with pytest.raises(LiveProcessLockError, match="already running"):
            second.acquire()
    finally:
        second.release()
        first.release()


def test_partial_account_lock_failure_releases_earlier_lock(tmp_path):
    blocker = LiveProcessLock(
        replace(
            lock_identity(), entropy_account="z-blocked",
            hedge_account="zz-blocked"),
        directory=tmp_path,
    )
    contender = LiveProcessLock(
        replace(
            lock_identity(), entropy_account="a-rollback",
            hedge_account="z-blocked"),
        directory=tmp_path,
    )
    probe = LiveProcessLock(
        replace(
            lock_identity(), entropy_account="a-rollback",
            hedge_account="b-probe"),
        directory=tmp_path,
    )
    blocker.acquire()
    try:
        with pytest.raises(LiveProcessLockError, match="already running"):
            contender.acquire()
        probe.acquire()
        probe.release()
    finally:
        contender.release()
        blocker.release()


def test_lock_file_contains_only_digest_and_pid(tmp_path):
    lock = LiveProcessLock(lock_identity(), directory=tmp_path)
    lock.acquire()
    lock.release()

    assert len(lock.paths) == 2
    payload = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in lock.paths)
    assert "account-1" not in payload
    assert "anth" not in payload
    assert payload.count("digest") == 2
    assert "pid" in payload
