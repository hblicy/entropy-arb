"""zkLighter venue adapter (Lighter mainnet / Lighter Robinhood chain).

Market data and account state come from Lighter's public REST + websocket
APIs via plain aiohttp/websockets, so --record-only data collection works
without the SDK. Trading lazily imports the official `lighter` SDK
(https://github.com/elliottech/lighter-python) for transaction signing only.

Market orders carry mandatory avg-execution-price protection and settle
asynchronously on the authenticated account_orders websocket; send_taker()
hides that behind the same normalized OrderResult returned by Hyperliquid.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Optional

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import VenueConf
from .feeds import LighterBookFeed
from .models import OrderResult
from .reference import ReferenceState

log = logging.getLogger("lighter")

OPEN_STATUSES = {"in-progress", "pending", "open"}
TERMINAL_STATUSES = {
    "filled",
    "canceled",
    "canceled-post-only",
    "canceled-reduce-only",
    "canceled-position-not-allowed",
    "canceled-margin-not-allowed",
    "canceled-too-much-slippage",
    "canceled-not-enough-liquidity",
    "canceled-self-trade",
    "canceled-expired",
    "canceled-oco",
    "canceled-child",
    "canceled-liquidation",
    "canceled-invalid-balance",
}
AUTH_REFRESH_SEC = 8 * 60
REST_TIMEOUT = 10.0
COI_COUNTER_BITS = 40
COI_COUNTER_MASK = (1 << COI_COUNTER_BITS) - 1
COI_RESERVATION_SIZE = 1 << 16


def _default_coi_state_path() -> Path:
    configured = os.getenv("XDG_STATE_HOME")
    if configured:
        base = Path(configured)
    elif os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home()
    else:
        base = Path.home() / ".local" / "state"
    return base / "entropy-arb" / "lighter-coi.sqlite3"


def _reserve_coi_range(
        state_path: Path, namespace: str, floor: int) -> tuple[int, int]:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        state_path, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS coi_ranges ("
            "namespace TEXT PRIMARY KEY, last_reserved INTEGER NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT last_reserved FROM coi_ranges WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        previous = int(row[0]) if row is not None else 0
        first = max(previous + 1, floor, 1)
        last = first + COI_RESERVATION_SIZE - 1
        if last > COI_COUNTER_MASK:
            raise RuntimeError(
                "Lighter client_order_index counter space is exhausted")
        connection.execute(
            "INSERT INTO coi_ranges(namespace, last_reserved) VALUES(?, ?) "
            "ON CONFLICT(namespace) DO UPDATE SET last_reserved=excluded.last_reserved",
            (namespace, last),
        )
        connection.execute("COMMIT")
        return first, last
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


class AccountOrdersFeed:
    """Authenticated stream of our own order updates (settlement channel)."""

    def __init__(self, name: str, ws_url: str, market_id: int,
                 account_index: int, signer, *, api_key_index: int,
                 notify: Optional[Callable[[], None]] = None) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.signer = signer
        self.notify = notify
        self.ready = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._terminal: OrderedDict[int, dict] = OrderedDict()
        self._retained: set[int] = set()

    def watch(self, coi: int) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if coi in self._terminal:
            fut.set_result(self._terminal[coi])
            return fut
        self._pending[coi] = fut
        return fut

    def unwatch(self, coi: int) -> None:
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def terminal(self, coi: int) -> Optional[dict]:
        return self._terminal.get(coi)

    def retain(self, coi: int) -> None:
        self._retained.add(coi)

    def consume_terminal(self, coi: int) -> None:
        self._retained.discard(coi)
        self._terminal.pop(coi, None)

    def _resolve(self, coi: int, info: dict) -> None:
        tracked = coi in self._pending or coi in self._retained
        self._terminal[coi] = info
        while len(self._terminal) > 512:
            removable = next(
                (key for key in self._terminal if key not in self._retained),
                None)
            if removable is None:
                break
            self._terminal.pop(removable)
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.set_result(info)
        if tracked and self.notify is not None:
            self.notify()

    def _handle_orders(self, msg: dict) -> None:
        for lst in (msg.get("orders") or {}).values():
            for o in lst or []:
                status = str(o.get("status", ""))
                if status in OPEN_STATUSES:
                    continue
                if status not in TERMINAL_STATUSES:
                    log.warning(
                        "[%s] unrecognised order status %r; awaiting a known "
                        "terminal update", self.name, status)
                    continue
                try:
                    coi = int(o.get("client_order_index"))
                except (TypeError, ValueError):
                    continue
                try:
                    fb = float(o.get("filled_base_amount") or 0.0)
                    fq = float(o.get("filled_quote_amount") or 0.0)
                except (TypeError, ValueError):
                    log.warning(
                        "[%s] malformed terminal fill for coi %d; awaiting a "
                        "valid update", self.name, coi)
                    continue
                if (not math.isfinite(fb) or not math.isfinite(fq)
                        or fb < 0 or fq < 0
                        or (fb == 0 and fq != 0)
                        or (fb > 0 and fq <= 0)):
                    log.warning(
                        "[%s] invalid terminal fill for coi %d; awaiting a "
                        "valid update", self.name, coi)
                    continue
                self._resolve(coi, {"status": status, "filled_base": fb,
                                    "filled_quote": fq,
                                    "avg_px": (fq / fb) if fb > 0 else None})

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                auth, err = self.signer.create_auth_token_with_expiry(
                    api_key_index=self.api_key_index)
                if err is not None:
                    raise RuntimeError(f"auth token: {err}")
                connected_at = time.monotonic()
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t in ("subscribed/account_orders", "update/account_orders"):
                            if t.startswith("subscribed"):
                                log.info("[%s] account orders stream ready", self.name)
                            self.ready.set()
                            self._handle_orders(msg)
                        elif t == "connected":
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"account_orders/{self.market_id}/"
                                           f"{self.account_index}",
                                "auth": auth}))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
                        if (time.monotonic() - connected_at > AUTH_REFRESH_SEC
                                and not self._pending):
                            log.info("[%s] refreshing account ws auth", self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] account ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
                self.ready.clear()
                if stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.ready.clear()


class LighterVenue:
    kind = "lighter"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        assert conf.lighter_profile is not None
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.profile = conf.lighter_profile
        self.book = OrderBook()
        self.reference = ReferenceState()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market_id = -1
        self.price_decimals = 2
        self.size_decimals = 4
        self.min_base = 0.0
        self.min_quote = 10.0
        self.signer = None
        self.orders_feed: Optional[AccountOrdersFeed] = None
        creds = conf.lighter_creds
        api_key_index = getattr(creds, "api_key_index", None)
        if api_key_index is None:
            api_key_index = 0
        self._coi_prefix = int(api_key_index) << COI_COUNTER_BITS
        self._coi = int(time.time() * 1000) & COI_COUNTER_MASK
        self._coi_limit = COI_COUNTER_MASK
        self._coi_state_path: Optional[Path] = None
        self._coi_namespace: Optional[str] = None

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None,
                   headers: Optional[dict] = None):
        request_kwargs = {
            "params": params,
            "timeout": aiohttp.ClientTimeout(total=REST_TIMEOUT),
        }
        if headers is not None:
            request_kwargs["headers"] = headers
        async with self.session.get(
                self.profile.api_url + path, **request_kwargs) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/api/v1/orderBooks")
        for ob in data.get("order_books") or []:
            if ob.get("symbol") != self.conf.symbol:
                continue
            if ob.get("status") != "active":
                raise RuntimeError(f"[{self.name}] market status={ob.get('status')}")
            self.market_id = int(ob["market_id"])
            self.price_decimals = int(ob["supported_price_decimals"])
            self.size_decimals = int(ob["supported_size_decimals"])
            self.min_base = float(ob["min_base_amount"])
            self.min_quote = float(ob["min_quote_amount"])
            log.info("[%s] %s market_id=%d px_dec=%d sz_dec=%d min_base=%s "
                     "min_quote=%s taker_fee=%s", self.name, ob["symbol"],
                     self.market_id, self.price_decimals, self.size_decimals,
                     ob["min_base_amount"], ob["min_quote_amount"],
                     ob.get("taker_fee"))
            return
        raise RuntimeError(f"[{self.name}] {self.conf.symbol} not found on "
                           f"{self.profile.name}")

    def init_signer(self) -> None:
        c = self.conf.lighter_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from lighter import SignerClient
        except ImportError as e:
            raise RuntimeError(
                "live trading on Lighter needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(git+https://github.com/elliottech/lighter-python.git)") from e
        signer = SignerClient(
            url=self.profile.api_url,
            account_index=c.account_index,
            api_private_keys={c.api_key_index: c.api_private_key},
            chain_id=self.profile.chain_id,
        )
        self.signer = signer
        err = signer.check_client()
        if err is not None:
            raise RuntimeError(f"[{self.name}] API key check failed: {err}")
        self._enable_persistent_coi(_default_coi_state_path())
        log.info("[%s] signer ready (account %d)", self.name, c.account_index)

    def configure_peer(self, other) -> None:
        """Lighter deployments do not share Hyperliquid account state."""
        return

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        def book_notify() -> None:
            notify("book", self.key)

        def order_notify() -> None:
            notify("order", self.key)

        orders_feed = None
        if live:
            c = self.conf.lighter_creds
            orders_feed = AccountOrdersFeed(
                self.name, self.profile.ws_url, self.market_id,
                c.account_index, self.signer, api_key_index=c.api_key_index,
                notify=order_notify)
        tasks = [asyncio.create_task(
            LighterBookFeed(self.name, self.profile.ws_url, self.market_id,
                            self.book, book_notify, self.reference).run(stop),
            name=f"book-{self.key}")]
        if orders_feed is not None:
            self.orders_feed = orders_feed
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"acct-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        return self.orders_feed is not None and self.orders_feed.ready.is_set()

    async def warm_http(self) -> None:
        """Keep the order-path HTTPS connections warm (a cold TLS handshake
        adds 10-15ms to the first order after an idle spell)."""
        try:
            await self._get("/api/v1/status")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)
        if self.signer is None:
            return
        try:
            sess = self.signer.api_client.rest_client.pool_manager
            async with sess.get(self.profile.api_url + "/api/v1/status",
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except Exception as e:
            log.debug("[%s] signer keepalive failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    def _enable_persistent_coi(self, state_path) -> None:
        c = self.conf.lighter_creds
        account_index = getattr(c, "account_index", None)
        api_key_index = getattr(c, "api_key_index", None)
        if account_index is None or api_key_index is None:
            raise RuntimeError(
                f"[{self.name}] cannot allocate client_order_index without "
                "account and API key indexes")
        path = Path(state_path)
        namespace = (
            f"{self.profile.chain_id}:{account_index}:{api_key_index}")
        floor = int(time.time() * 1000) & COI_COUNTER_MASK
        first, last = _reserve_coi_range(path, namespace, floor)
        self._coi_state_path = path
        self._coi_namespace = namespace
        self._coi = first - 1
        self._coi_limit = last

    def _reserve_more_coi(self) -> None:
        assert self._coi_state_path is not None
        assert self._coi_namespace is not None
        first, last = _reserve_coi_range(
            self._coi_state_path, self._coi_namespace, self._coi + 1)
        self._coi = first - 1
        self._coi_limit = last

    def _next_coi(self) -> int:
        if self._coi_state_path is not None:
            if self._coi >= self._coi_limit:
                self._reserve_more_coi()
            self._coi += 1
            return self._coi_prefix | self._coi
        self._coi = (self._coi + 1) & COI_COUNTER_MASK
        if self._coi == 0:
            self._coi = 1
        return self._coi_prefix | self._coi

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> OrderResult:
        """Market order with avg-price protection; settle via account ws."""
        assert self.signer is not None
        from lighter import SignerClient
        try:
            coi = self._next_coi()
            while (self.orders_feed is not None
                   and self.orders_feed.terminal(coi) is not None):
                log.warning(
                    "[%s] skipping client_order_index %d because a prior "
                    "terminal result is already cached", self.name, coi)
                coi = self._next_coi()
        except Exception as exc:
            return OrderResult.send_failed(
                "client_order_index allocation failed: "
                f"{type(exc).__name__}: {exc}")
        fut = self.orders_feed.watch(coi) if self.orders_feed else None
        base_amount = int(round(qty * 10 ** self.size_decimals))
        price = int(round(limit_px * 10 ** self.price_decimals))
        try:
            _tx, resp, err = await asyncio.wait_for(
                self.signer.create_order(
                    market_index=self.market_id,
                    client_order_index=coi,
                    base_amount=base_amount,
                    price=price,
                    is_ask=not is_buy,
                    order_type=SignerClient.ORDER_TYPE_MARKET,
                    time_in_force=(
                        SignerClient
                        .ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL),
                    reduce_only=reduce_only,
                    order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
                ),
                timeout=self.settle_timeout,
            )
        except asyncio.TimeoutError:
            if fut is not None:
                self.orders_feed.unwatch(coi)
                self.orders_feed.retain(coi)
            log.error("[%s] order submission timed out for coi %d after %.1fs",
                      self.name, coi, self.settle_timeout)
            return OrderResult.unknown(
                "order submission timed out", order_ref=str(coi))
        except Exception as e:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = f"{type(e).__name__}: {e}"
            if getattr(e, "status", None) == 429 or "(429)" in str(e):
                msg = "RATE_LIMITED: " + msg
                return OrderResult.send_failed(msg)
            if fut is not None:
                self.orders_feed.retain(coi)
            return OrderResult.unknown(
                "order submission error", msg, order_ref=str(coi))
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = str(err) if err is not None else \
                f"tx rejected code={resp.code} msg={getattr(resp, 'message', None)}"
            if (getattr(resp, "code", None) == 429
                    or "rate limit" in msg.lower()
                    or "too many requests" in msg.lower()):
                msg = "RATE_LIMITED: " + msg
            return OrderResult.send_failed(msg)
        if fut is None:
            return OrderResult.unknown(
                "sent-unconfirmed", order_ref=str(coi))
        try:
            info = await asyncio.wait_for(fut, timeout=self.settle_timeout)
            return OrderResult(
                status=info["status"],
                filled_base=info["filled_base"],
                avg_px=info.get("avg_px"),
                order_ref=str(coi),
            )
        except asyncio.TimeoutError:
            self.orders_feed.unwatch(coi)
            self.orders_feed.retain(coi)
            log.warning("[%s] no settle confirmation for coi %d in %.1fs",
                        self.name, coi, self.settle_timeout)
            return OrderResult.unknown("timeout", order_ref=str(coi))
        except Exception as exc:
            self.orders_feed.unwatch(coi)
            self.orders_feed.retain(coi)
            log.error("[%s] invalid terminal result for coi %d: %r",
                      self.name, coi, exc)
            return OrderResult.unknown(
                "invalid-terminal-result", repr(exc), order_ref=str(coi))

    async def resolve_order(self, order_ref: str) -> Optional[OrderResult]:
        if self.orders_feed is None:
            return None
        try:
            coi = int(order_ref)
        except (TypeError, ValueError):
            return None
        info = self.orders_feed.terminal(coi)
        if info is None:
            c = self.conf.lighter_creds
            if (self.signer is None or c is None
                    or c.account_index is None or c.api_key_index is None):
                return None
            auth, err = self.signer.create_auth_token_with_expiry(
                api_key_index=c.api_key_index)
            if err is not None:
                raise RuntimeError(
                    f"[{self.name}] order lookup auth token: {err}")
            data = await self._get(
                "/api/v1/accountOrders",
                params={
                    "account_index": c.account_index,
                    "client_order_indexes": str(coi),
                },
                headers={"authorization": auth},
            )
            code = data.get("code", 200)
            if code != 200:
                raise RuntimeError(
                    f"[{self.name}] order lookup failed: code={code} "
                    f"message={data.get('message')}")
            self.orders_feed._handle_orders({
                "orders": {str(self.market_id): data.get("orders") or []},
            })
            info = self.orders_feed.terminal(coi)
            if info is None:
                return None
        result = OrderResult(
            status=info["status"],
            filled_base=info["filled_base"],
            avg_px=info.get("avg_px"),
        )
        self.orders_feed.consume_terminal(coi)
        return result

    # -------------------------------------------------------------- accounts

    async def _account(self) -> Optional[dict]:
        c = self.conf.lighter_creds
        if c is None or c.account_index is None:
            return None
        data = await self._get("/api/v1/account",
                               params={"by": "index",
                                       "value": str(c.account_index)})
        for acct in data.get("accounts") or []:
            return acct
        return None

    async def fetch_equity(self):
        acct = await self._account()
        if acct is None:
            return None
        return (float(acct.get("total_asset_value") or 0.0),
                float(acct.get("available_balance") or 0.0))

    async def fetch_position(self) -> float:
        acct = await self._account()
        if acct is None:
            raise RuntimeError(f"[{self.name}] account not found")
        for p in acct.get("positions") or []:
            if int(p.get("market_id", -1)) == self.market_id:
                return float(p.get("sign") or 1.0) * float(p.get("position") or 0.0)
        return 0.0

    async def close(self) -> None:
        if self.signer is not None:
            await self.signer.api_client.close()
