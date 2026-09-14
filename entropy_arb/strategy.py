"""Exchange-independent dynamic residual strategy primitives."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional


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
