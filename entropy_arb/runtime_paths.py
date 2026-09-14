"""Deterministic market-scoped paths for dynamic strategy state."""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Optional, Union

from .strategy import MarketIdentity


PathLike = Union[str, Path]


@dataclass(frozen=True)
class StrategyPaths:
    campaign: Path
    pending: Optional[Path]
    events: Path
    legacy_campaign: Path
    legacy_pending: Optional[Path]


def _safe_piece(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return (cleaned or "market")[:24]


def _with_marker(path: Path, marker: str, *, default_suffix: str = "") -> Path:
    suffix = path.suffix or default_suffix
    return path.with_name(f"{path.stem}.{marker}{suffix}")


def market_scoped_path(path: PathLike, identity: MarketIdentity) -> Path:
    if not isinstance(identity, MarketIdentity):
        raise TypeError("identity must be MarketIdentity")
    canonical = json.dumps(
        asdict(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    readable = "--".join(_safe_piece(value) for value in (
        identity.entropy_dex,
        identity.entropy_symbol,
        identity.hedge_venue,
        identity.hedge_symbol,
    ))
    configured = Path(path)
    return configured.with_name(
        f"{configured.stem}.{readable}-{digest}{configured.suffix}")


def strategy_paths(campaign_path: PathLike, event_path: PathLike,
                   identity: MarketIdentity, *, shadow: bool) -> StrategyPaths:
    configured_campaign = Path(campaign_path)
    scoped_campaign = market_scoped_path(configured_campaign, identity)
    scoped_events = market_scoped_path(event_path, identity)
    if shadow:
        return StrategyPaths(
            campaign=_with_marker(scoped_campaign, "shadow"),
            pending=None,
            events=_with_marker(scoped_events, "shadow"),
            legacy_campaign=_with_marker(configured_campaign, "shadow"),
            legacy_pending=None,
        )
    return StrategyPaths(
        campaign=scoped_campaign,
        pending=_with_marker(
            scoped_campaign, "pending", default_suffix=".json"),
        events=scoped_events,
        legacy_campaign=configured_campaign,
        legacy_pending=_with_marker(
            configured_campaign, "pending", default_suffix=".json"),
    )
