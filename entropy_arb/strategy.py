"""Exchange-independent dynamic residual strategy primitives."""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

from .book import OrderBook, plan_convergence_trade, plan_matched_close
from .slippage import SlippageModel

if TYPE_CHECKING:
    from .campaign import PositionCampaign


MODEL_NOT_READY = "MODEL_NOT_READY"
READY = "READY"
REGIME_UNSTABLE = "REGIME_UNSTABLE"


def _quantile(ordered: list[float], q: float) -> float:
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


@dataclass(frozen=True)
class ModelSnapshot:
    version: int
    minute: int
    samples: int
    status: str
    median_bps: Optional[float]
    lower_bps: Optional[float]
    q25_bps: Optional[float]
    q75_bps: Optional[float]
    upper_bps: Optional[float]

    @property
    def ready(self) -> bool:
        return self.status == READY

    @property
    def iqr_bps(self) -> Optional[float]:
        if self.q25_bps is None or self.q75_bps is None:
            return None
        return self.q75_bps - self.q25_bps


@dataclass(frozen=True)
class MarketIdentity:
    entropy_symbol: str
    entropy_dex: str
    hedge_symbol: str
    hedge_venue: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip()
                   for value in (
                       self.entropy_symbol,
                       self.entropy_dex,
                       self.hedge_symbol,
                       self.hedge_venue,
                   )):
            raise ValueError("market identity values must not be empty")


@dataclass(frozen=True)
class WarmStartResult:
    accepted: int = 0
    rejected_identity: int = 0
    rejected_reference: int = 0
    rejected_value: int = 0
    rejected_time: int = 0


