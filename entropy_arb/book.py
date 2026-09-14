"""Order book state and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Freshness is connection-based (any inbound ws frame touches alive_ts): a quiet
market is not stale, only a dead feed is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.last_update_mono = 0.0
        self.alive_ts = 0.0
        self.alive_mono = 0.0

    def touch(self) -> None:
        self.alive_ts = time.time()
        self.alive_mono = time.monotonic()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ready = True
        self.last_update_ts = time.time()
        self.last_update_mono = time.monotonic()
        self.touch()

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list) -> None:
        self.bids = {float(l["px"]): float(l["sz"])
                     for l in levels[0] if float(l["sz"]) > 0}
        self.asks = {float(l["px"]): float(l["sz"])
                     for l in levels[1] if float(l["sz"]) > 0}
        self.ready = True
        self.last_update_ts = time.time()
        self.last_update_mono = time.monotonic()
        self.touch()

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def is_fresh(self, max_age_sec: float) -> bool:
        return self.ready and bool(self.bids) and bool(self.asks) and (
            time.monotonic() - self.alive_mono <= max_age_sec)


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if b_px * (1.0 - sell_fee) < a_px * (1.0 + buy_fee) * (1.0 + threshold):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


def quantity_within_notional(levels: List[Level], cap_notional: float) -> float:
    """Return the maximum base quantity whose walked notional fits the cap."""
    remaining = cap_notional
    qty = 0.0
    for px, size in levels:
        if remaining <= 0.0:
            break
        take = min(size, remaining / px)
        qty += take
        remaining -= take * px
        if take < size:
            break
    return qty


def _notional_within_cap(value: float, cap: float) -> bool:
    return value <= cap + max(1e-9, abs(cap) * 1e-12)


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(
        q_max * take_fraction,
        quantity_within_notional(asks, cap_notional),
        quantity_within_notional(bids, cap_notional),
    )
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        target = floor_step(target - size_step, size_step)
        if target < min_base:
            return None, "below_min_base"
        buy_limit, buy_notional = walk_depth(asks, target)
        sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        raise ArithmeticError("planned leg notional exceeds cap")
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"


@dataclass(frozen=True)
class ConvergencePlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    open_depth_slippage_bps: float
    convergence_bps: float
    projected_net_bps: float


@dataclass(frozen=True)
class MatchedClosePlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    buy_depth_slippage_bps: float
    sell_depth_slippage_bps: float


def _validate_plan_number(name: str, value: float, *,
                          positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    if not positive and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _directional_signed_residual(
        direction: str, buy_px: float, sell_px: float,
        reference_basis_bps: float) -> float:
    if direction == "sell_entropy":
        signed_premium = (sell_px / buy_px - 1.0) * 1e4
    elif direction == "buy_entropy":
        signed_premium = (buy_px / sell_px - 1.0) * 1e4
    else:
        raise ValueError(f"unknown direction {direction!r}")
    return signed_premium - reference_basis_bps


def _convergence_space(direction: str, signed_residual_bps: float,
                       exit_residual_bps: float) -> float:
    if direction == "sell_entropy":
        return signed_residual_bps - exit_residual_bps
    if direction == "buy_entropy":
        return exit_residual_bps - signed_residual_bps
    raise ValueError(f"unknown direction {direction!r}")


def _bounded_common_depth(asks: List[Level], bids: List[Level], *,
                          buy_slippage_bps: float,
                          sell_slippage_bps: float) -> float:
    best_ask = asks[0][0]
    best_bid = bids[0][0]
    qty = 0.0
    i = j = 0
    ask_remaining = bid_remaining = 0.0
    ask_px = bid_px = 0.0
    while True:
        if ask_remaining <= 0:
            if i >= len(asks):
                break
            ask_px, ask_remaining = asks[i]
            i += 1
        if bid_remaining <= 0:
            if j >= len(bids):
                break
            bid_px, bid_remaining = bids[j]
            j += 1
        buy_slip = (ask_px / best_ask - 1.0) * 1e4
        sell_slip = (best_bid / bid_px - 1.0) * 1e4
        if (buy_slip > buy_slippage_bps + 1e-9
                or sell_slip > sell_slippage_bps + 1e-9):
            break
        take = min(ask_remaining, bid_remaining)
        qty += take
        ask_remaining -= take
        bid_remaining -= take
    return qty


def plan_convergence_trade(
        buy_book: OrderBook, sell_book: OrderBook, *, direction: str,
        reference_basis_bps: float, exit_residual_bps: float,
        round_trip_fee_bps: float, close_slippage_reserve_bps: float,
        min_expected_profit_bps: float, buy_slippage_budget_bps: float,
        sell_slippage_budget_bps: float, take_fraction: float,
        cap_notional: float, min_base: float, min_notional: float,
        size_step: float):
    """Plan a matched opening slice against a frozen residual exit target."""
    if direction not in {"sell_entropy", "buy_entropy"}:
        raise ValueError(f"unknown direction {direction!r}")
    if (isinstance(reference_basis_bps, bool)
            or not isinstance(reference_basis_bps, (int, float))):
        raise ValueError("reference_basis_bps must be a number")
    basis = float(reference_basis_bps)
    if not math.isfinite(basis):
        raise ValueError("reference_basis_bps must be finite")
    exit_residual = float(exit_residual_bps)
    if not math.isfinite(exit_residual):
        raise ValueError("exit_residual_bps must be finite")
    fees = _validate_plan_number("round_trip_fee_bps", round_trip_fee_bps)
    close_reserve = _validate_plan_number(
        "close_slippage_reserve_bps", close_slippage_reserve_bps)
    min_profit = _validate_plan_number(
        "min_expected_profit_bps", min_expected_profit_bps)
    buy_budget = _validate_plan_number(
        "buy_slippage_budget_bps", buy_slippage_budget_bps)
    sell_budget = _validate_plan_number(
        "sell_slippage_budget_bps", sell_slippage_budget_bps)
    take_fraction = _validate_plan_number(
        "take_fraction", take_fraction, positive=True)
    if take_fraction > 1:
        raise ValueError("take_fraction must not exceed 1")
    cap_notional = _validate_plan_number(
        "cap_notional", cap_notional, positive=True)
    min_base = _validate_plan_number("min_base", min_base)
    min_notional = _validate_plan_number("min_notional", min_notional)
    size_step = _validate_plan_number("size_step", size_step, positive=True)

    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    best_ask = asks[0][0]
    best_bid = bids[0][0]
    top_residual = _directional_signed_residual(
        direction, best_ask, best_bid, basis)
    convergence = _convergence_space(
        direction, top_residual, exit_residual)
    top_projected = convergence - fees - close_reserve
    if top_projected < min_profit:
        return None, "insufficient_net_edge"

    q_max = 0.0
    i = j = 0
    ask_remaining = bid_remaining = 0.0
    ask_px = bid_px = 0.0
    while True:
        if ask_remaining <= 0:
            if i >= len(asks):
                break
            ask_px, ask_remaining = asks[i]
            i += 1
        if bid_remaining <= 0:
            if j >= len(bids):
                break
            bid_px, bid_remaining = bids[j]
            j += 1
        buy_slip = (ask_px / best_ask - 1.0) * 1e4
        sell_slip = (best_bid / bid_px - 1.0) * 1e4
        projected = (convergence - fees - buy_slip - sell_slip
                     - close_reserve)
        if (buy_slip > buy_budget + 1e-9
                or sell_slip > sell_budget + 1e-9
                or projected < min_profit):
            break
        take = min(ask_remaining, bid_remaining)
        q_max += take
        ask_remaining -= take
        bid_remaining -= take
    if q_max <= 0:
        return None, "depth_slippage_exceeded"

    target = min(
        q_max * take_fraction,
        quantity_within_notional(asks, cap_notional),
        quantity_within_notional(bids, cap_notional),
    )
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        target = floor_step(target - size_step, size_step)
        if target < min_base:
            return None, "below_min_base"
        buy_limit, buy_notional = walk_depth(asks, target)
        sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        raise ArithmeticError("planned convergence leg notional exceeds cap")
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    buy_slip = (buy_limit / best_ask - 1.0) * 1e4
    sell_slip = (best_bid / sell_limit - 1.0) * 1e4
    projected = convergence - fees - buy_slip - sell_slip - close_reserve
    return ConvergencePlan(
        qty=target,
        buy_limit=buy_limit,
        sell_limit=sell_limit,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        q_max=q_max,
        open_depth_slippage_bps=buy_slip + sell_slip,
        convergence_bps=convergence,
        projected_net_bps=projected,
    ), "ok"


def plan_matched_close(
        buy_book: OrderBook, sell_book: OrderBook, *, max_qty: float,
        cap_notional: float, buy_slippage_bps: float,
        sell_slippage_bps: float, min_base: float, min_notional: float,
        size_step: float):
    """Plan a risk-reducing equal-base close inside fixed price bounds."""
    max_qty = _validate_plan_number("max_qty", max_qty, positive=True)
    cap_notional = _validate_plan_number(
        "cap_notional", cap_notional, positive=True)
    buy_budget = _validate_plan_number(
        "buy_slippage_bps", buy_slippage_bps)
    sell_budget = _validate_plan_number(
        "sell_slippage_bps", sell_slippage_bps)
    min_base = _validate_plan_number("min_base", min_base)
    min_notional = _validate_plan_number("min_notional", min_notional)
    size_step = _validate_plan_number("size_step", size_step, positive=True)
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    q_max = _bounded_common_depth(
        asks, bids, buy_slippage_bps=buy_budget,
        sell_slippage_bps=sell_budget)
    target = min(
        q_max,
        max_qty,
        quantity_within_notional(asks, cap_notional),
        quantity_within_notional(bids, cap_notional),
    )
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        raise ArithmeticError("planned close leg notional exceeds cap")
    return MatchedClosePlan(
        qty=target,
        buy_limit=buy_limit,
        sell_limit=sell_limit,
        buy_notional=buy_notional,
        sell_notional=sell_notional,
        buy_depth_slippage_bps=(buy_limit / asks[0][0] - 1.0) * 1e4,
        sell_depth_slippage_bps=(bids[0][0] / sell_limit - 1.0) * 1e4,
    ), "ok"
