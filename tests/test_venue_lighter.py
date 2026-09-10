import asyncio

import aiohttp
import pytest

from entropy_arb.config import LighterProfile, VenueConf
from entropy_arb.reference import ReferenceState
from entropy_arb.venue_lighter import (
    LighterVenue,
    parse_lighter_rest_market,
)


def test_lighter_venue_owns_reference_state():
    profile = LighterProfile("robinhood", "https://api", "wss://ws", 1)
    conf = VenueConf(
        key="hedge", kind="lighter", label="RH", symbol="ANTHROPIC",
        fee_bps=0.0, cap_usd=1000.0, orders_per_min=30,
        lighter_profile=profile)

    venue = LighterVenue(conf, object(), 5.0)

    assert isinstance(venue.reference, ReferenceState)


def test_lighter_rest_funding_converts_eight_hour_rate_to_hourly_bps():
    update = parse_lighter_rest_market(
        {"index_price": "100.0", "mark_price": "100.1"},
        {"exchange": "lighter", "rate": 0.000032},
    )

    assert update.index_px == 100.0
    assert update.mark_px == 100.1
    assert update.funding_current_bps_per_hour == pytest.approx(0.04)
    assert update.funding_last_ts_ms is None


def test_lighter_refresh_reference_rest_selects_market_and_funding():
    async def go():
        profile = LighterProfile("robinhood", "https://api", "wss://ws", 1)
        conf = VenueConf(
            key="hedge", kind="lighter", label="RH", symbol="ANTHROPIC",
            fee_bps=0.0, cap_usd=1000.0, orders_per_min=30,
            lighter_profile=profile)
        venue = LighterVenue(conf, object(), 5.0)
        venue.market_id = 32

        async def get(path, params=None, headers=None):
            assert headers is None
            if path == "/api/v1/orderBookDetails":
                assert params == {"market_id": 32}
                return {"order_book_details": [
                    {"market_id": 31, "symbol": "OTHER"},
                    {"market_id": 32, "symbol": "ANTHROPIC",
                     "index_price": "100.0", "mark_price": "100.1"},
                ]}
            assert path == "/api/v1/funding-rates"
            assert params is None
            return {"funding_rates": [
                {"market_id": 32, "exchange": "binance", "rate": 0.0001},
                {"market_id": 32, "exchange": "lighter", "rate": 0.000032},
            ]}

        venue._get = get
        changed = await venue.refresh_reference_rest()

        assert changed is True
        assert venue.reference.snapshot.source == "rest"
        assert venue.reference.snapshot.index_px == 100.0
        assert venue.reference.snapshot.funding_current_bps_per_hour \
            == pytest.approx(0.04)
        assert venue.reference.last_ws_received_mono == 0.0

    asyncio.run(go())


def test_lighter_market_load_survives_initial_reference_http_failure(caplog):
    async def go():
        profile = LighterProfile("robinhood", "https://api", "wss://ws", 1)
        conf = VenueConf(
            key="hedge", kind="lighter", label="RH", symbol="ANTHROPIC",
            fee_bps=0.0, cap_usd=1000.0, orders_per_min=30,
            lighter_profile=profile)
        venue = LighterVenue(conf, object(), 5.0)
        calls = 0

        async def get(path, params=None, headers=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                assert path == "/api/v1/orderBooks"
                return {"order_books": [{
                    "symbol": "ANTHROPIC", "market_id": 38,
                    "status": "active", "supported_price_decimals": 1,
                    "supported_size_decimals": 5,
                    "min_base_amount": "0.0032",
                    "min_quote_amount": "10",
                }]}
            raise aiohttp.ClientConnectionError("reference offline")

        venue._get = get
        await venue.load_market()

        assert venue.market_id == 38
        assert venue.reference.snapshot.source == ""
        assert "initial reference REST failed" in caplog.text

    asyncio.run(go())