class ResidualModel:
    """Robust minute model with latched regime-instability recovery."""

    def __init__(self, *, window_minutes: int, min_samples: int,
                 lower_quantile: float, upper_quantile: float,
                 regime_window_minutes: int, recovery_minutes: int) -> None:
        integers = (window_minutes, min_samples, regime_window_minutes,
                    recovery_minutes)
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in integers):
            raise ValueError("minute counts must be integers")
        if any(value <= 0 for value in integers):
            raise ValueError("minute counts must be positive")
        if min_samples > window_minutes:
            raise ValueError("min_samples must not exceed window_minutes")
        if regime_window_minutes > window_minutes:
            raise ValueError(
                "regime_window_minutes must not exceed window_minutes")
        if not (math.isfinite(lower_quantile)
                and math.isfinite(upper_quantile)
                and 0 <= lower_quantile < .5 < upper_quantile <= 1):
            raise ValueError("invalid residual quantiles")
        self.window_minutes = window_minutes
        self.min_samples = min_samples
        self.lower_quantile = lower_quantile
        self.upper_quantile = upper_quantile
        self.regime_window_minutes = regime_window_minutes
        self.recovery_minutes = recovery_minutes
        self._observations: Dict[int, Optional[float]] = {}
        self._latest_minute = -1
        self._version = 0
        self._unstable = False
        self._stable_recovery = 0

    def observe(self, *, minute: int, residual_bps: Optional[float],
                valid: bool) -> None:
        if (isinstance(minute, bool) or not isinstance(minute, int)
                or minute < 0):
            raise ValueError("minute must be a non-negative integer")
        if not isinstance(valid, bool):
            raise ValueError("valid must be boolean")
        if valid and (residual_bps is None
                      or isinstance(residual_bps, bool)
                      or not isinstance(residual_bps, (int, float))
                      or not math.isfinite(residual_bps)):
            raise ValueError("valid residual must be a finite number")
        value = float(residual_bps) if valid else None
        changed = (minute not in self._observations
                   or self._observations[minute] != value)
        if not changed:
            return
        self._observations[minute] = value
        self._version += 1
        previous_latest = self._latest_minute
        self._latest_minute = max(self._latest_minute, minute)
        self._drop_expired(self._latest_minute)
        if minute < previous_latest:
            return
        instant_unstable = self._instant_unstable(self._latest_minute)
        if instant_unstable:
            self._unstable = True
            self._stable_recovery = 0
        elif self._unstable:
            if value is None:
                self._stable_recovery = 0
            else:
                self._stable_recovery += 1
                if self._stable_recovery >= self.recovery_minutes:
                    self._unstable = False
                    self._stable_recovery = 0

    def snapshot(self, *, now_minute: int) -> ModelSnapshot:
        if (isinstance(now_minute, bool) or not isinstance(now_minute, int)
                or now_minute < 0):
            raise ValueError("now_minute must be a non-negative integer")
        values = self._window_values(now_minute, self.window_minutes)
        ordered = sorted(values)
        if not ordered:
            return ModelSnapshot(
                self._version, now_minute, 0, MODEL_NOT_READY,
                None, None, None, None, None)
        median = _quantile(ordered, .5)
        lower = _quantile(ordered, self.lower_quantile)
        q25 = _quantile(ordered, .25)
        q75 = _quantile(ordered, .75)
        upper = _quantile(ordered, self.upper_quantile)
        status = MODEL_NOT_READY
        if len(ordered) >= self.min_samples:
            status = REGIME_UNSTABLE if self._unstable else READY
        return ModelSnapshot(
            self._version, now_minute, len(ordered), status,
            median, lower, q25, q75, upper)

    def _window_values(self, now_minute: int,
                       length: int) -> list[float]:
        first = now_minute - length + 1
        return [
            value for minute, value in self._observations.items()
            if first <= minute <= now_minute and value is not None
        ]

    def _drop_expired(self, now_minute: int) -> None:
        first = now_minute - self.window_minutes + 1
        expired = [minute for minute in self._observations if minute < first]
        for minute in expired:
            del self._observations[minute]

    def _instant_unstable(self, now_minute: int) -> bool:
        main = sorted(self._window_values(now_minute, self.window_minutes))
        if len(main) < self.min_samples:
            return False
        missing_start = now_minute - 4
        if all(self._observations.get(minute) is None
               for minute in range(missing_start, now_minute + 1)):
            return True
        short = sorted(self._window_values(
            now_minute, self.regime_window_minutes))
        if not short:
            return False
        main_median = _quantile(main, .5)
        main_iqr = _quantile(main, .75) - _quantile(main, .25)
        short_median = _quantile(short, .5)
        short_iqr = _quantile(short, .75) - _quantile(short, .25)
        median_shift = abs(short_median - main_median)
        if main_iqr == 0:
            return median_shift > 0 or short_iqr > 0
        return median_shift > main_iqr or short_iqr > 2 * main_iqr


_HISTORY_FIELDS = {
    "minute_ts",
    "entropy_symbol",
    "entropy_dex",
    "hedge_symbol",
    "hedge_venue",
    "entropy_reference_age_ms",
    "hedge_reference_age_ms",
    "reference_update_skew_ms",
    "residual_close_bps",
}


def _finite_float(row: dict[str, str], field: str) -> float:
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f"{field} must be finite")
    return value


