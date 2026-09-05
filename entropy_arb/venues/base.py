"""Structural contract implemented by every trading venue adapter."""
from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, Protocol, runtime_checkable

from ..book import OrderBook
from ..config import VenueConf
from ..models import OrderResult


@runtime_checkable
class VenueAdapter(Protocol):
    kind: str
    conf: VenueConf
    key: str
    name: str
    book: OrderBook
    position: float
    cash: float
    volume_usd: float
    equity: Optional[float]
    free: Optional[float]
    start_equity: Optional[float]
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    last_traded_ts: float
    size_decimals: int
    min_base: float
    min_quote: float

    async def load_market(self) -> None: ...

    def init_signer(self) -> None: ...

    def configure_peer(self, other: "VenueAdapter") -> None: ...

    def start_tasks(
            self, stop: asyncio.Event, notify: Callable[..., None],
            live: bool) -> List[asyncio.Task]: ...

    def ready_to_trade(self) -> bool: ...

    async def warm_http(self) -> None: ...

    def px_round(self, px: float, round_up: bool) -> float: ...

    async def send_taker(
            self, *, is_buy: bool, qty: float, limit_px: float,
            reduce_only: bool = False) -> OrderResult: ...

    async def resolve_order(self, order_ref: str) -> Optional[OrderResult]:
        """Return a terminal result, or None while the order is still unknown."""
        ...

    async def fetch_equity(self): ...

    async def fetch_position(self) -> float: ...

    async def close(self) -> None: ...
