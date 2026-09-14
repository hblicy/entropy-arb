import json
import math

import pytest

from entropy_arb.campaign import (
    CampaignInvariantError,
    CampaignStateError,
    CampaignStore,
    PositionCampaign,
)
from entropy_arb.strategy import MarketIdentity, ModelSnapshot


def snapshot():
    return ModelSnapshot(
        version=7,
        minute=100,
        samples=180,
        status="READY",
        median_bps=10.0,
        lower_bps=-20.0,
        q25_bps=0.0,
        q75_bps=20.0,
        upper_bps=40.0,
    )


def campaign(**overrides):
    values = {
        "campaign_id": "campaign-1",
        "mode": "shadow",
        "identity": MarketIdentity(
            "ANTH", "io", "ANTHROPIC", "lighter-rh"),
        "direction": "buy_entropy",
        "opened_at": 1000.0,
        "qty": 1.0,
        "entropy_avg_px": 100.0,
        "hedge_avg_px": 101.0,
        "frozen_model": snapshot(),
        "entry_boundary_bps": -20.0,
        "exit_target_bps": 5.0,
        "fees_usd": 0.09,
        "realized_pnl_usd": -0.09,
    }
    values.update(overrides)
    return PositionCampaign(**values)


def test_campaign_transitions_by_wall_clock():
    value = campaign()

    assert value.status_at(4599, soft_sec=3600, hard_sec=21600) == "OPEN"
    assert value.status_at(4600, soft_sec=3600, hard_sec=21600) == "SOFT_EXIT"
    assert value.status_at(22600, soft_sec=3600, hard_sec=21600) == "HARD_EXIT"


def test_campaign_rejects_reverse_add():
    with pytest.raises(CampaignInvariantError, match="direction"):
        campaign().apply_matched_fill(
            intent="ADD",
            direction="sell_entropy",
            qty=0.1,
            entropy_px=99.0,
            hedge_px=100.0,
            fees_usd=0.01,
        )


def test_add_updates_weighted_prices_and_fees():
    updated = campaign().apply_matched_fill(
        intent="ADD",
        direction="buy_entropy",
        qty=1.0,
        entropy_px=102.0,
        hedge_px=103.0,
        fees_usd=0.1,
    )

    assert updated.qty == 2.0
    assert updated.entropy_avg_px == 101.0
    assert updated.hedge_avg_px == 102.0
    assert updated.fees_usd == pytest.approx(0.19)
    assert updated.realized_pnl_usd == pytest.approx(-0.19)


def test_partial_close_reduces_quantity_without_crossing_zero():
    updated = campaign().apply_matched_fill(
        intent="CLOSE",
        direction="buy_entropy",
        qty=0.4,
        entropy_px=102.0,
        hedge_px=102.0,
        fees_usd=0.05,
    )

    assert updated.qty == pytest.approx(0.6)
    assert updated.realized_pnl_usd == pytest.approx(0.26)

    with pytest.raises(CampaignInvariantError, match="exceeds"):
        updated.apply_matched_fill(
            intent="CLOSE",
            direction="buy_entropy",
            qty=0.7,
            entropy_px=102.0,
            hedge_px=102.0,
            fees_usd=0.05,
        )


def test_full_close_returns_none():
    assert campaign().apply_matched_fill(
        intent="FORCED_CLOSE",
        direction="buy_entropy",
        qty=1.0,
        entropy_px=98.0,
        hedge_px=102.0,
        fees_usd=0.05,
    ) is None


def test_atomic_store_round_trip_and_shadow_path(tmp_path):
    store = CampaignStore(
        str(tmp_path / "campaign-state.json"), shadow=True)
    expected = campaign()

    store.save(expected)

    assert store.path.name == "campaign-state.shadow.json"
    assert store.load() == expected
    assert not list(tmp_path.glob("*.tmp"))


def test_store_can_persist_flat_state(tmp_path):
    store = CampaignStore(str(tmp_path / "state.json"), shadow=False)

    store.save(None)

    assert store.load() is None
    assert store.path.exists()


def test_store_rejects_corrupt_or_unknown_schema(tmp_path):
    path = tmp_path / "campaign-state.json"
    path.write_text('{"schema_version":999,"campaign":null}',
                    encoding="utf-8")

    with pytest.raises(CampaignStateError, match="schema_version"):
        CampaignStore(str(path), shadow=False).load()

    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(CampaignStateError, match="valid JSON"):
        CampaignStore(str(path), shadow=False).load()


def test_campaign_rejects_nonfinite_persisted_values(tmp_path):
    path = tmp_path / "state.json"
    payload = {
        "schema_version": 1,
        "campaign": {
            "campaign_id": "bad",
            "mode": "live",
            "identity": {
                "entropy_symbol": "ANTH",
                "entropy_dex": "io",
                "hedge_symbol": "ANTHROPIC",
                "hedge_venue": "lighter-rh",
            },
            "direction": "buy_entropy",
            "opened_at": 1,
            "qty": math.nan,
            "entropy_avg_px": 100,
            "hedge_avg_px": 100,
            "frozen_model": snapshot().__dict__,
            "entry_boundary_bps": -20,
            "exit_target_bps": 5,
            "fees_usd": 0,
            "realized_pnl_usd": 0,
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CampaignStateError, match="qty"):
        CampaignStore(str(path), shadow=False).load()