def warm_start_residual_model(
        model: ResidualModel, *, path: str, identity: MarketIdentity,
        now_minute: int, max_age_sec: float,
        max_skew_sec: float) -> WarmStartResult:
    """Load recent, identity-matched, reference-valid minute closes."""
    if (isinstance(now_minute, bool) or not isinstance(now_minute, int)
            or now_minute < 0):
        raise ValueError("now_minute must be a non-negative integer")
    if any(not math.isfinite(value) or value <= 0
           for value in (max_age_sec, max_skew_sec)):
        raise ValueError("reference limits must be finite and positive")
    if not os.path.exists(path):
        return WarmStartResult()

    accepted_rows: list[tuple[int, float]] = []
    counts = {
        "accepted": 0,
        "rejected_identity": 0,
        "rejected_reference": 0,
        "rejected_value": 0,
        "rejected_time": 0,
    }
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not _HISTORY_FIELDS.issubset(set(reader.fieldnames or ())):
            raise ValueError("minute history header is incompatible")
        for row in reader:
            try:
                row_identity = MarketIdentity(
                    (row.get("entropy_symbol") or "").strip(),
                    (row.get("entropy_dex") or "").strip(),
                    (row.get("hedge_symbol") or "").strip(),
                    (row.get("hedge_venue") or "").strip(),
                )
            except ValueError:
                counts["rejected_identity"] += 1
                continue
            if row_identity != identity:
                counts["rejected_identity"] += 1
                continue
            try:
                timestamp = _finite_float(row, "minute_ts")
            except (KeyError, TypeError, ValueError):
                counts["rejected_time"] += 1
                continue
            minute = math.floor(timestamp / 60.0)
            first_minute = now_minute - model.window_minutes + 1
            if timestamp < 0 or minute < first_minute or minute > now_minute:
                counts["rejected_time"] += 1
                continue
            try:
                entropy_age = _finite_float(
                    row, "entropy_reference_age_ms")
                hedge_age = _finite_float(row, "hedge_reference_age_ms")
                skew = _finite_float(row, "reference_update_skew_ms")
            except (KeyError, TypeError, ValueError):
                counts["rejected_reference"] += 1
                continue
            if (min(entropy_age, hedge_age, skew) < 0
                    or entropy_age > max_age_sec * 1000.0
                    or hedge_age > max_age_sec * 1000.0
                    or skew > max_skew_sec * 1000.0):
                counts["rejected_reference"] += 1
                continue
            try:
                residual = _finite_float(row, "residual_close_bps")
            except (KeyError, TypeError, ValueError):
                counts["rejected_value"] += 1
                continue
            accepted_rows.append((minute, residual))
            counts["accepted"] += 1

    for minute, residual in sorted(accepted_rows):
        model.observe(minute=minute, residual_bps=residual, valid=True)
    return WarmStartResult(**counts)


@dataclass(frozen=True)
class MarketView:
    entropy_book: OrderBook
    hedge_book: OrderBook
    entropy_oracle_px: Optional[float]
    hedge_index_px: Optional[float]
    entropy_reference_age_sec: Optional[float]
    hedge_reference_age_sec: Optional[float]
    reference_skew_sec: Optional[float]
    books_ready: bool
    entropy_fee_bps: float
    hedge_fee_bps: float
    take_fraction: float
    entry_cap_notional: float
    min_base: float
    min_notional: float
    size_step: float


@dataclass(frozen=True)
class StrategyDecision:
    intent: str = "SKIP"
    direction: str = ""
    reason: str = ""
    plan: Optional[Any] = None
    model: Optional[ModelSnapshot] = None
    signed_residual_bps: Optional[float] = None
    reference_basis_bps: Optional[float] = None
    entry_boundary_bps: Optional[float] = None
    exit_target_bps: Optional[float] = None
    convergence_bps: Optional[float] = None
    round_trip_fee_bps: Optional[float] = None
    buy_slippage_budget_bps: Optional[float] = None
    sell_slippage_budget_bps: Optional[float] = None
    estimated_campaign_pnl_usd: Optional[float] = None


