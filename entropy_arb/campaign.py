"""Durable single-campaign state for dynamic residual trading."""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

from .strategy import MarketIdentity, ModelSnapshot


SCHEMA_VERSION = 1


class CampaignInvariantError(ValueError):
    pass


class CampaignStateError(ValueError):
    pass


class CampaignRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class CampaignReconciliation:
    campaign: Optional["PositionCampaign"]
    reason: str = ""


def _finite(name: str, value, *, positive: bool = False,
            nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CampaignInvariantError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise CampaignInvariantError(f"{name} must be finite")
    if positive and result <= 0:
        raise CampaignInvariantError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise CampaignInvariantError(f"{name} must be non-negative")
    return result


@dataclass(frozen=True)
class PositionCampaign:
    campaign_id: str
    mode: str
    identity: MarketIdentity
    direction: str
    opened_at: float
    qty: float
    entropy_avg_px: float
    hedge_avg_px: float
    frozen_model: ModelSnapshot
    entry_boundary_bps: float
    exit_target_bps: float
    fees_usd: float
    realized_pnl_usd: float

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_id, str) or not self.campaign_id:
            raise CampaignInvariantError("campaign_id must not be empty")
        if self.mode not in {"shadow", "live"}:
            raise CampaignInvariantError("mode must be shadow or live")
        if not isinstance(self.identity, MarketIdentity):
            raise CampaignInvariantError("identity must be MarketIdentity")
        if self.direction not in {"sell_entropy", "buy_entropy"}:
            raise CampaignInvariantError("invalid campaign direction")
        _finite("opened_at", self.opened_at, nonnegative=True)
        _finite("qty", self.qty, positive=True)
        _finite("entropy_avg_px", self.entropy_avg_px, positive=True)
        _finite("hedge_avg_px", self.hedge_avg_px, positive=True)
        if (not isinstance(self.frozen_model, ModelSnapshot)
                or not self.frozen_model.ready):
            raise CampaignInvariantError(
                "frozen_model must be a ready ModelSnapshot")
        model = self.frozen_model
        for name in ("version", "minute", "samples"):
            value = getattr(model, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < (1 if name in {"version", "samples"} else 0)):
                raise CampaignInvariantError(
                    f"frozen_model.{name} must be a valid integer")
        quantiles = [
            _finite(f"frozen_model.{name}", getattr(model, name))
            for name in ("lower_bps", "q25_bps", "median_bps",
                         "q75_bps", "upper_bps")
        ]
        if quantiles != sorted(quantiles):
            raise CampaignInvariantError(
                "frozen_model quantiles must be ordered")
        _finite("entry_boundary_bps", self.entry_boundary_bps)
        _finite("exit_target_bps", self.exit_target_bps)
        _finite("fees_usd", self.fees_usd, nonnegative=True)
        _finite("realized_pnl_usd", self.realized_pnl_usd)

    def status_at(self, now: float, *, soft_sec: float,
                  hard_sec: float) -> str:
        now = _finite("now", now, nonnegative=True)
        soft = _finite("soft_sec", soft_sec, positive=True)
        hard = _finite("hard_sec", hard_sec, positive=True)
        if soft >= hard:
            raise CampaignInvariantError("soft_sec must be below hard_sec")
        age = max(now - self.opened_at, 0.0)
        if age >= hard:
            return "HARD_EXIT"
        if age >= soft:
            return "SOFT_EXIT"
        return "OPEN"

    def apply_matched_fill(
            self, *, intent: str, direction: str, qty: float,
            entropy_px: float, hedge_px: float,
            fees_usd: float) -> Optional["PositionCampaign"]:
        if direction != self.direction:
            raise CampaignInvariantError(
                "fill direction does not match campaign direction")
        quantity = _finite("qty", qty, positive=True)
        entropy_price = _finite("entropy_px", entropy_px, positive=True)
        hedge_price = _finite("hedge_px", hedge_px, positive=True)
        fill_fees = _finite("fees_usd", fees_usd, nonnegative=True)
        if intent in {"OPEN", "ADD"}:
            total = self.qty + quantity
            return replace(
                self,
                qty=total,
                entropy_avg_px=(
                    self.entropy_avg_px * self.qty
                    + entropy_price * quantity) / total,
                hedge_avg_px=(
                    self.hedge_avg_px * self.qty
                    + hedge_price * quantity) / total,
                fees_usd=self.fees_usd + fill_fees,
                realized_pnl_usd=self.realized_pnl_usd - fill_fees,
            )
        if intent not in {"CLOSE", "FORCED_CLOSE"}:
            raise CampaignInvariantError(f"unknown intent {intent!r}")
        if quantity > self.qty + 1e-12:
            raise CampaignInvariantError("close quantity exceeds campaign")
        if self.direction == "buy_entropy":
            gross_per_base = (
                entropy_price - self.entropy_avg_px
                + self.hedge_avg_px - hedge_price)
        else:
            gross_per_base = (
                self.entropy_avg_px - entropy_price
                + hedge_price - self.hedge_avg_px)
        remaining = max(self.qty - quantity, 0.0)
        if remaining <= 1e-12:
            return None
        updated = replace(
            self,
            qty=remaining,
            fees_usd=self.fees_usd + fill_fees,
            realized_pnl_usd=(
                self.realized_pnl_usd
                + gross_per_base * quantity - fill_fees),
        )
        return updated


