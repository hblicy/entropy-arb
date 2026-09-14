"""Low-frequency journal for dynamic-strategy decisions and campaigns."""
from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


STRATEGY_EVENT_HEADER = [
    "ts_ms", "time_utc", "mode", "event", "intent", "reason",
    "decision_id", "campaign_id", "entropy_symbol", "entropy_dex",
    "hedge_symbol", "hedge_venue", "direction", "campaign_status",
    "model_version", "model_samples", "model_status", "model_median_bps",
    "model_lower_bps", "model_upper_bps", "model_iqr_bps",
    "signed_residual_bps", "reference_basis_bps", "entry_boundary_bps",
    "exit_target_bps", "convergence_bps", "round_trip_fee_bps",
    "buy_slippage_budget_bps", "sell_slippage_budget_bps",
    "projected_net_bps", "projected_net_usd",
    "estimated_campaign_pnl_usd", "qty", "planned_notional_usd",
    "entropy_reference_age_ms", "hedge_reference_age_ms",
    "reference_update_skew_ms", "net_funding_bps_per_hour",
    "entropy_fill_px", "hedge_fill_px", "hold_seconds",
    "realized_pnl_usd",
]


@dataclass(frozen=True)
class StrategyEvent:
    """One strategy observation. ``ts`` is Unix time in seconds."""

    ts: float
    mode: str
    event: str
    intent: str = ""
    reason: str = ""
    decision_id: str = ""
    campaign_id: str = ""
    entropy_symbol: str = ""
    entropy_dex: str = ""
    hedge_symbol: str = ""
    hedge_venue: str = ""
    direction: str = ""
    campaign_status: str = ""
    model_version: str = ""
    model_samples: Optional[int] = None
    model_status: str = ""
    model_median_bps: Optional[float] = None
    model_lower_bps: Optional[float] = None
    model_upper_bps: Optional[float] = None
    model_iqr_bps: Optional[float] = None
    signed_residual_bps: Optional[float] = None
    reference_basis_bps: Optional[float] = None
    entry_boundary_bps: Optional[float] = None
    exit_target_bps: Optional[float] = None
    convergence_bps: Optional[float] = None
    round_trip_fee_bps: Optional[float] = None
    buy_slippage_budget_bps: Optional[float] = None
    sell_slippage_budget_bps: Optional[float] = None
    projected_net_bps: Optional[float] = None
    projected_net_usd: Optional[float] = None
    estimated_campaign_pnl_usd: Optional[float] = None
    qty: Optional[float] = None
    planned_notional_usd: Optional[float] = None
    entropy_reference_age_ms: Optional[float] = None
    hedge_reference_age_ms: Optional[float] = None
    reference_update_skew_ms: Optional[float] = None
    net_funding_bps_per_hour: Optional[float] = None
    entropy_fill_px: Optional[float] = None
    hedge_fill_px: Optional[float] = None
    hold_seconds: Optional[float] = None
    realized_pnl_usd: Optional[float] = None


class StrategyEventRecorder:
    """Append-only UTF-8 CSV writer with bounded duplicate SKIP output."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._last_skip_key: Optional[tuple] = None
        exists = self.path.exists()
        if exists and self.path.stat().st_size:
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                try:
                    header = next(csv.reader(handle))
                except StopIteration:
                    header = []
            if header != STRATEGY_EVENT_HEADER:
                raise ValueError(
                    f"strategy event CSV header does not match: {self.path}")
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._handle, fieldnames=STRATEGY_EVENT_HEADER,
            extrasaction="raise")
        if not exists or self.path.stat().st_size == 0:
            self._writer.writeheader()
            self._handle.flush()

    def record(self, event: StrategyEvent) -> bool:
        if self._closed:
            raise ValueError("strategy event recorder is closed")
        if not math.isfinite(event.ts) or event.ts < 0:
            raise ValueError("strategy event timestamp must be finite and >= 0")
        if event.intent == "SKIP":
            skip_key = (
                int(event.ts // 60), event.reason, event.campaign_id,
                event.direction,
            )
            if skip_key == self._last_skip_key:
                return False
            self._last_skip_key = skip_key

        values = asdict(event)
        ts = values.pop("ts")
        row = {
            "ts_ms": int(round(ts * 1000)),
            "time_utc": datetime.fromtimestamp(
                ts, tz=timezone.utc).isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
        }
        row.update({key: "" if value is None else value
                    for key, value in values.items()})
        self._writer.writerow(row)
        self._handle.flush()
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._handle.close()
        self._closed = True

    def __enter__(self) -> "StrategyEventRecorder":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
