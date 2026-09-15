from dataclasses import replace

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
