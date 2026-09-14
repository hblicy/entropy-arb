"""Bounded real-fill slippage statistics for residual strategy entries."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class SlippageSample:
    timestamp: float
    adverse_bps: float
    decision_budget_bps: float


@dataclass(frozen=True)
class SlippageQuote:
    budget_bps: Optional[float]
    statistical_bps: float
    edge_cap_bps: float
    sample_count: int
    source: str
    reason: str = ""


@dataclass(frozen=True)
class SlippageProtection:
    budget_bps: float
    sample_count: int
    source: str


class SlippageModel:
    """Keep small per-side histories and venue-level breach controls."""

    PAUSE_SECONDS = 15 * 60.0

    def __init__(self, *, bootstrap_bps: float, min_bps: float,
                 safety_bps: float, hard_max_bps: float,
                 min_live_samples: int) -> None:
        self.bootstrap_bps = _finite("bootstrap_bps", bootstrap_bps)
        self.min_bps = _finite("min_bps", min_bps)
        self.safety_bps = _finite("safety_bps", safety_bps)
        self.hard_max_bps = _finite("hard_max_bps", hard_max_bps)
        if any(value < 0 for value in (
                self.bootstrap_bps, self.min_bps, self.safety_bps,
                self.hard_max_bps)):
            raise ValueError("slippage values must be non-negative")
        if self.min_bps > self.hard_max_bps:
            raise ValueError("min_bps must not exceed hard_max_bps")
        if (isinstance(min_live_samples, bool)
                or not isinstance(min_live_samples, int)
                or not 1 <= min_live_samples <= 50):
            raise ValueError("min_live_samples must be in [1, 50]")
        self.min_live_samples = min_live_samples
        self._samples: Dict[
            Tuple[str, str], Deque[SlippageSample]] = {}
        self._breaches: Dict[str, Deque[bool]] = {}
        self._paused_until: Dict[str, float] = {}

    @staticmethod
    def _key(venue: str, side: str) -> tuple[str, str]:
        if not isinstance(venue, str) or not venue:
            raise ValueError("venue must not be empty")
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        return venue, side

    def quote(self, *, venue: str, side: str, now: float,
              convergence_bps: float, round_trip_fee_bps: float,
              min_profit_bps: float,
              max_edge_fraction: float) -> SlippageQuote:
        now = _finite("now", now)
        convergence = _finite("convergence_bps", convergence_bps)
        fees = _finite("round_trip_fee_bps", round_trip_fee_bps)
        minimum = _finite("min_profit_bps", min_profit_bps)
        fraction = _finite("max_edge_fraction", max_edge_fraction)
        if now < 0 or fees < 0 or minimum < 0 or not 0 < fraction <= 1:
            raise ValueError("invalid slippage quote bounds")

        protection = self.protection(venue=venue, side=side, now=now)
        statistical = protection.budget_bps
        source = protection.source
        edge_cap = (convergence - fees - minimum) * fraction
        budget = min(statistical, edge_cap, self.hard_max_bps)
        if budget < self.min_bps:
            return SlippageQuote(
                None, statistical, edge_cap, protection.sample_count, source,
                "SLIPPAGE_BUDGET_TOO_SMALL")
        return SlippageQuote(
            budget, statistical, edge_cap, protection.sample_count, source)

    def protection(self, *, venue: str, side: str,
                   now: float) -> SlippageProtection:
        key = self._key(venue, side)
        now = _finite("now", now)
        if now < 0:
            raise ValueError("now must be non-negative")
        selected = self._selected_samples(key, now)
        if len(selected) < self.min_live_samples:
            budget = min(
                max(self.bootstrap_bps, self.min_bps), self.hard_max_bps)
            source = "bootstrap"
        else:
            budget = min(
                max(_percentile(
                    [sample.adverse_bps for sample in selected], .95)
                    + self.safety_bps, self.min_bps),
                self.hard_max_bps,
            )
            source = "live"
        return SlippageProtection(budget, len(selected), source)

    def record(self, *, venue: str, side: str, now: float,
               adverse_bps: float, decision_budget_bps: float) -> None:
        key = self._key(venue, side)
        now = _finite("now", now)
        adverse = _finite("adverse_bps", adverse_bps)
        budget = _finite("decision_budget_bps", decision_budget_bps)
        if now < 0 or adverse < 0 or budget < 0:
            raise ValueError("slippage samples must be non-negative")
        samples = self._samples.setdefault(key, deque(maxlen=50))
        samples.append(SlippageSample(now, adverse, budget))
        breaches = self._breaches.setdefault(venue, deque(maxlen=10))
        breaches.append(adverse > budget)
        if adverse > self.hard_max_bps or sum(breaches) >= 5:
            self._paused_until[venue] = now + self.PAUSE_SECONDS

    def sample_count(self, venue: str, side: str) -> int:
        return len(self._samples.get(self._key(venue, side), ()))

    def entry_size_factor(self, venue: str, now: float) -> float:
        if not isinstance(venue, str) or not venue:
            raise ValueError("venue must not be empty")
        _finite("now", now)
        return 0.5 if sum(self._breaches.get(venue, ())) >= 3 else 1.0

    def entry_paused(self, venue: str, now: float) -> bool:
        if not isinstance(venue, str) or not venue:
            raise ValueError("venue must not be empty")
        now = _finite("now", now)
        return now < self._paused_until.get(venue, 0.0)

    def _selected_samples(
            self, key: tuple[str, str], now: float) -> list[SlippageSample]:
        all_samples = list(self._samples.get(key, ()))
        recent = [
            sample for sample in all_samples
            if now - 3600.0 <= sample.timestamp <= now
        ]
        return recent if len(recent) >= 20 else all_samples
