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


def test_different_account_or_market_has_different_lock(tmp_path):
    base = lock_identity()
    locks = [
        LiveProcessLock(base, directory=tmp_path),
        LiveProcessLock(
            replace(base, entropy_account="account-2"),
            directory=tmp_path,
        ),
        LiveProcessLock(
            replace(
                base,
                market=replace(base.market, entropy_symbol="SNDK")),
            directory=tmp_path,
        ),
    ]
    for lock in locks:
        lock.acquire()
    for lock in reversed(locks):
        lock.release()


def test_lock_file_contains_only_digest_and_pid(tmp_path):
    lock = LiveProcessLock(lock_identity(), directory=tmp_path)
    lock.acquire()
    lock.release()

    payload = lock.path.read_text(encoding="utf-8").lower()
    assert "account-1" not in payload
    assert "anth" not in payload
    assert "digest" in payload
    assert "pid" in payload
