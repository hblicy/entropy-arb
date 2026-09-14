import json

import pytest

from entropy_arb.recovery_state import (
    PendingExecutionState,
    PendingExecutionStateError,
    PendingExecutionStore,
    PendingLegState,
    pending_execution_path,
)
from entropy_arb.strategy import MarketIdentity


def pending_state():
    return PendingExecutionState(
        execution_id="exec-1",
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        intent="CLOSE",
        direction="buy_entropy",
        campaign_id="campaign-1",
        qty=1.0,
        buy=PendingLegState(
            venue_key="entropy", is_buy=True, order_ref="buy-1",
            status="timeout", filled_base=0.2, avg_px=100.0,
            applied_fill=0.2, unresolved=True),
        sell=PendingLegState(
            venue_key="hedge", is_buy=False, order_ref="sell-1",
            status="filled", filled_base=1.0, avg_px=100.1,
            applied_fill=1.0, unresolved=False),
        audit_ok=True,
        campaign_applied=False,
    )


def test_pending_store_round_trips_without_secrets(tmp_path):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    expected = pending_state()

    store.save(expected)

    assert store.load() == expected
    payload = path.read_text(encoding="utf-8").lower()
    assert "private_key" not in payload
    assert "api_private_key" not in payload
    assert "token" not in payload


def test_pending_store_rejects_incompatible_state(tmp_path):
    path = tmp_path / "campaign.pending.json"
    path.write_text(json.dumps({
        "schema_version": 999,
        "pending_execution": None,
    }), encoding="utf-8")

    with pytest.raises(PendingExecutionStateError, match="schema_version"):
        PendingExecutionStore(path).load()


def test_pending_store_rejects_applied_fill_above_observed_fill(tmp_path):
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="applied_fill"):
        PendingLegState(
            venue_key=state.buy.venue_key,
            is_buy=True,
            order_ref=state.buy.order_ref,
            status=state.buy.status,
            filled_base=0.1,
            avg_px=state.buy.avg_px,
            applied_fill=0.2,
            unresolved=True,
        )


def test_pending_store_can_atomically_clear(tmp_path):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    store.save(pending_state())

    store.save(None)

    assert store.load() is None
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_path_is_derived_without_colliding_with_campaign_file(tmp_path):
    campaign = tmp_path / "campaign-state.json"

    pending = pending_execution_path(campaign)

    assert pending == tmp_path / "campaign-state.pending.json"
    assert pending != campaign
