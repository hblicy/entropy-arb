import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

from entropy_arb.config import load_config
from entropy_arb.models import OrderResult
import entropy_arb.venue_lighter as lighter_module
from entropy_arb.venue_hl import HLVenue, NonceAllocator
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.venues.base import VenueAdapter


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
MINIMAL = """
thresholds:
  midline_bps: 0
  upper_bps: 4
  lower_bps: 4
"""


def make_cfg(hedge):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(MINIMAL)
    f.close()
    return load_config(
        f.name, NO_ENV, symbol="SNDK", hedge_venue=hedge)


def test_existing_venues_satisfy_runtime_protocol():
    hl_cfg = make_cfg("tradexyz")
    lighter_cfg = make_cfg("lighter")
    hl = HLVenue(hl_cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    lighter = LighterVenue(lighter_cfg.hedge, object(), 5.0)
    assert isinstance(hl, VenueAdapter)
    assert isinstance(lighter, VenueAdapter)


def test_hl_parse_returns_typed_fill():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.5", "avgPx": "100.25"}}
    ]}}}
    result = HLVenue._parse(body)
    assert result == OrderResult(
        status="filled", filled_base=0.5, avg_px=100.25)


def test_hl_parse_marks_resting_ioc_as_unknown():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 1}}
    ]}}}
    result = HLVenue._parse(body)
    assert result.status == "resting?"
    assert result.unresolved is True


def test_hl_peer_setup_shares_nonce_and_deduplicates_equity():
    cfg = make_cfg("tradexyz")
    anchor = HLVenue(cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    hedge = HLVenue(cfg.hedge, "https://api", "wss://ws", object(), 5.0)
    anchor.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())
    hedge.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())

    anchor.configure_peer(hedge)

    assert hedge.account.nonces is anchor.account.nonces
    assert hedge.include_core_equity is False


class FakeSigning:
    def __init__(self):
        self.order_request = None

    def order_request_to_order_wire(self, request, _asset_id):
        self.order_request = request
        return {"wire": "order"}

    @staticmethod
    def order_wires_to_order_action(wires):
        return {"orders": wires}

    @staticmethod
    def sign_l1_action(*_args):
        return {"signature": "test"}


def live_hl_venue(settle_timeout=0.01):
    cfg = make_cfg("tradexyz")
    venue = HLVenue(
        cfg.entropy, "https://api", "wss://ws", object(), settle_timeout)
    venue.coin = "io:SNDK"
    venue.asset_id = 110000
    venue._signing = FakeSigning()
    venue.account = SimpleNamespace(
        wallet=object(), query_address="0xaccount", is_mainnet=True,
        nonces=SimpleNamespace(next=lambda: 123))
    venue._next_cloid = lambda: SimpleNamespace(to_raw=lambda: "0xtest")
    return venue


def test_hip3_order_request_omits_cloid():
    async def go():
        venue = live_hl_venue()

        async def post_exchange(_payload):
            return ({"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.5", "avgPx": "100"}}
            ]}}}, None, False)

        venue._post_exchange = post_exchange
        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.status == "filled"
        assert "cloid" not in venue._signing.order_request

    asyncio.run(go())


def test_hip3_unknown_response_does_not_poll_by_cloid():
    async def go():
        venue = live_hl_venue()
        info_calls = 0

        async def post_exchange(_payload):
            return None, None, True

        async def info(_payload):
            nonlocal info_calls
            info_calls += 1
            return None

        venue._post_exchange = post_exchange
        venue._info = info
        result = await venue.send_taker(
            is_buy=False, qty=0.5, limit_px=100.0)

        assert result.unresolved is True
        assert info_calls == 0

    asyncio.run(go())


def test_lighter_submission_timeout_returns_unknown_and_unwatches(
        monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    class HangingSigner:
        def __init__(self):
            self.cancelled = False

        async def create_order(self, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    class OrdersFeed:
        def __init__(self):
            self.future = None
            self.unwatched = []

        def watch(self, coi):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

        def unwatch(self, coi):
            self.unwatched.append(coi)
            if self.future is not None and not self.future.done():
                self.future.cancel()

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        signer = HangingSigner()
        orders = OrdersFeed()
        venue.signer = signer
        venue.orders_feed = orders

        result = await asyncio.wait_for(
            venue.send_taker(is_buy=True, qty=0.5, limit_px=100.0),
            timeout=0.05)

        assert result.unresolved is True
        assert result.status == "order submission timed out"
        assert orders.unwatched
        assert signer.cancelled is True

    asyncio.run(go())


def test_lighter_start_failure_does_not_leave_book_task(monkeypatch):
    class BrokenAccountFeed:
        def __init__(self, *_args):
            raise RuntimeError("account feed init failed")

    monkeypatch.setattr(lighter_module, "AccountOrdersFeed", BrokenAccountFeed)

    async def go():
        cfg = make_cfg("lighter")
        cfg.hedge.lighter_creds = SimpleNamespace(account_index=7)
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        before = set(asyncio.all_tasks())
        leaked = []
        try:
            with pytest.raises(RuntimeError, match="account feed init failed"):
                venue.start_tasks(
                    asyncio.Event(), lambda: None, live=True)
            leaked = [task for task in asyncio.all_tasks()
                      if task not in before and not task.done()]
            assert leaked == []
        finally:
            for task in leaked:
                task.cancel()
            if leaked:
                await asyncio.gather(*leaked, return_exceptions=True)

    asyncio.run(go())
