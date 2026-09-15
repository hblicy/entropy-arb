"""Durable, non-secret evidence for an in-flight dynamic execution."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .campaign import PositionCampaign, _campaign_from_dict
from .strategy import MarketIdentity, ModelSnapshot


SCHEMA_VERSION = 3
_LEG_FIELDS = {
    "venue_key", "is_buy", "order_ref", "status", "filled_base",
    "avg_px", "applied_fill", "unresolved",
}
_EXECUTION_FIELDS = {
    "execution_id", "identity", "intent", "direction", "campaign_id",
    "qty", "decided_at", "frozen_model", "entry_boundary_bps",
    "exit_target_bps", "entropy_expected_px", "hedge_expected_px",
    "entropy_fee_bps", "hedge_fee_bps", "campaign_before", "buy", "sell",
    "audit_ok", "campaign_applied", "audit", "settled_at",
}
_AUDIT_FIELDS = {
    "reason", "signed_residual_bps", "reference_basis_bps",
    "convergence_bps", "round_trip_fee_bps", "buy_slippage_budget_bps",
    "sell_slippage_budget_bps", "projected_net_bps", "projected_net_usd",
    "estimated_campaign_pnl_usd", "entropy_reference_age_ms",
    "hedge_reference_age_ms", "reference_update_skew_ms",
    "net_funding_bps_per_hour", "planned_notional_usd",
}
_IDENTITY_FIELDS = {
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue",
}
_MODEL_FIELDS = {
    "version", "minute", "samples", "status", "median_bps", "lower_bps",
    "q25_bps", "q75_bps", "upper_bps",
}


class PendingExecutionStateError(ValueError):
    pass


def _coerce_finite(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PendingExecutionStateError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PendingExecutionStateError(
            f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise PendingExecutionStateError(f"{name} must be finite")
    return result


def _finite(name: str, value, *, positive: bool = False) -> float:
    result = _coerce_finite(name, value)
    if positive and result <= 0:
        raise PendingExecutionStateError(f"{name} must be positive")
    if not positive and result < 0:
        raise PendingExecutionStateError(f"{name} must be non-negative")
    return result


def _signed_finite(name: str, value) -> float:
    return _coerce_finite(name, value)


def _optional_finite(name: str, value, *, signed: bool) -> None:
    if value is None:
        return
    if signed:
        _signed_finite(name, value)
    else:
        _finite(name, value)


@dataclass(frozen=True)
class PendingAuditContext:
    reason: str
    signed_residual_bps: Optional[float]
    reference_basis_bps: Optional[float]
    convergence_bps: Optional[float]
    round_trip_fee_bps: Optional[float]
    buy_slippage_budget_bps: Optional[float]
    sell_slippage_budget_bps: Optional[float]
    projected_net_bps: Optional[float]
    projected_net_usd: Optional[float]
    estimated_campaign_pnl_usd: Optional[float]
    entropy_reference_age_ms: Optional[float]
    hedge_reference_age_ms: Optional[float]
    reference_update_skew_ms: Optional[float]
    net_funding_bps_per_hour: Optional[float]
    planned_notional_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise PendingExecutionStateError("audit.reason must not be empty")
        for name in (
                "signed_residual_bps", "reference_basis_bps",
                "projected_net_bps", "projected_net_usd",
                "estimated_campaign_pnl_usd", "net_funding_bps_per_hour"):
            _optional_finite(
                f"audit.{name}", getattr(self, name), signed=True)
        for name in (
                "convergence_bps", "round_trip_fee_bps",
                "buy_slippage_budget_bps", "sell_slippage_budget_bps",
                "entropy_reference_age_ms", "hedge_reference_age_ms",
                "reference_update_skew_ms"):
            _optional_finite(
                f"audit.{name}", getattr(self, name), signed=False)
        _finite(
            "audit.planned_notional_usd", self.planned_notional_usd,
            positive=True)


@dataclass(frozen=True)
class PendingLegState:
    venue_key: str
    is_buy: bool
    order_ref: Optional[str]
    status: str
    filled_base: float
    avg_px: Optional[float]
    applied_fill: float
    unresolved: bool

    def __post_init__(self) -> None:
        if not isinstance(self.venue_key, str) or not self.venue_key:
            raise PendingExecutionStateError("venue_key must not be empty")
        if not isinstance(self.is_buy, bool):
            raise PendingExecutionStateError("is_buy must be boolean")
        if (self.order_ref is not None
                and (not isinstance(self.order_ref, str)
                     or not self.order_ref)):
            raise PendingExecutionStateError(
                "order_ref must be a non-empty string or null")
        if not isinstance(self.status, str) or not self.status:
            raise PendingExecutionStateError("status must not be empty")
        filled = _finite("filled_base", self.filled_base)
        applied = _finite("applied_fill", self.applied_fill)
        if applied > filled + 1e-12:
            raise PendingExecutionStateError(
                "applied_fill must not exceed filled_base")
        if self.avg_px is not None:
            _finite("avg_px", self.avg_px, positive=True)
        if filled > 0 and self.avg_px is None:
            raise PendingExecutionStateError(
                "avg_px is required when filled_base is positive")
        if not isinstance(self.unresolved, bool):
            raise PendingExecutionStateError("unresolved must be boolean")


@dataclass(frozen=True)
class PendingExecutionState:
    execution_id: str
    identity: MarketIdentity
    intent: str
    direction: str
    campaign_id: str
    qty: float
    decided_at: float
    frozen_model: ModelSnapshot
    entry_boundary_bps: float
    exit_target_bps: float
    entropy_expected_px: float
    hedge_expected_px: float
    entropy_fee_bps: float
    hedge_fee_bps: float
    campaign_before: Optional[PositionCampaign]
    buy: PendingLegState
    sell: PendingLegState
    audit_ok: bool
    campaign_applied: bool
    audit: PendingAuditContext
    settled_at: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id:
            raise PendingExecutionStateError("execution_id must not be empty")
        if not isinstance(self.identity, MarketIdentity):
            raise PendingExecutionStateError("identity must be MarketIdentity")
        if self.intent not in {"OPEN", "ADD", "CLOSE", "FORCED_CLOSE"}:
            raise PendingExecutionStateError("intent is invalid")
        if self.direction not in {"buy_entropy", "sell_entropy"}:
            raise PendingExecutionStateError("direction is invalid")
        if not isinstance(self.campaign_id, str) or not self.campaign_id:
            raise PendingExecutionStateError(
                "campaign_id must be a non-empty string")
        _finite("qty", self.qty, positive=True)
        _finite("decided_at", self.decided_at)
        if (not isinstance(self.frozen_model, ModelSnapshot)
                or not self.frozen_model.ready):
            raise PendingExecutionStateError(
                "frozen_model must be a ready ModelSnapshot")
        for name in ("version", "minute", "samples"):
            value = getattr(self.frozen_model, name)
            minimum = 1 if name in {"version", "samples"} else 0
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < minimum):
                raise PendingExecutionStateError(
                    f"frozen_model.{name} must be a valid integer")
        quantiles = [
            _signed_finite(
                f"frozen_model.{name}", getattr(self.frozen_model, name))
            for name in ("lower_bps", "q25_bps", "median_bps",
                         "q75_bps", "upper_bps")
        ]
        if quantiles != sorted(quantiles):
            raise PendingExecutionStateError(
                "frozen_model quantiles must be ordered")
        _signed_finite("entry_boundary_bps", self.entry_boundary_bps)
        _signed_finite("exit_target_bps", self.exit_target_bps)
        _finite("entropy_expected_px", self.entropy_expected_px,
                positive=True)
        _finite("hedge_expected_px", self.hedge_expected_px, positive=True)
        _finite("entropy_fee_bps", self.entropy_fee_bps)
        _finite("hedge_fee_bps", self.hedge_fee_bps)
        if self.intent == "OPEN":
            if self.campaign_before is not None:
                raise PendingExecutionStateError(
                    "campaign_before must be null for OPEN")
        elif (not isinstance(self.campaign_before, PositionCampaign)
              or self.campaign_before.campaign_id != self.campaign_id):
            raise PendingExecutionStateError(
                "campaign_before must match campaign_id for non-OPEN")
        if (self.campaign_before is not None
                and (self.campaign_before.mode != "live"
                     or self.campaign_before.identity != self.identity)):
            raise PendingExecutionStateError(
                "campaign_before must match live market identity")
        if self.intent == "ADD":
            if self.direction != self.campaign_before.direction:
                raise PendingExecutionStateError(
                    "ADD direction must equal campaign_before.direction")
        elif self.intent in {"CLOSE", "FORCED_CLOSE"}:
            if self.direction == self.campaign_before.direction:
                raise PendingExecutionStateError(
                    f"{self.intent} direction must be opposite to "
                    "campaign_before.direction")
        if not isinstance(self.buy, PendingLegState) or not self.buy.is_buy:
            raise PendingExecutionStateError("buy leg is invalid")
        if not isinstance(self.sell, PendingLegState) or self.sell.is_buy:
            raise PendingExecutionStateError("sell leg is invalid")
        if {self.buy.venue_key, self.sell.venue_key} != {"entropy", "hedge"}:
            raise PendingExecutionStateError(
                "pending leg venue keys must equal {entropy, hedge}")
        expected_buy = (
            "entropy" if self.direction == "buy_entropy" else "hedge")
        expected_sell = (
            "hedge" if self.direction == "buy_entropy" else "entropy")
        if (self.buy.venue_key != expected_buy
                or self.sell.venue_key != expected_sell):
            raise PendingExecutionStateError(
                "pending leg venues are inconsistent with direction")
        if (self.buy.filled_base > self.qty + 1e-12
                or self.sell.filled_base > self.qty + 1e-12):
            raise PendingExecutionStateError(
                "pending leg filled_base must not exceed execution qty")
        if not isinstance(self.audit, PendingAuditContext):
            raise PendingExecutionStateError(
                "audit must be PendingAuditContext")
        if self.settled_at is not None:
            settled_at = _finite("settled_at", self.settled_at)
            if settled_at < self.decided_at:
                raise PendingExecutionStateError(
                    "settled_at must not be earlier than decided_at")
        unresolved = self.buy.unresolved or self.sell.unresolved
        if unresolved and self.settled_at is not None:
            raise PendingExecutionStateError(
                "settled_at must be null while a leg is unresolved")
        if not unresolved and self.settled_at is None:
            raise PendingExecutionStateError(
                "settled_at is required when both legs are terminal")
        if not isinstance(self.audit_ok, bool):
            raise PendingExecutionStateError("audit_ok must be boolean")
        if not isinstance(self.campaign_applied, bool):
            raise PendingExecutionStateError(
                "campaign_applied must be boolean")


def pending_execution_path(campaign_path) -> Path:
    campaign = Path(campaign_path)
    return campaign.with_name(
        f"{campaign.stem}.pending{campaign.suffix or '.json'}")


def _leg_from_dict(raw) -> PendingLegState:
    if not isinstance(raw, dict) or set(raw) != _LEG_FIELDS:
        raise PendingExecutionStateError("pending leg fields are incompatible")
    return PendingLegState(**raw)


def _audit_from_dict(raw) -> PendingAuditContext:
    if not isinstance(raw, dict) or set(raw) != _AUDIT_FIELDS:
        raise PendingExecutionStateError(
            "pending audit fields are incompatible")
    return PendingAuditContext(**raw)


def _execution_from_dict(raw) -> PendingExecutionState:
    if not isinstance(raw, dict) or set(raw) != _EXECUTION_FIELDS:
        raise PendingExecutionStateError(
            "pending execution fields are incompatible")
    identity = raw["identity"]
    if not isinstance(identity, dict) or set(identity) != _IDENTITY_FIELDS:
        raise PendingExecutionStateError(
            "pending identity fields are incompatible")
    values = dict(raw)
    try:
        values["identity"] = MarketIdentity(**identity)
    except (TypeError, ValueError) as exc:
        raise PendingExecutionStateError(
            f"pending identity is invalid: {exc}") from exc
    model = raw["frozen_model"]
    if not isinstance(model, dict) or set(model) != _MODEL_FIELDS:
        raise PendingExecutionStateError(
            "pending frozen model fields are incompatible")
    try:
        values["frozen_model"] = ModelSnapshot(**model)
        values["campaign_before"] = (
            None if raw["campaign_before"] is None
            else _campaign_from_dict(raw["campaign_before"]))
        values["buy"] = _leg_from_dict(raw["buy"])
        values["sell"] = _leg_from_dict(raw["sell"])
        values["audit"] = _audit_from_dict(raw["audit"])
        return PendingExecutionState(**values)
    except (TypeError, ValueError) as exc:
        raise PendingExecutionStateError(
            f"pending execution state is invalid: {exc}") from exc


class PendingExecutionStore:
    def __init__(self, path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[PendingExecutionState]:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise PendingExecutionStateError(
                f"cannot read pending execution state {self.path}: {exc}") \
                from exc
        except json.JSONDecodeError as exc:
            raise PendingExecutionStateError(
                f"pending execution state is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != {
                "schema_version", "pending_execution"}:
            raise PendingExecutionStateError(
                "pending execution state envelope is incompatible")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise PendingExecutionStateError(
                f"pending execution state {self.path} has unsupported "
                f"schema_version {raw['schema_version']!r}; manual "
                "verification required")
        pending = raw["pending_execution"]
        return None if pending is None else _execution_from_dict(pending)

    def save(self, pending: Optional[PendingExecutionState]) -> None:
        if pending is not None and not isinstance(
                pending, PendingExecutionState):
            raise PendingExecutionStateError(
                "pending must be PendingExecutionState or None")
        try:
            serialized = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "pending_execution": (
                        None if pending is None else asdict(pending)),
                },
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise PendingExecutionStateError(
                f"pending execution state is not serializable: {exc}") \
                from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                    "x", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp",
                    delete=False, encoding="utf-8") as handle:
                temp_path = Path(handle.name)
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
            temp_path = None
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except OSError as exc:
            raise PendingExecutionStateError(
                f"cannot persist pending execution state {self.path}: {exc}") \
                from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
