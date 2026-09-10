"""Hyperliquid HIP-3 dex venue adapter (Entropy = dex "io", trade.xyz = "xyz").

Market metadata, account state and order posting use Hyperliquid's public
/info and /exchange REST endpoints via plain aiohttp; the book comes from the
OFFICIAL websocket (see feeds.HLBookFeed). Trading lazily imports the
official `hyperliquid-python-sdk` signing helpers + eth_account —
--record-only data collection needs neither.

IOC limit orders settle synchronously in the /exchange response. HIP-3 orders
omit cloid because the exchange currently rejects that combination; unknown
outcomes (timeout/HTTP 408/5xx) are returned unresolved so the engine stops for
manual position verification instead of automatically repairing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import VenueConf
from .feeds import HLBookFeed
from .models import OrderResult
from .reference import InvalidReference, ReferenceState, ReferenceUpdate

log = logging.getLogger("hl")

INFO_TIMEOUT = 10.0


def parse_hl_rest_asset_ctx(ctx: dict) -> ReferenceUpdate:
    try:
        funding = ctx.get("funding")
        return ReferenceUpdate(
            oracle_px=(None if ctx.get("oraclePx") is None
                       else float(ctx["oraclePx"])),
            mark_px=(None if ctx.get("markPx") is None
                     else float(ctx["markPx"])),
            funding_current_bps_per_hour=(
                None if funding is None else float(funding) * 1e4),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise InvalidReference(f"invalid Hyperliquid REST asset context: {exc}") \
            from exc


class NonceAllocator:
    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, int(time.time() * 1000))
        return self._last


class HLAccount:
    def __init__(self, private_key: str, account_address: Optional[str],
                 api_url: str) -> None:
        from eth_account import Account
        self.wallet = Account.from_key(private_key)
        self.query_address = (account_address or self.wallet.address).lower()
        self.is_mainnet = api_url == "https://api.hyperliquid.xyz"
        self.nonces = NonceAllocator()

    def describe(self) -> str:
        s = f"signer={self.wallet.address} account={self.query_address}"
        if self.wallet.address.lower() != self.query_address:
            s += " (agent mode)"
        return s


class HLVenue:
    kind = "hl"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession, settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = api_url
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.reference = ReferenceState()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.include_core_equity = True  # cleared when two venues share one account
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.account: Optional[HLAccount] = None
        self.coin = ""
        self.asset_id = -1
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 10.0
        self._signing = None      # lazy hyperliquid-sdk signing module

    async def _info(self, payload: dict):
        async with self.session.post(
                self.api_url + "/info", json=payload,
                timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def load_market(self) -> None:
        dexs = await self._info({"type": "perpDexs"})
        names = [(d or {}).get("name", "") for d in dexs]
        if self.conf.hl_dex not in names:
            raise RuntimeError(f"[{self.name}] dex '{self.conf.hl_dex}' not "
                               f"found on Hyperliquid (available: "
                               f"{[n for n in names if n][:20]}...)")
        dex_index = names.index(self.conf.hl_dex)
        meta = await self._info({"type": "meta", "dex": self.conf.hl_dex})
        want = f"{self.conf.hl_dex}:{self.conf.symbol}"
        for idx, a in enumerate(meta["universe"]):
            if a["name"] not in (want, self.conf.symbol):
                continue
            if a.get("isDelisted"):
                raise RuntimeError(f"[{self.name}] {a['name']} is delisted")
            self.coin = a["name"]
            self.asset_id = 110000 + (dex_index - 1) * 10000 + idx
            self.size_decimals = int(a["szDecimals"])
            self.min_base = 10 ** -self.size_decimals
            log.info("[%s] %s asset_id=%d szDecimals=%d maxLev=%sx %s",
                     self.name, self.coin, self.asset_id, self.size_decimals,
                     a.get("maxLeverage"),
                     "isolated-only" if a.get("onlyIsolated") else "")
            try:
                await self.refresh_reference_rest()
            except (aiohttp.ClientError, asyncio.TimeoutError,
                    InvalidReference) as exc:
                log.warning("[%s] initial reference REST failed: %s",
                            self.name, exc)
            return
        raise RuntimeError(f"[{self.name}] {want} not found")

    async def refresh_reference_rest(self) -> bool:
        websocket_generation = self.reference.websocket_generation
        try:
            data = await self._info({
                "type": "metaAndAssetCtxs", "dex": self.conf.hl_dex})
            meta, contexts = data
            index = next(
                idx for idx, asset in enumerate(meta["universe"])
                if asset.get("name") == self.coin)
            update = parse_hl_rest_asset_ctx(contexts[index])
        except (KeyError, IndexError, StopIteration, TypeError,
                ValueError) as exc:
            raise InvalidReference(
                f"invalid Hyperliquid REST reference payload for "
                f"{self.coin}: {exc}") from exc
        return self.reference.apply_rest_if_ws_unchanged(
            update,
            expected_websocket_generation=websocket_generation,
        )

    def init_signer(self) -> None:
        c = self.conf.hl_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from hyperliquid.utils import signing as hl_signing
        except ImportError as e:
            raise RuntimeError(
                "live trading on Hyperliquid needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(hyperliquid-python-sdk)") from e
        self._signing = hl_signing
        self.account = HLAccount(c.private_key, c.account_address, self.api_url)
        log.info("[%s] %s", self.name, self.account.describe())

    def share_nonces_with(self, other: "HLVenue") -> None:
        """One signer address must use one nonce sequence."""
        if (self.account and other.account and
                self.account.wallet.address == other.account.wallet.address):
            other.account.nonces = self.account.nonces
            log.info("[%s]/[%s] same signer — shared nonce allocator",
                     self.name, other.name)

    def configure_peer(self, other) -> None:
        """Configure shared Hyperliquid signer and account accounting."""
        if not isinstance(other, HLVenue):
            return
        self.share_nonces_with(other)
        address = self._query_address()
        if address and address == other._query_address():
            other.include_core_equity = False

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        def book_notify() -> None:
            notify("book", self.key)

        return [asyncio.create_task(
            HLBookFeed(self.name, self.ws_url, self.coin, self.book,
                       book_notify, self.reference).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._info({"type": "exchangeStatus"})
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        max_dec = max(0, 6 - self.size_decimals)
        sig_dec = 4 - math.floor(math.log10(px))
        dec = max(0, min(max_dec, sig_dec))
        f = 10.0 ** dec
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> OrderResult:
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        order_req = {"coin": self.coin, "is_buy": is_buy, "sz": round(qty, 8),
                     "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Ioc"}},
                     "reduce_only": reduce_only}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            action = s.order_wires_to_order_action([wire])
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            return OrderResult.send_failed(f"signing failed: {e!r}")

        body, err, unresolved = await self._post_exchange(payload)
        if err is not None:
            return OrderResult.send_failed(err)
        if unresolved:
            return OrderResult.unknown("exchange response unknown")
        return self._parse(body)

    async def _post_exchange(self, payload: dict):
        try:
            async with self.session.post(
                    self.api_url + "/exchange", json=payload,
                    timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 408 or r.status >= 500:
                    return None, None, True
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                return json.loads(text), None, False
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, True

    @staticmethod
    def _parse(body: dict) -> OrderResult:
        def fail(msg: str) -> OrderResult:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return OrderResult.send_failed(msg)

        if body.get("status") == "err":
            return fail(str(body.get("response")))
        if body.get("status") != "ok":
            return OrderResult.unknown(
                "unexpected-response",
                f"unexpected response: {str(body)[:200]}")
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return OrderResult.unknown(
                "malformed-response",
                f"malformed response: {str(body)[:200]}")
        if "filled" in st:
            fill = st["filled"]
            filled_base = float(fill.get("totalSz") or 0.0)
            if filled_base > 0 and not fill.get("avgPx"):
                return OrderResult.unknown(
                    "malformed-response",
                    "positive fill is missing its average price")
            return OrderResult(
                status="filled",
                filled_base=filled_base,
                avg_px=(float(fill["avgPx"])
                        if fill.get("avgPx") else None),
            )
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                return OrderResult(status="canceled")
            return fail(msg)
        if "resting" in st:
            return OrderResult.unknown("resting?")
        return OrderResult.unknown(
            "unknown-status", f"unknown status: {str(st)[:150]}")

    async def resolve_order(self, order_ref: str) -> Optional[OrderResult]:
        # HIP-3 market orders omit a client order id, so ambiguous HTTP
        # responses cannot be correlated with a later terminal order status.
        return None

    # -------------------------------------------------------------- accounts

    def _query_address(self):
        if self.account is not None:
            return self.account.query_address
        c = self.conf.hl_creds
        return c.account_address.lower() if c and c.account_address else None

    async def fetch_equity(self):
        """Unified account equity via the portfolio endpoint — the same
        Portfolio Value the HL UI shows. Falls back to summing clearinghouse
        buckets if the endpoint shape changes. When both venues share one HL
        account (include_core_equity cleared on the hedge), that venue reports
        only its dex bucket to avoid double-counting."""
        addr = self._query_address()
        if addr is None:
            return None
        if self.include_core_equity:
            try:
                p = await self._info({"type": "portfolio", "user": addr})
                for period, d in p:
                    if period == "day":
                        hist = d.get("accountValueHistory") or []
                        if hist:
                            return float(hist[-1][1]), None
            except Exception as e:
                log.debug("[%s] portfolio fetch failed, falling back: %r",
                          self.name, e)
        dexs = [self.conf.hl_dex] + ([""] if self.include_core_equity else [])
        eq = fr = 0.0
        for dex in dexs:
            st = await self._info({"type": "clearinghouseState", "user": addr,
                                   "dex": dex})
            ms = st.get("marginSummary") or {}
            eq += float(ms.get("accountValue") or 0.0)
            fr += float(st.get("withdrawable") or 0.0)
        return eq, fr

    async def fetch_position(self) -> float:
        addr = self._query_address()
        assert addr is not None
        st = await self._info({"type": "clearinghouseState", "user": addr,
                               "dex": self.conf.hl_dex})
        for ap in st.get("assetPositions") or []:
            pos = ap.get("position") or {}
            if pos.get("coin") == self.coin:
                return float(pos.get("szi") or 0.0)
        return 0.0

    async def close(self) -> None:
        pass
