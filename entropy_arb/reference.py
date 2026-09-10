"""Exchange-independent reference prices and funding state."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Optional


class InvalidReference(ValueError):
    """A reference update contains invalid or unsupported data."""


@dataclass(frozen=True)
class MarketReference:
    oracle_px: Optional[float] = None
    index_px: Optional[float] = None
    mark_px: Optional[float] = None
    funding_current_bps_per_hour: Optional[float] = None
    funding_last_bps_per_hour: Optional[float] = None
    funding_last_ts_ms: Optional[int] = None
    exchange_ts_ms: Optional[int] = None
    received_mono: float = 0.0
    source: str = ""


@dataclass(frozen=True)
class ReferenceUpdate:
    oracle_px: Optional[float] = None
    index_px: Optional[float] = None
    mark_px: Optional[float] = None
    funding_current_bps_per_hour: Optional[float] = None
    funding_last_bps_per_hour: Optional[float] = None
    funding_last_ts_ms: Optional[int] = None
    exchange_ts_ms: Optional[int] = None


_PRICE_FIELDS = ("oracle_px", "index_px", "mark_px")
_FUNDING_FIELDS = (
    "funding_current_bps_per_hour",
    "funding_last_bps_per_hour",
)
_TIMESTAMP_FIELDS = ("funding_last_ts_ms", "exchange_ts_ms")
_UPDATE_FIELDS = _PRICE_FIELDS + _FUNDING_FIELDS + _TIMESTAMP_FIELDS


def _validate_update(update: ReferenceUpdate) -> None:
    if not any(getattr(update, field) is not None for field in _UPDATE_FIELDS):
        raise InvalidReference("reference update has no values")
    for field in _PRICE_FIELDS:
        value = getattr(update, field)
        if value is not None and (not math.isfinite(value) or value <= 0.0):
            raise InvalidReference(f"{field} must be finite and > 0")
    for field in _FUNDING_FIELDS:
        value = getattr(update, field)
        if value is not None and not math.isfinite(value):
            raise InvalidReference(f"{field} must be finite")
    for field in _TIMESTAMP_FIELDS:
        value = getattr(update, field)
        if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
                or value < 0):
            raise InvalidReference(f"{field} must be a non-negative integer")


class ReferenceState:
    """Atomically replace immutable reference snapshots on one event loop."""

    def __init__(self) -> None:
        self._snapshot = MarketReference()
        self.last_ws_received_mono = 0.0

    @property
    def snapshot(self) -> MarketReference:
        return self._snapshot

    def apply(self, update: ReferenceUpdate, *, source: str,
              received_mono: Optional[float] = None) -> bool:
        if source not in {"websocket", "rest"}:
            raise InvalidReference("source must be 'websocket' or 'rest'")
        _validate_update(update)
        received = time.monotonic() if received_mono is None else received_mono
        if not math.isfinite(received) or received < 0.0:
            raise InvalidReference("received_mono must be finite and >= 0")
        current = self._snapshot
        if (update.exchange_ts_ms is not None
                and current.exchange_ts_ms is not None
                and update.exchange_ts_ms < current.exchange_ts_ms):
            return False
        changes = {
            field: value
            for field in _UPDATE_FIELDS
            if (value := getattr(update, field)) is not None
        }
        self._snapshot = replace(
            current, **changes, received_mono=received, source=source)
        if source == "websocket":
            self.last_ws_received_mono = received
        return True

    def age_ms(self, now_mono: Optional[float] = None) -> Optional[float]:
        if not self._snapshot.source:
            return None
        now = time.monotonic() if now_mono is None else now_mono
        return max((now - self._snapshot.received_mono) * 1000.0, 0.0)

    def ws_is_fresh(self, stale_sec: float,
                    now_mono: Optional[float] = None) -> bool:
        if not self.last_ws_received_mono:
            return False
        now = time.monotonic() if now_mono is None else now_mono
        return now - self.last_ws_received_mono <= stale_sec
