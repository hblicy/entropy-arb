import asyncio

import aiohttp
import pytest

from entropy_arb.config import VenueConf
from entropy_arb.reference import ReferenceState
from entropy_arb.venue_hl import HLVenue, parse_hl_rest_asset_ctx


def test_hl_venue_owns_reference_state():
    conf = VenueConf(
        key="entropy", kind="hl", label="ENTROPY", symbol="ANTH",
        fee_bps=0.9, cap_usd=1000.0, orders_per_min=120, hl_dex="io")

    venue = HLVenue(conf, "https://api", "wss://ws", object(), 5.0)

    assert isinstance(venue.reference, ReferenceState)


def test_hl_rest_asset_context_converts_hourly_fraction_to_bps():
    update = parse_hl_rest_asset_ctx({
        "oraclePx": "100.2",
        "markPx": "100.3",
        "funding": "0.000032",
    })

    assert update.oracle_px == 100.2
    assert update.mark_px == 100.3
    assert update.funding_current_bps_per_hour == pytest.approx(0.32)


def test_hl_refresh_reference_rest_selects_matching_universe_context():
    async def go():
        conf = VenueConf(
            key="entropy", kind="hl", label="ENTROPY", symbol="ANTH",
            fee_bps=0.9, cap_usd=1000.0, orders_per_min=120, hl_dex="io")
        venue = HLVenue(conf, "https://api", "wss://ws", object(), 5.0)
        venue.coin = "io:ANTH"

        async def info(payload):
            assert payload == {"type": "metaAndAssetCtxs", "dex": "io"}
            return [
                {"universe": [{"name": "io:OTHER"}, {"name": "io:ANTH"}]},
                [{"oraclePx": "9", "markPx": "9", "funding": "0"},
                 {"oraclePx": "100.2", "markPx": "100.3",
                  "funding": "0.000032"}],
            ]

        venue._info = info
        changed = await venue.refresh_reference_rest()

        assert changed is True
        assert venue.reference.snapshot.source == "rest"
        assert venue.reference.snapshot.oracle_px == 100.2
        assert venue.reference.last_ws_received_mono == 0.0

    asyncio.run(go())


def test_hl_market_load_survives_initial_reference_http_failure(caplog):
    async def go():
        conf = VenueConf(
            key="entropy", kind="hl", label="ENTROPY", symbol="ANTH",
            fee_bps=0.9, cap_usd=1000.0, orders_per_min=120, hl_dex="io")
        venue = HLVenue(conf, "https://api", "wss://ws", object(), 5.0)

        async def info(payload):
            if payload["type"] == "perpDexs":
                return [None, {"name": "io"}]
            if payload["type"] == "meta":
                return {"universe": [{
                    "name": "io:ANTH", "szDecimals": 4,
                    "maxLeverage": 5}]}
            raise aiohttp.ClientConnectionError("reference offline")

        venue._info = info
        await venue.load_market()

        assert venue.coin == "io:ANTH"
        assert venue.reference.snapshot.source == ""
        assert "initial reference REST failed" in caplog.text

    asyncio.run(go())
