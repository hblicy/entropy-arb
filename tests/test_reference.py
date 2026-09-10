import math

import pytest

from entropy_arb.reference import (
    InvalidReference,
    ReferenceState,
    ReferenceUpdate,
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
