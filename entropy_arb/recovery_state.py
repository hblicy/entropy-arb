"""Durable, non-secret evidence for an in-flight dynamic execution."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .strategy import MarketIdentity


SCHEMA_VERSION = 1
_LEG_FIELDS = {
    "venue_key", "is_buy", "order_ref", "status", "filled_base",
    "avg_px", "applied_fill", "unresolved",
}
_EXECUTION_FIELDS = {
    "execution_id", "identity", "intent", "direction", "campaign_id",
    "qty", "buy", "sell", "audit_ok", "campaign_applied",
}
_IDENTITY_FIELDS = {
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue",
}


class PendingExecutionStateError(ValueError):
    pass


def _finite(name: str, value, *, positive: bool = False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise PendingExecutionStateError(f"{name} must be finite")
    result = float(value)
    if positive and result <= 0:
        raise PendingExecutionStateError(f"{name} must be positive")
    if not positive and result < 0:
        raise PendingExecutionStateError(f"{name} must be non-negative")
    return result


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
    campaign_id: Optional[str]
    qty: float
    buy: PendingLegState
    sell: PendingLegState
    audit_ok: bool
    campaign_applied: bool

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not self.execution_id:
            raise PendingExecutionStateError("execution_id must not be empty")
        if not isinstance(self.identity, MarketIdentity):
            raise PendingExecutionStateError("identity must be MarketIdentity")
        if self.intent not in {"OPEN", "ADD", "CLOSE", "FORCED_CLOSE"}:
            raise PendingExecutionStateError("intent is invalid")
        if self.direction not in {"buy_entropy", "sell_entropy"}:
            raise PendingExecutionStateError("direction is invalid")
        if (self.campaign_id is not None
                and (not isinstance(self.campaign_id, str)
                     or not self.campaign_id)):
            raise PendingExecutionStateError(
                "campaign_id must be a non-empty string or null")
        if self.intent != "OPEN" and self.campaign_id is None:
            raise PendingExecutionStateError(
                "campaign_id is required for a non-OPEN execution")
        _finite("qty", self.qty, positive=True)
        if not isinstance(self.buy, PendingLegState) or not self.buy.is_buy:
            raise PendingExecutionStateError("buy leg is invalid")
        if not isinstance(self.sell, PendingLegState) or self.sell.is_buy:
            raise PendingExecutionStateError("sell leg is invalid")
        if self.buy.venue_key == self.sell.venue_key:
            raise PendingExecutionStateError("pending legs must use two venues")
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
    values["buy"] = _leg_from_dict(raw["buy"])
    values["sell"] = _leg_from_dict(raw["sell"])
    return PendingExecutionState(**values)


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
                "unsupported pending execution schema_version "
                f"{raw['schema_version']!r}")
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