class DynamicResidualStrategy:
    """Pure strategy decision layer over normalized books and references."""

    def __init__(
            self, *, slippage: SlippageModel,
            reference_max_age_sec: float,
            reference_max_skew_sec: float,
            exit_band_fraction: float,
            min_exit_band_bps: float,
            min_expected_profit_bps: float,
            soft_hold_sec: float,
            hard_hold_sec: float,
            hard_slippage_bps: float,
            max_edge_fraction: float) -> None:
        self.slippage = slippage
        self.reference_max_age_sec = reference_max_age_sec
        self.reference_max_skew_sec = reference_max_skew_sec
        self.exit_band_fraction = exit_band_fraction
        self.min_exit_band_bps = min_exit_band_bps
        self.min_expected_profit_bps = min_expected_profit_bps
        self.soft_hold_sec = soft_hold_sec
        self.hard_hold_sec = hard_hold_sec
        self.hard_slippage_bps = hard_slippage_bps
        self.max_edge_fraction = max_edge_fraction

    @staticmethod
    def _skip(reason: str, *, direction: str = "",
              model: Optional[ModelSnapshot] = None) -> StrategyDecision:
        return StrategyDecision(
            intent="SKIP", direction=direction, reason=reason, model=model)

    @staticmethod
    def _book_gate(market: MarketView) -> Optional[str]:
        if not market.books_ready:
            return "BOOK_NOT_READY"
        prices = (
            market.entropy_book.best_bid(), market.entropy_book.best_ask(),
            market.hedge_book.best_bid(), market.hedge_book.best_ask())
        if any(value is None or not math.isfinite(value) or value <= 0
               for value in prices):
            return "BOOK_NOT_READY"
        return None

    def _reference_gate(self, market: MarketView) -> Optional[str]:
        values = (
            market.entropy_oracle_px, market.hedge_index_px,
            market.entropy_reference_age_sec,
            market.hedge_reference_age_sec,
            market.reference_skew_sec,
        )
        if any(value is None for value in values):
            return "REFERENCE_INCOMPLETE"
        if any(not math.isfinite(value) or value < 0 for value in values):
            return "REFERENCE_INCOMPLETE"
        if market.entropy_oracle_px <= 0 or market.hedge_index_px <= 0:
            return "REFERENCE_INCOMPLETE"
        if (market.entropy_reference_age_sec > self.reference_max_age_sec
                or market.hedge_reference_age_sec
                > self.reference_max_age_sec):
            return "REFERENCE_STALE"
        if market.reference_skew_sec > self.reference_max_skew_sec:
            return "REFERENCE_SKEW"
        return None

    @staticmethod
    def _model_gate(model: ModelSnapshot) -> Optional[str]:
        if model.status == MODEL_NOT_READY:
            return MODEL_NOT_READY
        if model.status != READY:
            return REGIME_UNSTABLE
        return None

    @staticmethod
    def _reference_values(
            market: MarketView) -> tuple[float, float, float]:
        basis = (
            market.entropy_oracle_px / market.hedge_index_px - 1.0) * 1e4
        sell_residual = (
            market.entropy_book.best_bid()
            / market.hedge_book.best_ask() - 1.0) * 1e4 - basis
        buy_residual = (
            market.entropy_book.best_ask()
            / market.hedge_book.best_bid() - 1.0) * 1e4 - basis
        return basis, sell_residual, buy_residual

    def decide(
            self, *, market: MarketView, model: ModelSnapshot,
            campaign: Optional["PositionCampaign"], now_wall: float,
            now_mono: float) -> StrategyDecision:
        book_error = self._book_gate(market)
        if book_error is not None:
            return self._skip(book_error, model=model)
        if campaign is not None:
            status = campaign.status_at(
                now_wall, soft_sec=self.soft_hold_sec,
                hard_sec=self.hard_hold_sec)
            if status == "HARD_EXIT":
                return self._hard_close(
                    market=market, model=model, campaign=campaign)
            reference_error = self._reference_gate(market)
            if reference_error is not None:
                return self._skip(
                    reference_error, direction=campaign.direction,
                    model=model)
            close = self._normal_or_soft_close(
                market=market, model=model, campaign=campaign,
                status=status, now_mono=now_mono)
            if close is not None:
                return close
            if status != "OPEN":
                return self._skip(
                    "SOFT_EXIT_WAITING", direction=campaign.direction,
                    model=model)
            model_error = self._model_gate(model)
            if model_error is not None:
                return self._skip(
                    model_error, direction=campaign.direction, model=model)
            return self._add_or_skip(
                market=market, model=model, campaign=campaign,
                now_mono=now_mono)

        model_error = self._model_gate(model)
        if model_error is not None:
            return self._skip(model_error, model=model)
        reference_error = self._reference_gate(market)
        if reference_error is not None:
            return self._skip(reference_error, model=model)
        return self._open_or_skip(
            market=market, model=model, now_mono=now_mono)

    def _open_or_skip(
            self, *, market: MarketView, model: ModelSnapshot,
            now_mono: float) -> StrategyDecision:
        basis, sell_residual, buy_residual = self._reference_values(market)
        candidates = []
        if sell_residual >= model.upper_bps:
            candidates.append((
                "sell_entropy", sell_residual, model.upper_bps))
        if buy_residual <= model.lower_bps:
            candidates.append((
                "buy_entropy", buy_residual, model.lower_bps))
        if not candidates:
            return self._skip("ENTRY_NOT_REACHED", model=model)
        decisions = [
            self._entry_decision(
                intent="OPEN", direction=direction, residual=residual,
                entry_boundary=boundary, market=market, model=model,
                basis=basis, now_mono=now_mono,
                exit_target=None)
            for direction, residual, boundary in candidates
        ]
        executable = [decision for decision in decisions
                      if decision.intent == "OPEN"]
        if executable:
            return max(
                executable,
                key=lambda decision: decision.plan.projected_net_bps)
        return decisions[0]

    def _add_or_skip(
            self, *, market: MarketView, model: ModelSnapshot,
            campaign: "PositionCampaign", now_mono: float) -> StrategyDecision:
        basis, sell_residual, buy_residual = self._reference_values(market)
        residual = (sell_residual if campaign.direction == "sell_entropy"
                    else buy_residual)
        reached = (
            residual >= campaign.entry_boundary_bps
            if campaign.direction == "sell_entropy"
            else residual <= campaign.entry_boundary_bps)
        if not reached:
            opposite_reached = (
                buy_residual <= model.lower_bps
                if campaign.direction == "sell_entropy"
                else sell_residual >= model.upper_bps)
            return self._skip(
                "CAMPAIGN_DIRECTION_LOCKED" if opposite_reached
                else "ENTRY_NOT_REACHED",
                direction=campaign.direction, model=model)
        return self._entry_decision(
            intent="ADD", direction=campaign.direction,
            residual=residual,
            entry_boundary=campaign.entry_boundary_bps,
            exit_target=campaign.exit_target_bps,
            market=market, model=model, basis=basis,
            now_mono=now_mono)

    def _entry_decision(
            self, *, intent: str, direction: str, residual: float,
            entry_boundary: float, exit_target: Optional[float],
            market: MarketView, model: ModelSnapshot, basis: float,
            now_mono: float) -> StrategyDecision:
        if (market.entry_cap_notional <= 0
                or market.entry_cap_notional < market.min_notional):
            return self._skip(
                "POSITION_CAP_REACHED", direction=direction, model=model)
        if (self.slippage.entry_paused("entropy", now_mono)
                or self.slippage.entry_paused("hedge", now_mono)):
            return self._skip(
                "SLIPPAGE_PAUSED", direction=direction, model=model)
        if exit_target is None:
            band = abs(entry_boundary - model.median_bps)
            exit_band = max(
                self.min_exit_band_bps, band * self.exit_band_fraction)
            exit_target = (
                model.median_bps + exit_band
                if direction == "sell_entropy"
                else model.median_bps - exit_band)
        convergence = (
            residual - exit_target
            if direction == "sell_entropy"
            else exit_target - residual)
        fees = 2.0 * (
            market.entropy_fee_bps + market.hedge_fee_bps)
        if direction == "sell_entropy":
            legs = (
                ("hedge", "buy"), ("entropy", "sell"),
                ("entropy", "buy"), ("hedge", "sell"))
            buy_book, sell_book = market.hedge_book, market.entropy_book
        else:
            legs = (
                ("entropy", "buy"), ("hedge", "sell"),
                ("hedge", "buy"), ("entropy", "sell"))
            buy_book, sell_book = market.entropy_book, market.hedge_book
        quotes = [
            self.slippage.quote(
                venue=venue, side=side, now=now_mono,
                convergence_bps=convergence,
                round_trip_fee_bps=fees,
                min_profit_bps=self.min_expected_profit_bps,
                max_edge_fraction=self.max_edge_fraction)
            for venue, side in legs
        ]
        if any(quote.budget_bps is None for quote in quotes):
            return self._skip(
                "SLIPPAGE_BUDGET_TOO_SMALL", direction=direction,
                model=model)
        buy_budget = quotes[0].budget_bps
        sell_budget = quotes[1].budget_bps
        close_reserve = quotes[2].budget_bps + quotes[3].budget_bps
        size_factor = min(
            self.slippage.entry_size_factor("entropy", now_mono),
            self.slippage.entry_size_factor("hedge", now_mono))
        cap = market.entry_cap_notional * size_factor
        plan, reason = plan_convergence_trade(
            buy_book, sell_book,
            direction=direction,
            reference_basis_bps=basis,
            exit_residual_bps=exit_target,
            round_trip_fee_bps=fees,
            close_slippage_reserve_bps=close_reserve,
            min_expected_profit_bps=self.min_expected_profit_bps,
            buy_slippage_budget_bps=buy_budget,
            sell_slippage_budget_bps=sell_budget,
            take_fraction=market.take_fraction,
            cap_notional=cap,
            min_base=market.min_base,
            min_notional=market.min_notional,
            size_step=market.size_step,
        )
        if plan is None:
            return self._skip(
                reason.upper(), direction=direction, model=model)
        return StrategyDecision(
            intent=intent,
            direction=direction,
            reason="ENTRY_SIGNAL",
            plan=plan,
            model=model,
            signed_residual_bps=residual,
            reference_basis_bps=basis,
            entry_boundary_bps=entry_boundary,
            exit_target_bps=exit_target,
            convergence_bps=convergence,
            round_trip_fee_bps=fees,
            buy_slippage_budget_bps=buy_budget,
            sell_slippage_budget_bps=sell_budget,
        )

    def _close_books_and_sides(
            self, market: MarketView,
            campaign: "PositionCampaign"):
        if campaign.direction == "sell_entropy":
            return (market.entropy_book, market.hedge_book,
                    "buy_entropy", ("entropy", "buy"),
                    ("hedge", "sell"))
        return (market.hedge_book, market.entropy_book,
                "sell_entropy", ("hedge", "buy"),
                ("entropy", "sell"))

    def _hard_close(
            self, *, market: MarketView, model: ModelSnapshot,
            campaign: "PositionCampaign") -> StrategyDecision:
        buy_book, sell_book, direction, _, _ = self._close_books_and_sides(
            market, campaign)
        plan, reason = plan_matched_close(
            buy_book, sell_book,
            max_qty=campaign.qty,
            cap_notional=market.entry_cap_notional,
            buy_slippage_bps=self.hard_slippage_bps,
            sell_slippage_bps=self.hard_slippage_bps,
            min_base=market.min_base,
            min_notional=market.min_notional,
            size_step=market.size_step,
        )
        if plan is None:
            return self._skip(
                f"HARD_EXIT_{reason.upper()}", direction=direction,
                model=model)
        return StrategyDecision(
            intent="FORCED_CLOSE", direction=direction,
            reason="HARD_HOLD_LIMIT", plan=plan, model=model,
            entry_boundary_bps=campaign.entry_boundary_bps,
            exit_target_bps=campaign.exit_target_bps,
            buy_slippage_budget_bps=self.hard_slippage_bps,
            sell_slippage_budget_bps=self.hard_slippage_bps,
        )

    def _normal_or_soft_close(
            self, *, market: MarketView, model: ModelSnapshot,
            campaign: "PositionCampaign", status: str,
            now_mono: float) -> Optional[StrategyDecision]:
        basis, sell_residual, buy_residual = self._reference_values(market)
        if campaign.direction == "sell_entropy":
            residual = buy_residual
            target_hit = residual <= campaign.exit_target_bps
        else:
            residual = sell_residual
            target_hit = residual >= campaign.exit_target_bps
        buy_book, sell_book, direction, buy_leg, sell_leg = (
            self._close_books_and_sides(market, campaign))
        buy_protection = self.slippage.protection(
            venue=buy_leg[0], side=buy_leg[1], now=now_mono)
        sell_protection = self.slippage.protection(
            venue=sell_leg[0], side=sell_leg[1], now=now_mono)
        estimated_pnl = self._estimate_close_pnl(
            market=market, campaign=campaign,
            buy_budget=buy_protection.budget_bps,
            sell_budget=sell_protection.budget_bps)
        soft_profitable = status == "SOFT_EXIT" and estimated_pnl >= 0
        if not target_hit and not soft_profitable:
            return None
        plan, reason = plan_matched_close(
            buy_book, sell_book,
            max_qty=campaign.qty,
            cap_notional=market.entry_cap_notional,
            buy_slippage_bps=buy_protection.budget_bps,
            sell_slippage_bps=sell_protection.budget_bps,
            min_base=market.min_base,
            min_notional=market.min_notional,
            size_step=market.size_step,
        )
        if plan is None:
            return self._skip(
                f"CLOSE_{reason.upper()}", direction=direction,
                model=model)
        return StrategyDecision(
            intent="CLOSE", direction=direction,
            reason=("RESIDUAL_EXIT_TARGET" if target_hit
                    else "SOFT_EXIT_NONNEGATIVE"),
            plan=plan,
            model=model,
            signed_residual_bps=residual,
            reference_basis_bps=basis,
            entry_boundary_bps=campaign.entry_boundary_bps,
            exit_target_bps=campaign.exit_target_bps,
            buy_slippage_budget_bps=buy_protection.budget_bps,
            sell_slippage_budget_bps=sell_protection.budget_bps,
            estimated_campaign_pnl_usd=estimated_pnl,
        )

    @staticmethod
    def _estimate_close_pnl(
            *, market: MarketView, campaign: "PositionCampaign",
            buy_budget: float, sell_budget: float) -> float:
        quantity = campaign.qty
        if campaign.direction == "buy_entropy":
            entropy_close = market.entropy_book.best_bid()
            hedge_close = market.hedge_book.best_ask()
            gross = (entropy_close - campaign.entropy_avg_px
                     + campaign.hedge_avg_px - hedge_close) * quantity
            entropy_side = entropy_close
            hedge_side = hedge_close
            buy_notional = hedge_close * quantity
            sell_notional = entropy_close * quantity
            buy_fee = market.hedge_fee_bps
            sell_fee = market.entropy_fee_bps
        else:
            entropy_close = market.entropy_book.best_ask()
            hedge_close = market.hedge_book.best_bid()
            gross = (campaign.entropy_avg_px - entropy_close
                     + hedge_close - campaign.hedge_avg_px) * quantity
            entropy_side = entropy_close
            hedge_side = hedge_close
            buy_notional = entropy_close * quantity
            sell_notional = hedge_close * quantity
            buy_fee = market.entropy_fee_bps
            sell_fee = market.hedge_fee_bps
        close_fees = (
            buy_notional * buy_fee / 1e4
            + sell_notional * sell_fee / 1e4)
        slippage_reserve = (
            buy_notional * buy_budget / 1e4
            + sell_notional * sell_budget / 1e4)
        if entropy_side <= 0 or hedge_side <= 0:
            raise ValueError("close prices must be positive")
        return (campaign.realized_pnl_usd + gross
                - close_fees - slippage_reserve)
