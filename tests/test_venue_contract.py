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
from entropy_arb.venue_lighter import AccountOrdersFeed, LighterVenue
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


def test_lighter_client_order_index_is_namespaced_by_api_key(monkeypatch):
    monkeypatch.setattr(lighter_module.time, "time", lambda: 1_800_000_000.0)
    cfg_a = make_cfg("lighter")
    cfg_b = make_cfg("lighter")
    cfg_a.hedge.lighter_creds = SimpleNamespace(api_key_index=2)
    cfg_b.hedge.lighter_creds = SimpleNamespace(api_key_index=3)
    venue_a = LighterVenue(cfg_a.hedge, object(), 0.01)
    venue_b = LighterVenue(cfg_b.hedge, object(), 0.01)

    coi_a = venue_a._next_coi()
    coi_b = venue_b._next_coi()

    assert coi_a != coi_b
    assert coi_a >> 40 == 2
    assert coi_b >> 40 == 3
    assert 0 < coi_a < 2 ** 48
    assert 0 < coi_b < 2 ** 48


def test_lighter_client_order_index_reservation_survives_restart(
        monkeypatch, tmp_path):
    monkeypatch.setattr(lighter_module.time, "time", lambda: 1_800_000_000.0)
    state_path = tmp_path / "lighter-coi.sqlite3"
    cfg = make_cfg("lighter")
    cfg.hedge.lighter_creds = SimpleNamespace(
        account_index=7, api_key_index=2)

    first_process = LighterVenue(cfg.hedge, object(), 0.01)
    first_process._enable_persistent_coi(state_path)
    first_ids = [first_process._next_coi() for _ in range(3)]

    restarted_process = LighterVenue(cfg.hedge, object(), 0.01)
    restarted_process._enable_persistent_coi(state_path)
    restarted_id = restarted_process._next_coi()

    assert restarted_id > first_ids[-1]
    assert restarted_id not in first_ids
    assert restarted_id >> 40 == 2
    assert restarted_id < 2 ** 48


def test_lighter_init_signer_enables_persistent_client_order_ids(
        monkeypatch, tmp_path):
    state_path = tmp_path / "lighter-coi.sqlite3"

    class Signer:
        def __init__(self, **_kwargs):
            self.api_client = SimpleNamespace()

        @staticmethod
        def check_client():
            return None

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Signer))
    monkeypatch.setattr(
        lighter_module, "_default_coi_state_path", lambda: state_path,
        raising=False)
    cfg = make_cfg("lighter")
    cfg.hedge.lighter_creds = SimpleNamespace(
        complete=True, account_index=7, api_key_index=2,
        api_private_key="test-private-key")
    venue = LighterVenue(cfg.hedge, object(), 0.01)

    venue.init_signer()

    assert state_path.is_file()
    assert venue._coi_state_path == state_path


def test_lighter_new_order_skips_preexisting_terminal_result(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    submitted = []

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        cfg.hedge.lighter_creds = SimpleNamespace(api_key_index=2)
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        venue._coi = 41
        feed = AccountOrdersFeed(
            "RH", "wss://ws", 7, 3, object(), api_key_index=2)
        venue.orders_feed = feed
        old_coi = venue._coi_prefix | 42
        feed._resolve(old_coi, {
            "status": "filled",
            "filled_base": 0.75,
            "filled_quote": 75.0,
            "avg_px": 100.0,
        })

        class AcceptedSigner:
            async def create_order(self, **kwargs):
                coi = kwargs["client_order_index"]
                submitted.append(coi)
                feed._resolve(coi, {
                    "status": "filled",
                    "filled_base": 0.5,
                    "filled_quote": 50.0,
                    "avg_px": 100.0,
                })
                return None, SimpleNamespace(code=200), None

        venue.signer = AcceptedSigner()
        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert submitted == [venue._coi_prefix | 43]
        assert result == OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0,
            order_ref=str(submitted[0]))
        assert feed.terminal(old_coi) is not None

    asyncio.run(go())


