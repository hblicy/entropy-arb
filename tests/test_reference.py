import math

import pytest

from entropy_arb.reference import (
    InvalidReference,
    MarketReference,
    ReferenceAlertState,
    ReferenceState,
    ReferenceUpdate,
    calculate_reference_metrics,
)


def test_reference_state_applies_valid_snapshot_atomically():
    state = ReferenceState()

    changed = state.apply(
        ReferenceUpdate(
            oracle_px=100.0,
            mark_px=100.1,
            funding_current_bps_per_hour=-0.2,
            exchange_ts_ms=2000,
        ),
        source="websocket",
        received_mono=10.0,
    )

    assert changed is True
    assert state.snapshot.oracle_px == 100.0
    assert state.snapshot.mark_px == 100.1
    assert state.snapshot.funding_current_bps_per_hour == -0.2
    assert state.snapshot.received_mono == 10.0
    assert state.snapshot.source == "websocket"
    assert state.last_ws_received_mono == 10.0


@pytest.mark.parametrize("bad_price", [0.0, -1.0, math.nan, math.inf])
def test_reference_state_rejects_invalid_prices_without_overwrite(bad_price):
    state = ReferenceState()
    state.apply(
        ReferenceUpdate(oracle_px=100.0, exchange_ts_ms=2000),
        source="websocket",
        received_mono=10.0,
    )

    with pytest.raises(InvalidReference):
        state.apply(
            ReferenceUpdate(oracle_px=bad_price, exchange_ts_ms=3000),
            source="websocket",
            received_mono=11.0,
        )

    assert state.snapshot.oracle_px == 100.0
    assert state.snapshot.exchange_ts_ms == 2000
    assert state.last_ws_received_mono == 10.0


@pytest.mark.parametrize("bad_funding", [math.nan, math.inf, -math.inf])
def test_reference_state_rejects_nonfinite_funding(bad_funding):
    state = ReferenceState()

    with pytest.raises(InvalidReference):
        state.apply(
            ReferenceUpdate(funding_current_bps_per_hour=bad_funding),
            source="rest",
            received_mono=10.0,
        )


def test_reference_state_rejects_out_of_order_exchange_timestamp():
    state = ReferenceState()
    state.apply(
        ReferenceUpdate(index_px=100.0, exchange_ts_ms=2000),
        source="websocket",
        received_mono=10.0,
    )

    changed = state.apply(
        ReferenceUpdate(index_px=99.0, exchange_ts_ms=1999),
        source="rest",
        received_mono=12.0,
    )

    assert changed is False
    assert state.snapshot.index_px == 100.0
    assert state.snapshot.received_mono == 10.0


def test_rest_update_does_not_refresh_websocket_freshness():
    state = ReferenceState()
    state.apply(
        ReferenceUpdate(index_px=100.0),
        source="websocket",
        received_mono=10.0,
    )
    state.apply(
        ReferenceUpdate(index_px=100.1),
        source="rest",
        received_mono=20.0,
    )

    assert state.age_ms(now_mono=21.0) == pytest.approx(1000.0)
    assert state.ws_is_fresh(15.0, now_mono=21.0) is True
    assert state.ws_is_fresh(10.0, now_mono=21.0) is False
    assert state.last_ws_received_mono == 10.0


def test_inflight_rest_update_cannot_overwrite_new_websocket_snapshot():
    state = ReferenceState()
    generation = state.websocket_generation
    state.apply(
        ReferenceUpdate(index_px=101.0),
        source="websocket",
        received_mono=20.0,
    )

    changed = state.apply_rest_if_ws_unchanged(
        ReferenceUpdate(index_px=99.0),
        expected_websocket_generation=generation,
        received_mono=21.0,
    )

    assert changed is False
    assert state.snapshot.index_px == 101.0
    assert state.snapshot.source == "websocket"
    assert state.snapshot.received_mono == 20.0


def test_reference_state_rejects_unknown_source_and_bad_timestamps():
    state = ReferenceState()

    with pytest.raises(InvalidReference, match="source"):
        state.apply(ReferenceUpdate(index_px=100.0), source="cache")
    with pytest.raises(InvalidReference, match="exchange_ts_ms"):
        state.apply(
            ReferenceUpdate(index_px=100.0, exchange_ts_ms=-1),
            source="rest",
        )
    with pytest.raises(InvalidReference, match="funding_last_ts_ms"):
        state.apply(
            ReferenceUpdate(funding_last_ts_ms=-1),
            source="rest",
        )


def test_empty_reference_has_no_age_or_websocket_freshness():
    state = ReferenceState()

    assert state.age_ms(now_mono=10.0) is None
    assert state.ws_is_fresh(60.0, now_mono=10.0) is False


