"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one CSV row per minute.
This is the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (entropy_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of Entropy over the hedge venue;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (entropy_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-entropy/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / entropy_ask - 1) * 1e4
                 the executable premium for BUY-entropy/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
`samples` says how many of the ~60 seconds qualified.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .book import OrderBook, plan_arb

log = logging.getLogger("recorder")

HEADER = ["minute_ts", "time_utc", "symbol", "entropy_dex", "hedge_venue",
          "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
          "premium_open_bps", "premium_high_bps", "premium_low_bps",
          "premium_close_bps", "premium_mean_bps", "premium_std_bps",
          "sell_edge_mean_bps", "sell_edge_max_bps",
          "buy_edge_mean_bps", "buy_edge_max_bps", "samples"]

SIGNAL_HEADER = [
    "ts_ms", "time_utc", "symbol", "entropy_dex", "hedge_venue",
    "event_id", "event", "direction",
    "elapsed_ms", "end_reason", "entropy_bid", "entropy_ask",
    "hedge_bid", "hedge_ask", "entropy_book_age_ms",
    "hedge_book_age_ms", "book_update_skew_ms", "top_edge_bps",
    "net_threshold_bps", "total_fee_bps", "plan_status", "qty",
    "buy_limit", "sell_limit", "planned_notional_usd",
    "crossable_notional_usd", "buy_depth_slippage_bps",
    "sell_depth_slippage_bps", "leg_slippage_limit_bps",
    "expected_edge_usd",
]


@dataclass
class _SignalState:
    event_id: str
    started_at: float
    last_written_at: float


def _next_archive_path(path: str) -> str:
    candidate = path + ".old"
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{path}.old.{suffix}"
        suffix += 1
    return candidate


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask")

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0

    def add(self, e_bid: float, e_ask: float, h_bid: float, h_ask: float) -> None:
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        prem = (e_mid / h_mid - 1.0) * 1e4
        sell_edge = (e_bid / h_ask - 1.0) * 1e4
        buy_edge = (h_bid / e_ask - 1.0) * 1e4
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        self.s_max = max(self.s_max, sell_edge)
        self.b_sum += buy_edge
        self.b_max = max(self.b_max, buy_edge)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask

    def row(self, symbol: str, entropy_dex: str,
            hedge_venue: str) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        return [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                symbol, entropy_dex, hedge_venue,
                f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
                f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
                f"{self.p_open:.3f}", f"{self.p_high:.3f}",
                f"{self.p_low:.3f}", f"{self.p_close:.3f}",
                f"{mean:.3f}", f"{math.sqrt(var):.3f}",
                f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
                f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
                self.n]


class MinuteRecorder:
    def __init__(self, path: str, entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float = 1.0, *,
                 symbol: str = "", entropy_dex: str = "",
                 hedge_venue: str = "") -> None:
        self.path = path
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.symbol = symbol
        self.entropy_dex = entropy_dex
        self.hedge_venue = hedge_venue
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            # never append rows under a different schema's header
            with open(self.path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.path, self.path)
                    os.replace(self.path, self.path + ".old")
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording 1-minute orderbook data -> %s", self.path)

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row(
            self.symbol, self.entropy_dex, self.hedge_venue))
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.entropy_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bid, e_ask = self.entropy_book.best_bid(), self.entropy_book.best_ask()
        h_bid, h_ask = self.hedge_book.best_bid(), self.hedge_book.best_ask()
        if None in (e_bid, e_ask, h_bid, h_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute)
        self._agg.add(e_bid, e_ask, h_bid, h_ask)

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)


