"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.

Both touch the book on any inbound frame (connection-based freshness: a quiet
market is not stale, only a dead feed is) and reconnect with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .reference import InvalidReference, ReferenceState, ReferenceUpdate

log = logging.getLogger("feeds")


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


def _optional_int(data: dict, key: str) -> Optional[int]:
    value = data.get(key)
    return None if value is None else int(value)


def parse_lighter_market_stats(
        msg: dict, market_id: int) -> Optional[ReferenceUpdate]:
    if _chan_id(str(msg.get("channel", ""))) != market_id:
        return None
    stats = msg["market_stats"]
    current = float(stats["current_funding_rate"])
    last = float(stats["funding_rate"])
    return ReferenceUpdate(
        index_px=float(stats["index_price"]),
        mark_px=float(stats["mark_price"]),
        funding_current_bps_per_hour=current * 100.0,
        funding_last_bps_per_hour=last * 100.0,
        funding_last_ts_ms=int(stats["funding_timestamp"]),
        exchange_ts_ms=int(msg["timestamp"]),
    )


def parse_hl_asset_ctx(msg: dict, coin: str) -> Optional[ReferenceUpdate]:
    data = msg["data"]
    if data.get("coin") != coin:
        return None
    ctx = data["ctx"]
    funding = float(ctx["funding"])
    return ReferenceUpdate(
        oracle_px=float(ctx["oraclePx"]),
        mark_px=float(ctx["markPx"]),
        funding_current_bps_per_hour=funding * 1e4,
        exchange_ts_ms=_optional_int(data, "time"),
    )


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None],
                 reference: ReferenceState) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self.reference = reference
        self._nonce: Optional[int] = None
        self._synced = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"market_stats/{self.market_id}"}))

    def _handle_reference(self, msg: dict, *,
                          received_mono: Optional[float] = None) -> None:
        try:
            update = parse_lighter_market_stats(msg, self.market_id)
            if update is not None:
                self.reference.apply(
                    update, source="websocket", received_mono=received_mono)
        except (KeyError, TypeError, ValueError, InvalidReference) as exc:
            log.warning("[%s] invalid reference on market_stats/%d: %s",
                        self.name, self.market_id, exc)

    async def _handle_book(self, ws, msg: dict, snapshot: bool) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin != prev:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await ws.send(json.dumps({"type": "subscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "update/order_book":
                            self.book.touch()
                            await self._handle_book(ws, msg, snapshot=False)
                        elif t == "subscribed/order_book":
                            self.book.touch()
                            await self._handle_book(ws, msg, snapshot=True)
                        elif t in ("update/market_stats",
                                  "subscribed/market_stats"):
                            self._handle_reference(msg)
                        elif t == "connected":
                            self.book.touch()
                            await self._subscribe(ws)
                        elif t == "ping":
                            self.book.touch()
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], reference: ReferenceState,
                 ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.reference = reference
        self.ping_sec = ping_sec
        self._snapped = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": self.coin,
                             "fast": True}}))
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "activeAssetCtx", "coin": self.coin}}))

    def _on_frame(self, msg: dict, *,
                  received_mono: Optional[float] = None) -> None:
        channel = msg.get("channel")
        if channel == "activeAssetCtx":
            try:
                update = parse_hl_asset_ctx(msg, self.coin)
                if update is not None:
                    self.reference.apply(
                        update, source="websocket",
                        received_mono=received_mono)
            except (KeyError, TypeError, ValueError, InvalidReference) as exc:
                log.warning("[%s] invalid reference on activeAssetCtx/%s: %s",
                            self.name, self.coin, exc)
            return
        self.book.touch()
        if channel == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(d["levels"])
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    self.book.clear()
                    self._snapped = False
                    await self._subscribe(ws)
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self._on_frame(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
