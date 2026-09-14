#!/usr/bin/env python3
"""Deterministic dynamic-strategy replay from minute and signal CSV files.

Signal journals contain only top-of-book observations. Results are therefore
coverage and invariant checks, not fill-quality or realized-PnL estimates.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook, walk_depth  # noqa: E402
from entropy_arb.campaign import PositionCampaign  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.slippage import SlippageModel  # noqa: E402
from entropy_arb.strategy import (  # noqa: E402
    DynamicResidualStrategy,
    MarketIdentity,
    MarketView,
    ResidualModel,
)


LEGACY_APPROXIMATION = (
    "threshold-censored legacy top-of-book approximation")
SNAPSHOT_APPROXIMATION = "continuous top-of-book snapshot approximation"
IDENTITY_FIELDS = (
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue")
MINUTE_FIELDS = {
    "minute_ts", *IDENTITY_FIELDS,
    "entropy_reference_age_ms", "hedge_reference_age_ms",
    "reference_update_skew_ms", "residual_close_bps",
}
SIGNAL_FIELDS = {
    "ts_ms", *IDENTITY_FIELDS, "event_id", "event", "direction",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "entropy_book_age_ms", "hedge_book_age_ms",
    "entropy_oracle_px", "hedge_index_px",
    "entropy_reference_age_ms", "hedge_reference_age_ms",
    "reference_update_skew_ms", "planned_notional_usd",
    "crossable_notional_usd",
}


@dataclass(frozen=True)
class ReplayResult:
    approximation: str
    minute_rows: int
    signal_rows: int
    requested_end_ts: float
    coverage_end_ts: Optional[float]
    timeline_complete: bool
    actions: int
    timestamps_monotonic: bool
    raw_buy_coverage: float
    raw_sell_coverage: float
    residual_open_coverage: float
    campaigns_opened: int
    campaigns_completed: int
    campaigns_still_open: int
    normal_closes: int
    soft_closes: int
    hard_closes: int
    hold_p50_seconds: Optional[float]
    hold_p75_seconds: Optional[float]
    hold_p95_seconds: Optional[float]
    hold_max_seconds: Optional[float]
    max_planned_leg_notional: float
    max_accumulated_leg_notional: float
    max_slippage_budget_bps: float
    reference_gate_rejects: int
    invalid_reference_entries: int
    entries_during_unstable_gap: int
    reverse_campaigns: int


def _open_csv(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, newline="", encoding="utf-8")


def _finite(source: dict, field: str) -> Optional[float]:
    raw = source.get(field)
    if raw is None or not str(raw).strip():
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _identity(row: dict) -> MarketIdentity:
    return MarketIdentity(*(
        (row.get(field) or "").strip() for field in IDENTITY_FIELDS))


def _require_one_identity(identities: set[MarketIdentity]) -> MarketIdentity:
    if len(identities) > 1:
        raise ValueError("multiple markets found in replay inputs")
    if not identities:
        raise ValueError("replay inputs contain no market identity")
    return next(iter(identities))


def _load_minutes(path: str) -> tuple[list[dict], MarketIdentity]:
    rows = []
    identities = set()
    with _open_csv(path) as handle:
        reader = csv.DictReader(handle)
        missing = MINUTE_FIELDS - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "minute CSV missing required columns: "
                + ", ".join(sorted(missing)))
        for source in reader:
            identity = _identity(source)
            identities.add(identity)
            timestamp = _finite(source, "minute_ts")
            if timestamp is None or timestamp < 0:
                continue
            rows.append({
                "timestamp": timestamp,
                "minute": int(timestamp // 60),
                "identity": identity,
                "residual": _finite(source, "residual_close_bps"),
                "entropy_age_ms": _finite(
                    source, "entropy_reference_age_ms"),
                "hedge_age_ms": _finite(
                    source, "hedge_reference_age_ms"),
                "skew_ms": _finite(source, "reference_update_skew_ms"),
            })
    identity = _require_one_identity(identities)
    # Last fragment wins for duplicate minute closes, matching analysis.
    merged = {row["minute"]: row for row in rows}
    return sorted(merged.values(), key=lambda row: row["minute"]), identity


def _load_signals(paths: Sequence[str]) -> tuple[list[dict], MarketIdentity]:
    rows = []
    identities = set()
    seen = set()
    for path in paths:
        with _open_csv(path) as handle:
            reader = csv.DictReader(handle)
            missing = SIGNAL_FIELDS - set(reader.fieldnames or ())
            if missing:
                raise ValueError(
                    "signal CSV missing required columns: "
                    + ", ".join(sorted(missing)))
            for source in reader:
                identity = _identity(source)
                identities.add(identity)
                timestamp_ms = _finite(source, "ts_ms")
                if timestamp_ms is None or timestamp_ms < 0:
                    raise ValueError("signal timestamp must be finite")
                event = (source.get("event") or "").strip()
                direction = (source.get("direction") or "").strip()
                if event == "snapshot":
                    if direction:
                        raise ValueError("snapshot direction must be empty")
                elif event not in {"start", "sample", "end"}:
                    raise ValueError("signal event is invalid")
                elif direction not in {"buy_entropy", "sell_entropy"}:
                    raise ValueError("signal direction is invalid")
                dedupe = (
                    timestamp_ms, direction,
                    event,
                    (source.get("event_id") or "").strip(),
                )
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                row = dict(source)
                row["timestamp_ms"] = timestamp_ms
                row["identity"] = identity
                rows.append(row)
    identity = _require_one_identity(identities)
    rows.sort(key=lambda row: (
        row["timestamp_ms"], row["direction"], row.get("event_id", "")))
    return rows, identity


def _make_model(config) -> ResidualModel:
    return ResidualModel(
        window_minutes=config.strategy_window_minutes,
        min_samples=config.strategy_min_samples,
        lower_quantile=config.strategy_lower_quantile,
        upper_quantile=config.strategy_upper_quantile,
        regime_window_minutes=config.strategy_regime_window_minutes,
        recovery_minutes=config.strategy_regime_recovery_minutes,
    )


def _make_strategy(config) -> DynamicResidualStrategy:
    slippage = SlippageModel(
        bootstrap_bps=config.slippage_bootstrap_bps,
        min_bps=config.slippage_min_bps,
        safety_bps=config.slippage_safety_bps,
        hard_max_bps=config.slippage_hard_max_bps,
        min_live_samples=config.slippage_min_live_samples,
    )
    return DynamicResidualStrategy(
        slippage=slippage,
        reference_max_age_sec=(
            config.strategy_entry_reference_max_age_sec),
        reference_max_skew_sec=(
            config.strategy_entry_reference_max_skew_sec),
        exit_band_fraction=config.strategy_exit_band_fraction,
        min_exit_band_bps=config.strategy_min_exit_band_bps,
        min_expected_profit_bps=config.strategy_min_expected_profit_bps,
        soft_hold_sec=config.strategy_soft_hold_minutes * 60.0,
        hard_hold_sec=config.strategy_hard_hold_minutes * 60.0,
        hard_slippage_bps=config.slippage_hard_max_bps,
        max_edge_fraction=config.slippage_max_edge_fraction,
    )


def _book(bid: Optional[float], ask: Optional[float], notional: float,
          take_fraction: float) -> OrderBook:
    book = OrderBook()
    if (bid is None or ask is None or bid <= 0 or ask <= 0
            or notional <= 0):
        return book
    size = notional / max(bid, ask) / take_fraction
    book.apply_hl([
        [{"px": str(bid), "sz": str(size)}],
        [{"px": str(ask), "sz": str(size)}],
    ])
    return book


def _signal_market(
        row: dict, config, *,
        entry_cap_notional: Optional[float] = None,
) -> tuple[MarketView, bool]:
    values = {
        field: _finite(row, field)
        for field in (
            "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
            "entropy_book_age_ms", "hedge_book_age_ms",
            "entropy_oracle_px", "hedge_index_px",
            "entropy_reference_age_ms", "hedge_reference_age_ms",
            "reference_update_skew_ms", "planned_notional_usd",
            "crossable_notional_usd",
        )
    }
    caps = [config.max_order_notional]
    if (row.get("event") or "").strip() == "snapshot":
        value = values["crossable_notional_usd"]
        caps.append(value if value is not None and value > 0 else 0.0)
    else:
        for field in ("planned_notional_usd", "crossable_notional_usd"):
            value = values[field]
            caps.append(value if value is not None and value > 0 else 0.0)
    recorded_cap = min(caps)
    cap = recorded_cap if entry_cap_notional is None else min(
        recorded_cap, max(entry_cap_notional, 0.0))
    book_limit_ms = config.staleness_sec * 1000.0
    books_ready = (
        values["entropy_book_age_ms"] is not None
        and values["hedge_book_age_ms"] is not None
        and values["entropy_book_age_ms"] <= book_limit_ms
        and values["hedge_book_age_ms"] <= book_limit_ms
        and values["entropy_book_age_ms"] >= 0
        and values["hedge_book_age_ms"] >= 0
            and recorded_cap > 0)
    reference_valid = (
        values["entropy_oracle_px"] is not None
        and values["hedge_index_px"] is not None
        and values["entropy_oracle_px"] > 0
        and values["hedge_index_px"] > 0
        and values["entropy_reference_age_ms"] is not None
        and values["hedge_reference_age_ms"] is not None
        and values["reference_update_skew_ms"] is not None
        and values["entropy_reference_age_ms"] >= 0
        and values["hedge_reference_age_ms"] >= 0
        and values["reference_update_skew_ms"] >= 0
        and values["entropy_reference_age_ms"]
        <= config.strategy_entry_reference_max_age_sec * 1000.0
        and values["hedge_reference_age_ms"]
        <= config.strategy_entry_reference_max_age_sec * 1000.0
        and values["reference_update_skew_ms"]
        <= config.strategy_entry_reference_max_skew_sec * 1000.0)
    return MarketView(
        entropy_book=_book(
            values["entropy_bid"], values["entropy_ask"], recorded_cap,
            config.take_fraction),
        hedge_book=_book(
            values["hedge_bid"], values["hedge_ask"], recorded_cap,
            config.take_fraction),
        entropy_oracle_px=values["entropy_oracle_px"],
        hedge_index_px=values["hedge_index_px"],
        entropy_reference_age_sec=(
            None if values["entropy_reference_age_ms"] is None
            else values["entropy_reference_age_ms"] / 1000.0),
        hedge_reference_age_sec=(
            None if values["hedge_reference_age_ms"] is None
            else values["hedge_reference_age_ms"] / 1000.0),
        reference_skew_sec=(
            None if values["reference_update_skew_ms"] is None
            else values["reference_update_skew_ms"] / 1000.0),
        books_ready=books_ready,
        entropy_fee_bps=config.entropy.fee_bps,
        hedge_fee_bps=config.hedge.fee_bps,
        take_fraction=config.take_fraction,
        entry_cap_notional=cap,
        min_base=1e-12,
        min_notional=config.min_order_notional,
        size_step=1e-12,
    ), reference_valid


def _entry_headroom(config, campaign: Optional[PositionCampaign],
                    direction: str, market: MarketView) -> float:
    entropy_mid = market.entropy_book.mid()
    hedge_mid = market.hedge_book.mid()
    if entropy_mid is None or hedge_mid is None:
        return 0.0
    sign = 0.0
    quantity = 0.0
    if campaign is not None:
        sign = -1.0 if campaign.direction == "sell_entropy" else 1.0
        quantity = campaign.qty
    entropy_position = sign * quantity
    hedge_position = -sign * quantity
    if direction == "sell_entropy":
        entropy_room_base = (
            config.entropy.cap_usd / entropy_mid + entropy_position)
        hedge_room_base = (
            config.hedge.cap_usd / hedge_mid - hedge_position)
        entropy_levels = market.entropy_book.sorted_bids()
        hedge_levels = market.hedge_book.sorted_asks()
    else:
        entropy_room_base = (
            config.entropy.cap_usd / entropy_mid - entropy_position)
        hedge_room_base = (
            config.hedge.cap_usd / hedge_mid + hedge_position)
        entropy_levels = market.entropy_book.sorted_asks()
        hedge_levels = market.hedge_book.sorted_bids()

    def capacity_notional(levels, room_base: float) -> float:
        if not levels or room_base <= 0:
            return 0.0
        quantity = min(room_base, sum(size for _, size in levels))
        return walk_depth(levels, quantity)[1]

    entropy_room = capacity_notional(entropy_levels, entropy_room_base)
    hedge_room = capacity_notional(hedge_levels, hedge_room_base)
    return max(0.0, min(
        config.max_order_notional, entropy_room, hedge_room))


def _apply_shadow(
        campaign: Optional[PositionCampaign], decision,
        identity: MarketIdentity, now: float,
        config) -> Optional[PositionCampaign]:
    plan = decision.plan
    if decision.intent in {"OPEN", "ADD"}:
        if decision.direction == "sell_entropy":
            entropy_px, hedge_px = plan.sell_limit, plan.buy_limit
        else:
            entropy_px, hedge_px = plan.buy_limit, plan.sell_limit
    elif campaign.direction == "sell_entropy":
        entropy_px, hedge_px = plan.buy_limit, plan.sell_limit
    else:
        entropy_px, hedge_px = plan.sell_limit, plan.buy_limit
    fees = plan.qty * (
        entropy_px * config.entropy.fee_bps
        + hedge_px * config.hedge.fee_bps) / 1e4
    if decision.intent == "OPEN":
        return PositionCampaign(
            campaign_id=uuid.uuid4().hex,
            mode="shadow",
            identity=identity,
            direction=decision.direction,
            opened_at=now,
            qty=plan.qty,
            entropy_avg_px=entropy_px,
            hedge_avg_px=hedge_px,
            frozen_model=decision.model,
            entry_boundary_bps=decision.entry_boundary_bps,
            exit_target_bps=decision.exit_target_bps,
            fees_usd=fees,
            realized_pnl_usd=-fees,
        )
    return campaign.apply_matched_fill(
        intent=decision.intent,
        direction=campaign.direction,
        qty=plan.qty,
        entropy_px=entropy_px,
        hedge_px=hedge_px,
        fees_usd=fees,
    )


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def replay_files(*, minutes_path: str, signal_paths: Sequence[str],
                 config, now_ts: Optional[float] = None) -> ReplayResult:
    minutes, minute_identity = _load_minutes(minutes_path)
    signals, signal_identity = _load_signals(signal_paths)
    if minute_identity != signal_identity:
        raise ValueError("multiple markets found across replay inputs")
    end = time.time() if now_ts is None else float(now_ts)
    if not math.isfinite(end) or end < 0:
        raise ValueError("now_ts must be finite and non-negative")
    available_signals = signals
    snapshot_times = [
        row["timestamp_ms"] / 1000.0
        for row in available_signals
        if (row.get("event") or "").strip() == "snapshot"
        and row["timestamp_ms"] / 1000.0 <= end
    ]
    has_snapshots = bool(snapshot_times)
    if has_snapshots:
        coverage_start = snapshot_times[0]
        signals = [
            row for row in available_signals
            if (coverage_start
                <= row["timestamp_ms"] / 1000.0
                <= end)
        ]
        approximation = SNAPSHOT_APPROXIMATION
    else:
        signals = [
            row for row in available_signals
            if row["timestamp_ms"] / 1000.0 <= end
        ]
        approximation = LEGACY_APPROXIMATION
    model = _make_model(config)
    strategy = _make_strategy(config)
    minute_index = 0
    last_model_minute = None
    campaign = None
    raw_buy = 0
    entry_decisions = 0
    opened = completed = 0
    normal = soft = hard = 0
    holds = []
    max_notional = 0.0
    max_slippage = 0.0
    reference_rejects = 0
    invalid_reference_entries = 0
    entries_during_unstable_gap = 0
    reverse_campaigns = 0
    actions = 0
    max_accumulated_notional = 0.0
    last_action_ts = float("-inf")
    armed = {"sell_entropy": None, "buy_entropy": None}

    def clear_armed() -> None:
        for direction in armed:
            armed[direction] = None

    for row in signals:
        ts = row["timestamp_ms"] / 1000.0
        closed_minute = int(ts // 60) - 1
        while (minute_index < len(minutes)
               and minutes[minute_index]["minute"] <= closed_minute):
            minute = minutes[minute_index]
            if last_model_minute is not None:
                for missing in range(
                        last_model_minute + 1, minute["minute"]):
                    model.observe(
                        minute=missing, residual_bps=None, valid=False)
            ages = (minute["entropy_age_ms"], minute["hedge_age_ms"])
            valid = (
                minute["residual"] is not None
                and all(value is not None and value >= 0 for value in ages)
                and minute["skew_ms"] is not None
                and minute["skew_ms"] >= 0
                and max(ages) <= (
                    config.strategy_entry_reference_max_age_sec * 1000.0)
                and minute["skew_ms"] <= (
                    config.strategy_entry_reference_max_skew_sec * 1000.0))
            model.observe(
                minute=minute["minute"],
                residual_bps=minute["residual"],
                valid=valid,
            )
            last_model_minute = minute["minute"]
            minute_index += 1
        if last_model_minute is not None:
            for missing in range(last_model_minute + 1, closed_minute + 1):
                model.observe(
                    minute=missing, residual_bps=None, valid=False)
                last_model_minute = missing

        raw_buy += row["direction"] == "buy_entropy"
        market, reference_valid = _signal_market(row, config)
        snapshot = model.snapshot(now_minute=int(ts // 60))
        prior = campaign
        decision = strategy.decide(
            market=market,
            model=snapshot,
            campaign=campaign,
            now_wall=ts,
            now_mono=ts,
        )
        if decision.reason.startswith("REFERENCE_"):
            reference_rejects += 1
        if decision.intent in {"OPEN", "ADD"}:
            headroom = _entry_headroom(
                config, campaign, decision.direction, market)
            if headroom < market.entry_cap_notional:
                limited_market = replace(
                    market, entry_cap_notional=headroom)
                decision = strategy.decide(
                    market=limited_market,
                    model=snapshot,
                    campaign=campaign,
                    now_wall=ts,
                    now_mono=ts,
                )
        if decision.intent not in {"OPEN", "ADD", "CLOSE", "FORCED_CLOSE"}:
            clear_armed()
            continue
        if decision.intent in {"OPEN", "ADD"}:
            if ts - last_action_ts < config.cooldown_sec:
                clear_armed()
                continue
            delay = config.premium_persist_sec
            if delay > 0:
                direction = decision.direction
                other = (
                    "buy_entropy" if direction == "sell_entropy"
                    else "sell_entropy")
                armed[other] = None
                if armed[direction] is None:
                    armed[direction] = ts
                    continue
                if ts - armed[direction] < delay:
                    continue
        clear_armed()
        if decision.intent in {"OPEN", "ADD"}:
            entry_decisions += 1
            invalid_reference_entries += not reference_valid
            entries_during_unstable_gap += (
                snapshot.status == "REGIME_UNSTABLE")
        if (prior is not None and decision.intent == "OPEN"
                and decision.direction != prior.direction):
            reverse_campaigns += 1
        plan = decision.plan
        max_notional = max(
            max_notional, plan.buy_notional, plan.sell_notional)
        for budget in (
                decision.buy_slippage_budget_bps,
                decision.sell_slippage_budget_bps):
            if budget is not None:
                max_slippage = max(max_slippage, budget)
        campaign = _apply_shadow(
            campaign, decision, signal_identity, ts, config)
        actions += 1
        last_action_ts = ts
        if campaign is not None:
            entropy_mid = market.entropy_book.mid()
            hedge_mid = market.hedge_book.mid()
            max_accumulated_notional = max(
                max_accumulated_notional,
                campaign.qty * entropy_mid,
                campaign.qty * hedge_mid,
            )
        if decision.intent == "OPEN":
            opened += 1
        if prior is not None and campaign is None:
            completed += 1
            holds.append(max(ts - prior.opened_at, 0.0))
            if decision.intent == "FORCED_CLOSE":
                hard += 1
            elif decision.reason == "SOFT_EXIT_NONNEGATIVE":
                soft += 1
            else:
                normal += 1

    total = len(signals)
    timestamps = [row["timestamp_ms"] for row in signals]
    lifecycle_rows = [
        row for row in signals
        if (row.get("event") or "").strip() != "snapshot"
    ]
    raw_total = len(lifecycle_rows)
    raw_sell = sum(
        row["direction"] == "sell_entropy" for row in lifecycle_rows)
    coverage_end = (
        None if not available_signals
        else available_signals[-1]["timestamp_ms"] / 1000.0)
    coverage_timestamps = []
    if has_snapshots:
        for row in available_signals:
            ts_ms = row["timestamp_ms"]
            if ts_ms / 1000.0 < coverage_start:
                continue
            coverage_timestamps.append(ts_ms)
            if ts_ms / 1000.0 >= end:
                break
    timeline_gaps_ok = all(
        right - left <= 2_500.0
        for left, right in zip(
            coverage_timestamps, coverage_timestamps[1:])
    )
    timeline_complete = bool(
        has_snapshots
        and coverage_end is not None
        and coverage_end >= end
        and timeline_gaps_ok
    )
    return ReplayResult(
        approximation=approximation,
        minute_rows=len(minutes),
        signal_rows=total,
        requested_end_ts=end,
        coverage_end_ts=coverage_end,
        timeline_complete=timeline_complete,
        actions=actions,
        timestamps_monotonic=all(
            left <= right for left, right in zip(timestamps, timestamps[1:])),
        raw_buy_coverage=(raw_buy / raw_total if raw_total else 0.0),
        raw_sell_coverage=(raw_sell / raw_total if raw_total else 0.0),
        residual_open_coverage=(entry_decisions / total if total else 0.0),
        campaigns_opened=opened,
        campaigns_completed=completed,
        campaigns_still_open=1 if campaign is not None else 0,
        normal_closes=normal,
        soft_closes=soft,
        hard_closes=hard,
        hold_p50_seconds=_percentile(holds, .50),
        hold_p75_seconds=_percentile(holds, .75),
        hold_p95_seconds=_percentile(holds, .95),
        hold_max_seconds=max(holds) if holds else None,
        max_planned_leg_notional=max_notional,
        max_accumulated_leg_notional=max_accumulated_notional,
        max_slippage_budget_bps=max_slippage,
        reference_gate_rejects=reference_rejects,
        invalid_reference_entries=invalid_reference_entries,
        entries_during_unstable_gap=entries_during_unstable_gap,
        reverse_campaigns=reverse_campaigns,
    )


def _format_optional(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="replay dynamic residual strategy from recorded data")
    parser.add_argument("--minutes", required=True)
    parser.add_argument("--signals", nargs="+", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--now-ts", type=float, default=time.time())
    args = parser.parse_args()

    _, identity = _load_signals(args.signals)
    config = load_config(
        args.config,
        os.path.join(os.path.dirname(args.config), ".replay-no-env"),
        symbol=identity.entropy_symbol,
        hedge_venue=identity.hedge_venue,
        hedge_symbol=identity.hedge_symbol,
        record_only=True,
        validate_outputs=False,
    )
    result = replay_files(
        minutes_path=args.minutes,
        signal_paths=args.signals,
        config=config,
        now_ts=args.now_ts,
    )
    print(f"\n=== dynamic residual replay ({result.approximation}) ===")
    print(f"minutes: {result.minute_rows}  signals: {result.signal_rows}")
    coverage = (
        "n/a" if result.coverage_end_ts is None
        else f"{result.coverage_end_ts:.3f}")
    print(f"requested end: {result.requested_end_ts:.3f}  "
          f"coverage end: {coverage}")
    if not result.timeline_complete:
        print("WARNING: replay timeline is incomplete; campaign and hold "
              "metrics apply only to the recorded coverage window.")
    print(f"simulated actions: {result.actions}")
    print(f"raw direction coverage: buy={result.raw_buy_coverage:.1%} "
          f"sell={result.raw_sell_coverage:.1%}")
    print(f"residual entry coverage: {result.residual_open_coverage:.1%}")
    print("campaigns: "
          f"opened={result.campaigns_opened} "
          f"completed={result.campaigns_completed} "
          f"still_open={result.campaigns_still_open}")
    print(f"closes: normal={result.normal_closes} soft={result.soft_closes} "
          f"hard={result.hard_closes}")
    print("hold: "
          f"p50={_format_optional(result.hold_p50_seconds)} "
          f"p75={_format_optional(result.hold_p75_seconds)} "
          f"p95={_format_optional(result.hold_p95_seconds)} "
          f"max={_format_optional(result.hold_max_seconds)}")
    print(f"max planned leg notional: "
          f"${result.max_planned_leg_notional:.2f}")
    print(f"max accumulated leg notional: "
          f"${result.max_accumulated_leg_notional:.2f}")
    print(f"max slippage budget: {result.max_slippage_budget_bps:.2f} bps")
    print(f"reference rejects: {result.reference_gate_rejects}  "
          f"invalid-reference entries: {result.invalid_reference_entries}")
    print("entries during unstable gaps: "
          f"{result.entries_during_unstable_gap}")
    print(f"reverse campaigns: {result.reverse_campaigns}")
    print("Replay PnL is intentionally not reported: recorded signals do not "
          "contain full depth or actual strategy fills.")


if __name__ == "__main__":
    main()