def test_sell_entropy_reference_metrics_preserve_signs():
    metrics = calculate_reference_metrics(
        direction="sell_entropy",
        entropy_bid=101.0,
        entropy_ask=101.2,
        hedge_bid=99.8,
        hedge_ask=100.0,
        entropy=MarketReference(
            oracle_px=100.5, funding_current_bps_per_hour=0.3),
        hedge=MarketReference(
            index_px=100.0, funding_current_bps_per_hour=0.1),
    )

    assert metrics.reference_basis_bps == pytest.approx(50.0)
    assert metrics.signed_executable_premium_bps == pytest.approx(100.0)
    assert metrics.signed_residual_bps == pytest.approx(50.0)
    assert metrics.residual_edge_bps == pytest.approx(50.0)
    assert metrics.net_funding_bps_per_hour == pytest.approx(0.2)


def test_buy_entropy_reference_metrics_reverse_residual_and_funding():
    metrics = calculate_reference_metrics(
        direction="buy_entropy",
        entropy_bid=98.8,
        entropy_ask=99.0,
        hedge_bid=100.0,
        hedge_ask=100.2,
        entropy=MarketReference(
            oracle_px=99.5, funding_current_bps_per_hour=0.3),
        hedge=MarketReference(
            index_px=100.0, funding_current_bps_per_hour=0.1),
    )

    assert metrics.reference_basis_bps == pytest.approx(-50.0)
    assert metrics.signed_executable_premium_bps == pytest.approx(-100.0)
    assert metrics.signed_residual_bps == pytest.approx(-50.0)
    assert metrics.residual_edge_bps == pytest.approx(50.0)
    assert metrics.net_funding_bps_per_hour == pytest.approx(-0.2)


def test_reference_metrics_leave_missing_derivatives_empty():
    metrics = calculate_reference_metrics(
        direction="sell_entropy",
        entropy_bid=101.0,
        entropy_ask=101.2,
        hedge_bid=99.8,
        hedge_ask=100.0,
        entropy=MarketReference(oracle_px=100.5),
        hedge=MarketReference(),
    )

    assert metrics.reference_basis_bps is None
    assert metrics.signed_executable_premium_bps == pytest.approx(100.0)
    assert metrics.signed_residual_bps is None
    assert metrics.residual_edge_bps is None
    assert metrics.net_funding_bps_per_hour is None


def test_reference_metrics_reject_unknown_direction():
    with pytest.raises(ValueError, match="unknown direction"):
        calculate_reference_metrics(
            direction="sideways",
            entropy_bid=101.0,
            entropy_ask=101.2,
            hedge_bid=99.8,
            hedge_ask=100.0,
            entropy=MarketReference(),
            hedge=MarketReference(),
        )


def test_residual_alert_requires_persistence_and_deduplicates():
    alerts = ReferenceAlertState(alert_bps=20.0, persist_sec=30.0)

    assert alerts.observe(
        now_mono=0.0, sell_residual_bps=21.0,
        buy_residual_bps=None, stale=False) == []
    assert alerts.observe(
        now_mono=29.9, sell_residual_bps=21.0,
        buy_residual_bps=None, stale=False) == []
    started = alerts.observe(
        now_mono=30.0, sell_residual_bps=22.0,
        buy_residual_bps=None, stale=False)
    assert [(e.kind, e.active, e.direction) for e in started] == [
        ("residual", True, "sell_entropy")]
    assert alerts.observe(
        now_mono=40.0, sell_residual_bps=25.0,
        buy_residual_bps=None, stale=False) == []

    recovered = alerts.observe(
        now_mono=41.0, sell_residual_bps=19.9,
        buy_residual_bps=None, stale=False)
    assert [(e.kind, e.active, e.direction) for e in recovered] == [
        ("residual", False, "sell_entropy")]
    assert alerts.observe(
        now_mono=42.0, sell_residual_bps=19.0,
        buy_residual_bps=None, stale=False) == []


def test_residual_alert_candidate_resets_before_persistence():
    alerts = ReferenceAlertState(alert_bps=20.0, persist_sec=30.0)

    alerts.observe(now_mono=0.0, sell_residual_bps=-21.0,
                   buy_residual_bps=None, stale=False)
    alerts.observe(now_mono=20.0, sell_residual_bps=-19.0,
                   buy_residual_bps=None, stale=False)
    assert alerts.observe(
        now_mono=40.0, sell_residual_bps=-21.0,
        buy_residual_bps=None, stale=False) == []


def test_stale_alert_and_recovery_are_emitted_once():
    alerts = ReferenceAlertState(alert_bps=20.0, persist_sec=30.0)

    started = alerts.observe(
        now_mono=1.0, sell_residual_bps=None,
        buy_residual_bps=None, stale=True)
    assert [(e.kind, e.active) for e in started] == [("stale", True)]
    assert alerts.observe(
        now_mono=2.0, sell_residual_bps=None,
        buy_residual_bps=None, stale=True) == []
    recovered = alerts.observe(
        now_mono=3.0, sell_residual_bps=None,
        buy_residual_bps=None, stale=False)
    assert [(e.kind, e.active) for e in recovered] == [("stale", False)]
    assert alerts.observe(
        now_mono=4.0, sell_residual_bps=None,
        buy_residual_bps=None, stale=False) == []
