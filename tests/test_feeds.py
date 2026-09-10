import asyncio
import json

import pytest

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed
from entropy_arb.reference import ReferenceState


class StubWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def message(event, nonce, begin_nonce=None, bid="100"):
    order_book = {
        "nonce": nonce,
        "bids": [{"price": bid, "size": "1"}],
        "asks": [{"price": "101", "size": "1"}],
    }
    if begin_nonce is not None:
        order_book["begin_nonce"] = begin_nonce
    return {
        "type": event,
        "channel": "order_book:32",
        "order_book": order_book,
    }


def test_lighter_diff_requires_begin_nonce_to_equal_previous_nonce():
    async def go():
        book = OrderBook()
        ws = StubWebSocket()
        feed = LighterBookFeed(
            "RH", "wss://example", 32, book, lambda: None,
            ReferenceState())
        await feed._handle_book(
            ws, message("subscribed/order_book", nonce=100), snapshot=True)

        await feed._handle_book(
            ws, message("update/order_book", nonce=102, begin_nonce=101),
            snapshot=False)

        assert feed._synced is False
        assert feed._nonce is None
        assert book.ready is False
        assert [item["type"] for item in ws.sent] == ["unsubscribe", "subscribe"]

    asyncio.run(go())


def test_lighter_diff_accepts_exact_nonce_continuation():
    async def go():
        book = OrderBook()
        ws = StubWebSocket()
        feed = LighterBookFeed(
            "RH", "wss://example", 32, book, lambda: None,
            ReferenceState())
        await feed._handle_book(
            ws, message("subscribed/order_book", nonce=100), snapshot=True)

        await feed._handle_book(
            ws, message("update/order_book", nonce=101, begin_nonce=100,
                        bid="100.5"), snapshot=False)

        assert feed._synced is True
        assert feed._nonce == 101
        assert book.best_bid() == 100.5
        assert ws.sent == []

    asyncio.run(go())


def test_lighter_subscribes_book_and_market_stats_on_same_socket():
    async def go():
        ws = StubWebSocket()
        feed = LighterBookFeed(
            "RH", "wss://example", 32, OrderBook(), lambda: None,
            ReferenceState())

        await feed._subscribe(ws)

        assert ws.sent == [
            {"type": "subscribe", "channel": "order_book/32"},
            {"type": "subscribe", "channel": "market_stats/32"},
        ]

    asyncio.run(go())


def test_lighter_market_stats_filters_market_and_converts_percent_to_bps():
    state = ReferenceState()
    feed = LighterBookFeed(
        "RH", "wss://example", 32, OrderBook(), lambda: None, state)

    feed._handle_reference({
        "type": "update/market_stats",
        "channel": "market_stats:31",
        "market_stats": {"index_price": "9"},
    }, received_mono=9.0)
    feed._handle_reference({
        "type": "update/market_stats",
        "channel": "market_stats:32",
        "market_stats": {
            "index_price": "100.0",
            "mark_price": "100.1",
            "current_funding_rate": "0.0012",
            "funding_rate": "0.0008",
            "funding_timestamp": 1234,
            "timestamp": 5678,
        },
    }, received_mono=10.0)

    assert state.snapshot.index_px == 100.0
    assert state.snapshot.mark_px == 100.1
    assert state.snapshot.funding_current_bps_per_hour == pytest.approx(0.12)
    assert state.snapshot.funding_last_bps_per_hour == pytest.approx(0.08)
    assert state.snapshot.funding_last_ts_ms == 1234
    assert state.snapshot.exchange_ts_ms == 5678


def test_bad_lighter_reference_does_not_change_book_state(caplog):
    book = OrderBook()
    book.apply_lighter({
        "bids": [{"price": "99", "size": "1"}],
        "asks": [{"price": "101", "size": "1"}],
    }, snapshot=True)
    state = ReferenceState()
    feed = LighterBookFeed(
        "RH", "wss://example", 32, book, lambda: None, state)
    feed._nonce = 77
    feed._synced = True
    before = (dict(book.bids), dict(book.asks), book.ready,
              book.alive_mono, feed._nonce, feed._synced)

    feed._handle_reference({
        "type": "update/market_stats",
        "channel": "market_stats:32",
        "market_stats": {"index_price": "not-a-number"},
    }, received_mono=10.0)

    assert (book.bids, book.asks, book.ready, book.alive_mono,
            feed._nonce, feed._synced) == before
    assert state.snapshot.source == ""
    assert "invalid reference" in caplog.text


def test_hl_subscribes_book_and_asset_context_on_same_socket():
    async def go():
        ws = StubWebSocket()
        feed = HLBookFeed(
            "ENTROPY", "wss://example", "io:ANTH", OrderBook(),
            lambda: None, ReferenceState())

        await feed._subscribe(ws)

        assert ws.sent == [
            {"method": "subscribe", "subscription": {
                "type": "l2Book", "coin": "io:ANTH", "fast": True}},
            {"method": "subscribe", "subscription": {
                "type": "activeAssetCtx", "coin": "io:ANTH"}},
        ]

    asyncio.run(go())


def test_hl_asset_context_filters_coin_and_converts_fraction_to_bps():
    state = ReferenceState()
    feed = HLBookFeed(
        "ENTROPY", "wss://example", "io:ANTH", OrderBook(),
        lambda: None, state)

    feed._on_frame({
        "channel": "activeAssetCtx",
        "data": {"coin": "xyz:ANTH", "ctx": {"oraclePx": "9"}},
    }, received_mono=9.0)
    feed._on_frame({
        "channel": "activeAssetCtx",
        "data": {"coin": "io:ANTH", "ctx": {
            "oraclePx": "100.2",
            "markPx": "100.3",
            "funding": "0.000032",
        }},
    }, received_mono=10.0)

    assert state.snapshot.oracle_px == 100.2
    assert state.snapshot.mark_px == 100.3
    assert state.snapshot.funding_current_bps_per_hour == pytest.approx(0.32)


def test_bad_hl_reference_does_not_touch_or_clear_book(caplog):
    book = OrderBook()
    book.apply_hl([
        [{"px": "99", "sz": "1"}],
        [{"px": "101", "sz": "1"}],
    ])
    state = ReferenceState()
    feed = HLBookFeed(
        "ENTROPY", "wss://example", "io:ANTH", book,
        lambda: None, state)
    before = (dict(book.bids), dict(book.asks), book.ready, book.alive_mono)

    feed._on_frame({
        "channel": "activeAssetCtx",
        "data": {"coin": "io:ANTH", "ctx": {"oraclePx": "nan"}},
    }, received_mono=10.0)

    assert (book.bids, book.asks, book.ready, book.alive_mono) == before
    assert state.snapshot.source == ""
    assert "invalid reference" in caplog.text