def test_lighter_coi_allocation_failure_is_definitive_send_failure(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)

        class Signer:
            calls = 0

            async def create_order(self, **_kwargs):
                self.calls += 1
                raise AssertionError("create_order must not be called")

        signer = Signer()
        venue.signer = signer

        def fail_before_submit():
            raise OSError("COI database is locked")

        monkeypatch.setattr(venue, "_next_coi", fail_before_submit)
        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.status == "send-failed"
        assert result.unresolved is False
        assert result.order_ref is None
        assert "client_order_index allocation failed" in result.err
        assert "COI database is locked" in result.err
        assert signer.calls == 0

    asyncio.run(go())


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


def test_hl_parse_marks_unrecognised_accepted_status_as_unknown():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"queued": {"oid": 1}}
    ]}}}

    result = HLVenue._parse(body)

    assert result.status == "unknown-status"
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


def test_hip3_positive_fill_without_average_price_is_unknown():
    async def go():
        venue = live_hl_venue()

        async def post_exchange(_payload):
            return ({"status": "ok", "response": {"data": {"statuses": [
                {"filled": {"totalSz": "0.5"}}
            ]}}}, None, False)

        venue._post_exchange = post_exchange

        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.unresolved is True
        assert result.filled_base == 0.0
        assert "average price" in (result.err or "")

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


def test_hl_http_408_is_an_unknown_order_outcome():
    class Response:
        status = 408

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @staticmethod
        async def text():
            return "upstream request timeout"

    class Session:
        @staticmethod
        def post(*_args, **_kwargs):
            return Response()

    async def go():
        venue = live_hl_venue()
        venue.session = Session()

        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.unresolved is True
        assert result.status == "exchange response unknown"

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
            self.retained = []

        def watch(self, coi):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

        @staticmethod
        def terminal(_coi):
            return None

        def unwatch(self, coi):
            self.unwatched.append(coi)
            if self.future is not None and not self.future.done():
                self.future.cancel()

        def retain(self, coi):
            self.retained.append(coi)

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
        assert result.order_ref is not None
        assert orders.unwatched
        assert orders.retained == orders.unwatched
        assert signer.cancelled is True

    asyncio.run(go())