def reconcile_campaign(
        campaign: Optional[PositionCampaign], *,
        entropy_position: float, hedge_position: float,
        step: float, net_tolerance: float) -> CampaignReconciliation:
    """Require durable campaign state to exactly explain both live legs."""
    entropy = _finite("entropy_position", entropy_position)
    hedge = _finite("hedge_position", hedge_position)
    common_step = _finite("step", step, positive=True)
    tolerance = _finite(
        "net_tolerance", net_tolerance, nonnegative=True)
    leg_tolerance = min(common_step / 2.0, max(tolerance, 1e-12))

    if campaign is None:
        if (abs(entropy) <= leg_tolerance
                and abs(hedge) <= leg_tolerance):
            return CampaignReconciliation(None)
        raise CampaignRecoveryError(
            "campaign recovery mismatch: no saved campaign but positions "
            f"are entropy={entropy:+.12g}, hedge={hedge:+.12g}")
    if not isinstance(campaign, PositionCampaign):
        raise ValueError("campaign must be PositionCampaign or None")

    sign = -1.0 if campaign.direction == "sell_entropy" else 1.0
    expected_entropy = sign * campaign.qty
    expected_hedge = -sign * campaign.qty
    entropy_error = entropy - expected_entropy
    hedge_error = hedge - expected_hedge
    net = entropy + hedge
    if (abs(entropy_error) > leg_tolerance
            or abs(hedge_error) > leg_tolerance
            or abs(net) > tolerance):
        raise CampaignRecoveryError(
            "campaign recovery mismatch: "
            f"saved={campaign.direction} qty={campaign.qty:.12g}; "
            f"expected entropy={expected_entropy:+.12g}, "
            f"hedge={expected_hedge:+.12g}; actual "
            f"entropy={entropy:+.12g}, hedge={hedge:+.12g}, net={net:+.12g}")
    return CampaignReconciliation(campaign)


_CAMPAIGN_FIELDS = {
    "campaign_id", "mode", "identity", "direction", "opened_at", "qty",
    "entropy_avg_px", "hedge_avg_px", "frozen_model",
    "entry_boundary_bps", "exit_target_bps", "fees_usd",
    "realized_pnl_usd",
}
_MODEL_FIELDS = {
    "version", "minute", "samples", "status", "median_bps", "lower_bps",
    "q25_bps", "q75_bps", "upper_bps",
}
_IDENTITY_FIELDS = {
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue",
}


def _campaign_from_dict(raw: dict) -> PositionCampaign:
    if not isinstance(raw, dict) or set(raw) != _CAMPAIGN_FIELDS:
        raise CampaignStateError("campaign fields are incompatible")
    identity_raw = raw["identity"]
    model_raw = raw["frozen_model"]
    if (not isinstance(identity_raw, dict)
            or set(identity_raw) != _IDENTITY_FIELDS):
        raise CampaignStateError("campaign identity fields are incompatible")
    if not isinstance(model_raw, dict) or set(model_raw) != _MODEL_FIELDS:
        raise CampaignStateError("frozen model fields are incompatible")
    try:
        return PositionCampaign(
            campaign_id=raw["campaign_id"],
            mode=raw["mode"],
            identity=MarketIdentity(**identity_raw),
            direction=raw["direction"],
            opened_at=raw["opened_at"],
            qty=raw["qty"],
            entropy_avg_px=raw["entropy_avg_px"],
            hedge_avg_px=raw["hedge_avg_px"],
            frozen_model=ModelSnapshot(**model_raw),
            entry_boundary_bps=raw["entry_boundary_bps"],
            exit_target_bps=raw["exit_target_bps"],
            fees_usd=raw["fees_usd"],
            realized_pnl_usd=raw["realized_pnl_usd"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CampaignStateError(f"invalid campaign state: {exc}") from exc


class CampaignStore:
    def __init__(self, path: str, *, shadow: bool) -> None:
        if not isinstance(path, str) or not path.strip():
            raise CampaignStateError("campaign state path must not be empty")
        configured = Path(path)
        if shadow:
            configured = configured.with_name(
                f"{configured.stem}.shadow{configured.suffix}")
        self.path = configured

    def load(self) -> Optional[PositionCampaign]:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise CampaignStateError(
                f"cannot read campaign state {self.path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CampaignStateError(
                f"campaign state is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) != {
                "schema_version", "campaign"}:
            raise CampaignStateError("campaign state envelope is incompatible")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise CampaignStateError(
                f"unsupported campaign schema_version "
                f"{raw['schema_version']!r}")
        campaign = raw["campaign"]
        if campaign is None:
            return None
        return _campaign_from_dict(campaign)

    def save(self, campaign: Optional[PositionCampaign]) -> None:
        if campaign is not None and not isinstance(
                campaign, PositionCampaign):
            raise CampaignStateError(
                "campaign must be PositionCampaign or None")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "campaign": None if campaign is None else asdict(campaign),
        }
        try:
            serialized = json.dumps(
                payload, allow_nan=False, ensure_ascii=False,
                sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CampaignStateError(
                f"campaign state is not serializable: {exc}") from exc
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
            raise CampaignStateError(
                f"cannot persist campaign state {self.path}: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