class SignalRecorder:
    """Record fee-aware signal lifecycles without placing orders."""

    def __init__(self, path: str, entropy, hedge, *, midline_bps: float,
                 upper_bps: float, lower_bps: float, take_fraction: float,
                 max_order_notional: float, min_base: float,
                 min_notional: float, size_step: float,
                 leg_slippage_bps: float, staleness_sec: float,
                 symbol: str, entropy_dex: str, hedge_venue: str,
                 sample_sec: float = 1.0) -> None:
        self.path = path
        self.entropy = entropy
        self.hedge = hedge
        self.midline_bps = midline_bps
        self.upper_bps = upper_bps
        self.lower_bps = lower_bps
        self.take_fraction = take_fraction
        self.max_order_notional = max_order_notional
        self.min_base = min_base
        self.min_notional = min_notional
        self.size_step = size_step
        self.leg_slippage_bps = leg_slippage_bps
        self.staleness_sec = staleness_sec
        self.sample_sec = sample_sec
        self.symbol = symbol
        self.entropy_dex = entropy_dex
        self.hedge_venue = hedge_venue
        self.rows_written = 0
        self._states = {"sell_entropy": None, "buy_entropy": None}
        self._event_seq = {"sell_entropy": 0, "buy_entropy": 0}
        self._run_id = uuid.uuid4().hex
        self._pending_rows = deque()
        self._fh = None
        self._writer = None
        self._closed = False
        self._serialization_failed = False

    def _open(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, encoding="utf-8") as existing:
                existing_header = existing.readline().strip()
            if existing_header != ",".join(SIGNAL_HEADER):
                old_path = _next_archive_path(self.path)
                log.warning("%s has an old header — rotated to %s",
                            self.path, old_path)
                os.replace(self.path, old_path)
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=SIGNAL_HEADER)
        if new:
            self._writer.writeheader()
            self._fh.flush()
        log.info("recording signal lifecycles -> %s", self.path)

    def _books_status(self, now: float) -> Optional[str]:
        books = (self.entropy.book, self.hedge.book)
        if any(not book.ready for book in books):
            return "book_not_ready"
        if any(book.best_bid() is None or book.best_ask() is None
               for book in books):
            return "empty_book"
        if any(now - book.alive_ts > self.staleness_sec
               for book in books):
            return "stale_book"
        return None

    def _direction(self, direction: str):
        if direction == "sell_entropy":
            return (self.hedge, self.entropy,
                    self.midline_bps + self.upper_bps)
        return (self.entropy, self.hedge,
                self.lower_bps - self.midline_bps)

    def _snapshot(self, direction: str, now: float) -> dict:
        buy, sell, threshold = self._direction(direction)
        e_book, h_book = self.entropy.book, self.hedge.book
        e_bid, e_ask = e_book.best_bid(), e_book.best_ask()
        h_bid, h_ask = h_book.best_bid(), h_book.best_ask()
        buy_ask, sell_bid = buy.book.best_ask(), sell.book.best_bid()
        top_edge = ""
        if buy_ask is not None and sell_bid is not None:
            top_edge = (sell_bid / buy_ask - 1.0) * 1e4
        invalid = self._books_status(now)
        plan = None
        if invalid is None:
            plan, plan_status = plan_arb(
                buy.book, sell.book,
                threshold_bps=threshold,
                buy_fee_bps=buy.fee_bps,
                sell_fee_bps=sell.fee_bps,
                take_fraction=self.take_fraction,
                cap_notional=self.max_order_notional,
                min_base=self.min_base,
                min_notional=self.min_notional,
                size_step=self.size_step,
            )
        else:
            plan_status = invalid
        row = {
            "entropy_bid": "" if e_bid is None else e_bid,
            "entropy_ask": "" if e_ask is None else e_ask,
            "hedge_bid": "" if h_bid is None else h_bid,
            "hedge_ask": "" if h_ask is None else h_ask,
            "entropy_book_age_ms": (
                max((now - e_book.last_update_ts) * 1000.0, 0.0)
                if e_book.last_update_ts else ""
            ),
            "hedge_book_age_ms": (
                max((now - h_book.last_update_ts) * 1000.0, 0.0)
                if h_book.last_update_ts else ""
            ),
            "book_update_skew_ms": (
                abs(e_book.last_update_ts - h_book.last_update_ts) * 1000.0
                if e_book.last_update_ts and h_book.last_update_ts else ""
            ),
            "top_edge_bps": top_edge,
            "net_threshold_bps": threshold,
            "total_fee_bps": buy.fee_bps + sell.fee_bps,
            "plan_status": plan_status,
            "leg_slippage_limit_bps": self.leg_slippage_bps,
        }
        if plan is not None:
            row.update({
                "qty": plan.qty,
                "buy_limit": plan.buy_limit,
                "sell_limit": plan.sell_limit,
                "planned_notional_usd": plan.buy_notional,
                "crossable_notional_usd": plan.q_max_notional,
                "buy_depth_slippage_bps": (
                    plan.buy_limit / buy_ask - 1.0) * 1e4,
                "sell_depth_slippage_bps": (
                    sell_bid / plan.sell_limit - 1.0) * 1e4,
                "expected_edge_usd": plan.exp_edge_usd,
            })
        return row

    def _qualifies(self, direction: str, now: float) -> tuple[bool, str]:
        invalid = self._books_status(now)
        if invalid is not None:
            return False, invalid
        buy, sell, threshold_bps = self._direction(direction)
        buy_ask = buy.book.best_ask()
        sell_bid = sell.book.best_bid()
        qualifies = sell_bid * (1.0 - sell.fee_bps / 1e4) >= (
            buy_ask * (1.0 + buy.fee_bps / 1e4)
            * (1.0 + threshold_bps / 1e4)
        )
        return qualifies, "" if qualifies else "edge_below_threshold"

    def _queue_row(self, event: str, direction: str, state: _SignalState,
                   now: float, end_reason: str = "") -> None:
        row = {name: "" for name in SIGNAL_HEADER}
        row.update(self._snapshot(direction, now))
        row.update({
            "ts_ms": int(now * 1000),
            "time_utc": datetime.fromtimestamp(now, tz=timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "symbol": self.symbol,
            "entropy_dex": self.entropy_dex,
            "hedge_venue": self.hedge_venue,
            "event_id": state.event_id,
            "event": event,
            "direction": direction,
            "elapsed_ms": int(round((now - state.started_at) * 1000)),
            "end_reason": end_reason,
        })
        self._pending_rows.append(row)

    def _flush_pending(self) -> None:
        if not self._pending_rows:
            return
        if self._writer is None:
            self._open()
        while self._pending_rows:
            row = self._pending_rows.popleft()
            try:
                self._writer.writerow(row)
            except BaseException:
                self._serialization_failed = True
                raise
            self._fh.flush()
            self.rows_written += 1

    def observe(self, now: Optional[float] = None, *,
                flush: bool = True) -> None:
        now = time.time() if now is None else now
        for direction in self._states:
            active = self._states[direction]
            qualifies, end_reason = self._qualifies(direction, now)
            if qualifies and active is None:
                self._event_seq[direction] += 1
                active = _SignalState(
                    event_id=(f"{direction}-{int(now * 1000)}-"
                              f"{self._run_id}-"
                              f"{self._event_seq[direction]}"),
                    started_at=now,
                    last_written_at=now,
                )
                self._queue_row("start", direction, active, now)
                self._states[direction] = active
            elif (qualifies and active is not None
                  and now - active.last_written_at >= self.sample_sec):
                self._queue_row("sample", direction, active, now)
                active.last_written_at = now
            elif not qualifies and active is not None:
                self._queue_row(
                    "end", direction, active, now, end_reason)
                self._states[direction] = None
        if flush:
            self._flush_pending()

    def close(self, now: Optional[float] = None) -> None:
        if self._closed:
            return
        primary_error = None
        try:
            if not self._serialization_failed:
                now = time.time() if now is None else now
                for direction, active in self._states.items():
                    if active is not None:
                        self._queue_row(
                            "end", direction, active, now, "shutdown")
                        self._states[direction] = None
                self._flush_pending()
        except BaseException as exc:
            primary_error = exc
        try:
            if self._fh is not None:
                self._fh.close()
        except BaseException as exc:
            if primary_error is None:
                primary_error = exc
            else:
                log.exception(
                    "signal file also failed while closing; preserving the "
                    "original error")
        finally:
            self._fh = self._writer = None
            self._closed = True
        if primary_error is not None:
            raise primary_error

    def _seconds_until_next_sample(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        active = [state for state in self._states.values()
                  if state is not None]
        if not active:
            return 3600.0
        due = min(state.last_written_at + self.sample_sec
                  for state in active)
        return max(due - now, 0.001)

    async def run(self, stop: asyncio.Event,
                  update_evt: asyncio.Event) -> None:
        primary_error = None
        try:
            try:
                while not stop.is_set():
                    update_evt.clear()
                    self.observe()
                    if stop.is_set():
                        break
                    timeout = self._seconds_until_next_sample()
                    if update_evt.is_set():
                        continue
                    try:
                        await asyncio.wait_for(
                            update_evt.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                primary_error = exc
                log.exception("signal recorder failed")
                stop.set()
                update_evt.set()
                raise
        finally:
            try:
                self.close()
            except Exception:
                stop.set()
                update_evt.set()
                if primary_error is None:
                    log.exception("signal recorder failed while closing")
                    raise
                log.exception(
                    "signal recorder also failed while closing; preserving "
                    "the original error")
            log.info("signal recorder stopped — %d row(s) written to %s",
                     self.rows_written, self.path)