def test_lighter_submission_transport_error_returns_unknown_and_unwatches(
        monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    class FailingSigner:
        async def create_order(self, **_kwargs):
            raise OSError("connection reset after write")

    class OrdersFeed:
        def __init__(self):
            self.future = None
            self.unwatched = []
            self.retained = []

        def watch(self, coi):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

        @staticmethod
        def terminal(_coi):
            return None

        def unwatch(self, coi):
            self.unwatched.append(coi)
            if self.future is not None and not self.future.done():
                self.future.cancel()

        def retain(self, coi):
            self.retained.append(coi)

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        orders = OrdersFeed()
        venue.signer = FailingSigner()
        venue.orders_feed = orders

        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.unresolved is True
        assert result.status == "order submission error"
        assert result.order_ref is not None
        assert "connection reset after write" in result.err
        assert orders.unwatched
        assert orders.retained == orders.unwatched

    asyncio.run(go())


def test_lighter_malformed_terminal_result_keeps_order_reference(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    class OrdersFeed:
        def __init__(self):
            self.future = None
            self.retained = []

        def watch(self, _coi):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

        @staticmethod
        def terminal(_coi):
            return None

        def unwatch(self, _coi):
            if self.future is not None and not self.future.done():
                self.future.cancel()

        def retain(self, coi):
            self.retained.append(coi)

    class AcceptedSigner:
        def __init__(self, orders):
            self.orders = orders

        async def create_order(self, **_kwargs):
            self.orders.future.set_result({
                "status": "filled",
                "filled_base": float("nan"),
                "avg_px": 100.0,
            })
            return None, SimpleNamespace(code=200), None

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        orders = OrdersFeed()
        venue.signer = AcceptedSigner(orders)
        venue.orders_feed = orders

        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.unresolved is True
        assert result.order_ref is not None
        assert "terminal" in result.status
        assert orders.retained == [int(result.order_ref)]

    asyncio.run(go())


def test_lighter_structured_429_response_is_rate_limited(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    class RejectedSigner:
        @staticmethod
        async def create_order(**_kwargs):
            return None, SimpleNamespace(
                code=429, message="too many requests"), None

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        venue.signer = RejectedSigner()

        result = await venue.send_taker(
            is_buy=True, qty=0.5, limit_px=100.0)

        assert result.rate_limited is True
        assert result.status == "send-failed"

    asyncio.run(go())


def test_lighter_start_failure_does_not_leave_book_task(monkeypatch):
    class BrokenAccountFeed:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("account feed init failed")

    monkeypatch.setattr(lighter_module, "AccountOrdersFeed", BrokenAccountFeed)

    async def go():
        cfg = make_cfg("lighter")
        cfg.hedge.lighter_creds = SimpleNamespace(
            account_index=7, api_key_index=2)
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


def test_lighter_failed_key_check_keeps_signer_owned_for_cleanup(monkeypatch):
    created = []

    class ApiClient:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class RejectedSigner:
        def __init__(self, **_kwargs):
            self.api_client = ApiClient()
            created.append(self)

        @staticmethod
        def check_client():
            return "bad key"

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=RejectedSigner))

    async def go():
        cfg = make_cfg("lighter")
        cfg.hedge.lighter_creds = SimpleNamespace(
            complete=True, account_index=7, api_key_index=2,
            api_private_key="test-private-key")
        venue = LighterVenue(cfg.hedge, object(), 0.01)

        with pytest.raises(RuntimeError, match="API key check failed"):
            venue.init_signer()

        assert venue.signer is created[0]
        await venue.close()
        assert created[0].api_client.closed is True

    asyncio.run(go())


def test_lighter_unrecognised_order_status_does_not_settle_pending_order():
    async def go():
        feed = AccountOrdersFeed(
            "RH", "wss://ws", 7, 3, object(), api_key_index=2)
        pending = feed.watch(42)

        feed._handle_orders({"orders": {"7": [{
            "client_order_index": 42,
            "status": "",
            "filled_base_amount": "0",
            "filled_quote_amount": "0",
        }]}})

        assert pending.done() is False
        assert 42 in feed._pending
        feed.unwatch(42)

    asyncio.run(go())


def test_lighter_terminal_order_notifies_recovery_progress():
    notifications = []
    feed = AccountOrdersFeed(
        "RH", "wss://ws", 7, 3, object(), api_key_index=2,
        notify=lambda: notifications.append(None))
    feed.retain(42)

    feed._handle_orders({"orders": {"7": [{
        "client_order_index": 42,
        "status": "filled",
        "filled_base_amount": "0.5",
        "filled_quote_amount": "50",
    }]}})

    assert notifications == [None]


def test_lighter_unrelated_terminal_order_does_not_notify_recovery_progress():
    notifications = []
    feed = AccountOrdersFeed(
        "RH", "wss://ws", 7, 3, object(), api_key_index=2,
        notify=lambda: notifications.append(None))
    feed.retain(42)

    feed._handle_orders({"orders": {"7": [{
        "client_order_index": 99,
        "status": "filled",
        "filled_base_amount": "0.5",
        "filled_quote_amount": "50",
    }]}})

    assert notifications == []
    assert feed.terminal(42) is None


def test_lighter_account_feed_auth_uses_configured_api_key(monkeypatch):
    class Signer:
        def __init__(self):
            self.api_key_indexes = []

        def create_auth_token_with_expiry(self, *, api_key_index):
            self.api_key_indexes.append(api_key_index)
            return "test-auth", None

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def __aiter__(self):
            async def messages():
                yield '{"type":"connected"}'
            return messages()

        async def send(self, _message):
            stop.set()

    stop = asyncio.Event()
    signer = Signer()
    monkeypatch.setattr(lighter_module, "ws_connect", lambda *_a, **_k: Socket())
    feed = AccountOrdersFeed(
        "RH", "wss://ws", 7, 3, signer, api_key_index=2)

    asyncio.run(feed.run(stop))

    assert signer.api_key_indexes == [2]


def test_lighter_nonfinite_terminal_fill_does_not_settle_pending_order():
    async def go():
        feed = AccountOrdersFeed(
            "RH", "wss://ws", 7, 3, object(), api_key_index=2)
        pending = feed.watch(42)

        feed._handle_orders({"orders": {"7": [{
            "client_order_index": 42,
            "status": "filled",
            "filled_base_amount": "nan",
            "filled_quote_amount": "50",
        }]}})

        assert pending.done() is False
        assert feed.terminal(42) is None
        feed.unwatch(42)

    asyncio.run(go())


def test_lighter_terminal_order_cache_resolves_unknown_order_reference():
    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.orders_feed = AccountOrdersFeed(
            "RH", "wss://ws", 7, 3, object(), api_key_index=2)

        venue.orders_feed._handle_orders({"orders": {"7": [{
            "client_order_index": 42,
            "status": "filled",
            "filled_base_amount": "0.5",
            "filled_quote_amount": "50",
        }]}})

        result = await venue.resolve_order("42")
        assert result == OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        assert venue.orders_feed.terminal(42) is None

    asyncio.run(go())


def test_lighter_retained_terminal_is_not_evicted_before_engine_consumes_it():
    feed = AccountOrdersFeed(
        "RH", "wss://ws", 7, 3, object(), api_key_index=2)
    feed.retain(42)
    feed._resolve(42, {
        "status": "filled", "filled_base": 0.5,
        "filled_quote": 50.0, "avg_px": 100.0,
    })

    for coi in range(1000, 1600):
        feed._resolve(coi, {
            "status": "canceled", "filled_base": 0.0,
            "filled_quote": 0.0, "avg_px": None,
        })

    assert feed.terminal(42) is not None
    feed.consume_terminal(42)
    assert feed.terminal(42) is None


def test_lighter_resolve_order_uses_authenticated_rest_on_cache_miss():
    async def go():
        cfg = make_cfg("lighter")
        cfg.hedge.lighter_creds = SimpleNamespace(
            account_index=7, api_key_index=2)
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 9
        venue.orders_feed = AccountOrdersFeed(
            "RH", "wss://ws", 9, 7, object(), api_key_index=2)
        auth_indexes = []

        def create_auth_token_with_expiry(*, api_key_index):
            auth_indexes.append(api_key_index)
            return "test-auth", None

        venue.signer = SimpleNamespace(
            create_auth_token_with_expiry=create_auth_token_with_expiry)
        calls = []

        async def get(path, params=None, headers=None):
            calls.append((path, params, headers))
            return {"code": 200, "orders": [{
                "client_order_index": 42,
                "market_index": 9,
                "status": "filled",
                "filled_base_amount": "0.5",
                "filled_quote_amount": "50",
            }]}

        venue._get = get

        result = await venue.resolve_order("42")

        assert result == OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        assert calls == [("/api/v1/accountOrders", {
            "account_index": 7,
            "client_order_indexes": "42",
        }, {"authorization": "test-auth"})]
        assert auth_indexes == [2]

    asyncio.run(go())


def test_lighter_close_propagates_sdk_close_error():
    class BrokenClient:
        async def close(self):
            raise OSError("sdk close failed")

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.signer = SimpleNamespace(api_client=BrokenClient())

        with pytest.raises(OSError, match="sdk close failed"):
            await venue.close()

    asyncio.run(go())
