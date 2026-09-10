"""Exchange-independent reference prices and funding state."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional


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
        self._websocket_generation = 0

    @property
    def snapshot(self) -> MarketReference:
        return self._snapshot

    @property
    def websocket_generation(self) -> int:
        return self._websocket_generation

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
            self._websocket_generation += 1
        return True

    def apply_rest_if_ws_unchanged(
            self, update: ReferenceUpdate, *,
            expected_websocket_generation: int,
            received_mono: Optional[float] = None) -> bool:
        if self._websocket_generation != expected_websocket_generation:
            return False
        return self.apply(
            update, source="rest", received_mono=received_mono)

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


@dataclass(frozen=True)
class ReferenceMetrics:
    reference_basis_bps: Optional[float]
    signed_executable_premium_bps: Optional[float]
    signed_residual_bps: Optional[float]
    residual_edge_bps: Optional[float]
    net_funding_bps_per_hour: Optional[float]


def calculate_reference_metrics(*, direction: str, entropy_bid: float,
                                entropy_ask: float, hedge_bid: float,
                                hedge_ask: float,
                                entropy: MarketReference,
                                hedge: MarketReference) -> ReferenceMetrics:
    if direction not in {"sell_entropy", "buy_entropy"}:
        raise ValueError(f"unknown direction {direction!r}")
    basis = None
    if entropy.oracle_px is not None and hedge.index_px is not None:
        basis = (entropy.oracle_px / hedge.index_px - 1.0) * 1e4
    signed_premium = (
        (entropy_bid / hedge_ask - 1.0) * 1e4
        if direction == "sell_entropy"
        else (entropy_ask / hedge_bid - 1.0) * 1e4
    )
    signed_residual = None if basis is None else signed_premium - basis
    residual_edge = signed_residual
    if direction == "buy_entropy" and signed_residual is not None:
        residual_edge = -signed_residual
    funding = None
    if (entropy.funding_current_bps_per_hour is not None
            and hedge.funding_current_bps_per_hour is not None):
        funding = (entropy.funding_current_bps_per_hour
                   - hedge.funding_current_bps_per_hour)
        if direction == "buy_entropy":
            funding = -funding
    return ReferenceMetrics(
        reference_basis_bps=basis,
        signed_executable_premium_bps=signed_premium,
        signed_residual_bps=signed_residual,
        residual_edge_bps=residual_edge,
        net_funding_bps_per_hour=funding,
    )


@dataclass(frozen=True)
class ReferenceAlertEvent:
    kind: str
    active: bool
    direction: Optional[str] = None
    value_bps: Optional[float] = None


class ReferenceAlertState:
    """Stateful residual persistence and stale/recovery event generator."""

    _DIRECTIONS = ("sell_entropy", "buy_entropy")

    def __init__(self, *, alert_bps: float, persist_sec: float) -> None:
        self.alert_bps = alert_bps
        self.persist_sec = persist_sec
        self._candidate_since: Dict[str, Optional[float]] = {
            direction: None for direction in self._DIRECTIONS}
        self._active: Dict[str, bool] = {
            direction: False for direction in self._DIRECTIONS}
        self._stale = False

    def observe(self, *, now_mono: float,
                sell_residual_bps: Optional[float],
                buy_residual_bps: Optional[float],
                stale: bool) -> list[ReferenceAlertEvent]:
        events = []
        if stale != self._stale:
            self._stale = stale
            events.append(ReferenceAlertEvent(kind="stale", active=stale))

        values = {
            "sell_entropy": sell_residual_bps,
            "buy_entropy": buy_residual_bps,
        }
        for direction, value in values.items():
            if value is None:
                self._candidate_since[direction] = None
                continue
            above = abs(value) >= self.alert_bps
            if above:
                if self._active[direction]:
                    continue
                since = self._candidate_since[direction]
                if since is None:
                    since = now_mono
                    self._candidate_since[direction] = since
                if now_mono - since >= self.persist_sec:
                    self._active[direction] = True
                    events.append(ReferenceAlertEvent(
                        kind="residual", active=True,
                        direction=direction, value_bps=value))
                continue
            self._candidate_since[direction] = None
            if self._active[direction]:
                self._active[direction] = False
                events.append(ReferenceAlertEvent(
                    kind="residual", active=False,
                    direction=direction, value_bps=value))
        return events
