import asyncio
import json

from entropy_arb.book import OrderBook
from entropy_arb.feeds import LighterBookFeed


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
        feed = LighterBookFeed("RH", "wss://example", 32, book, lambda: None)
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
        feed = LighterBookFeed("RH", "wss://example", 32, book, lambda: None)
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
