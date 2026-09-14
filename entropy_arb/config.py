"""Configuration: strategy from a YAML file, credentials from .env, market
selection (Entropy symbol + optional hedge symbol + hedge venue) from the
command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
optional --hedge-symbol, --hedge). Every YAML key is validated against the
schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (entropy_price / hedge_price - 1) * 10_000

    SELL entropy / BUY hedge  fires when the executable premium
        (entropy bid over hedge ask) >= midline_bps + upper_bps
    BUY entropy / SELL hedge  fires when the executable premium
        (entropy ask under hedge bid) <= midline_bps - lower_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import math
import os
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

from .runtime_paths import strategy_paths
from .strategy import MarketIdentity

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

HEDGE_VENUES = ("lighter", "lighter-rh", "tradexyz")


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "entropy" | "hedge"
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass
class Config:
    symbol: str
    hedge_venue: str
    entropy: VenueConf
    hedge: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    recorder_signal_csv: str
    recorder_signal_rotate_daily: bool
    # reference observation
    reference_rest_recovery_sec: float
    reference_stale_sec: float
    reference_residual_alert_bps: float
    reference_residual_persist_sec: float
    # strategy selection and dynamic residual model
    strategy_mode: str
    strategy_live_enabled: bool
    strategy_window_minutes: int
    strategy_min_samples: int
    strategy_lower_quantile: float
    strategy_upper_quantile: float
    strategy_regime_window_minutes: int
    strategy_regime_recovery_minutes: int
    strategy_exit_band_fraction: float
    strategy_min_exit_band_bps: float
    strategy_min_expected_profit_bps: float
    strategy_soft_hold_minutes: int
    strategy_hard_hold_minutes: int
    strategy_entry_reference_max_age_sec: float
    strategy_entry_reference_max_skew_sec: float
    strategy_state_file: str
    strategy_event_csv: str
    # dynamic slippage
    slippage_bootstrap_bps: float
    slippage_min_bps: float
    slippage_safety_bps: float
    slippage_hard_max_bps: float
    slippage_max_edge_fraction: float
    slippage_min_live_samples: int
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.entropy, self.hedge):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "entropy": {
        "dex": str,
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "hedge": {
        "taker_fee_bps": float,
        "max_position_usd": float,
        "max_orders_per_min": int,
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
        "signal_csv": str,
        "signal_rotate_daily": bool,
    },
    "reference": {
        "rest_recovery_sec": float,
        "stale_sec": float,
        "residual_alert_bps": float,
        "residual_persist_sec": float,
    },
    "strategy": {
        "mode": str,
        "live_enabled": bool,
        "window_minutes": int,
        "min_samples": int,
        "lower_quantile": float,
        "upper_quantile": float,
        "regime_window_minutes": int,
        "regime_recovery_minutes": int,
        "exit_band_fraction": float,
        "min_exit_band_bps": float,
        "min_expected_profit_bps": float,
        "soft_hold_minutes": int,
        "hard_hold_minutes": int,
        "entry_reference_max_age_sec": float,
        "entry_reference_max_skew_sec": float,
        "state_file": str,
        "event_csv": str,
    },
    "slippage": {
        "bootstrap_bps": float,
        "min_bps": float,
        "safety_bps": float,
        "hard_max_bps": float,
        "max_edge_fraction": float,
        "min_live_samples": int,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except ValueError:
        raise ConfigError(
            f"{name} must be an integer / 环境变量必须是整数") from None


# -------------------------------------------------------------------- loading

def _normalized_output_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _same_output_file(left: str, right: str) -> bool:
    left_path = _normalized_output_path(left)
    right_path = _normalized_output_path(right)
    if left_path == right_path:
        return True
    try:
        return (os.path.exists(left_path) and os.path.exists(right_path)
                and os.path.samefile(left_path, right_path))
    except OSError:
        return False


def _output_paths_conflict(left: str, right: str) -> bool:
    if _same_output_file(left, right):
        return True
    left_path = _normalized_output_path(left)
    right_path = _normalized_output_path(right)
    try:
        common = os.path.commonpath((left_path, right_path))
    except ValueError:
        return False  # e.g. different Windows drive letters
    return common == left_path or common == right_path


_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"}
_WINDOWS_RESERVED_NAMES.update(f"COM{i}" for i in range(1, 10))
_WINDOWS_RESERVED_NAMES.update(f"LPT{i}" for i in range(1, 10))


def _validate_identity(name: str, value: str) -> None:
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ConfigError(
            f"{name} must not contain control characters / 标识不得包含控制字符")


def _validate_windows_path_components(name: str, absolute: str) -> None:
    _drive, tail = os.path.splitdrive(absolute)
    for component in tail.replace("\\", "/").split("/"):
        if not component:
            continue
        normalized = component.rstrip(" .")
        device_stem = normalized.split(".", 1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise ConfigError(
                f"{name} uses a reserved device name / 输出路径使用了保留设备名")
        if os.name == "nt" and (
                normalized != component
                or any(char in '<>:"|?*' for char in component)):
            raise ConfigError(
                f"{name} is not a valid Windows path / 输出路径在 Windows 上无效")


def _validate_output_target(name: str, path: str) -> None:
    if not path or not path.strip():
        raise ConfigError(
            f"{name} must not be empty / 输出文件路径不能为空")
    absolute = os.path.abspath(path)
    _validate_windows_path_components(name, absolute)
    if os.path.lexists(absolute) and not os.path.isfile(absolute):
        raise ConfigError(
            f"{name} must be a regular file path / 必须指向普通文件")

    target_parent = os.path.dirname(absolute)
    ancestor = target_parent
    while not os.path.lexists(ancestor):
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            break
        ancestor = parent
    if not os.path.isdir(ancestor):
        raise ConfigError(
            f"{name} parent path must be a directory / 父路径必须是目录")
    created = False
    descriptor = None
    try:
        os.makedirs(target_parent, exist_ok=True)
        if os.path.lexists(absolute):
            descriptor = os.open(absolute, os.O_WRONLY | os.O_APPEND)
        else:
            try:
                descriptor = os.open(
                    absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                descriptor = os.open(absolute, os.O_WRONLY | os.O_APPEND)
        os.close(descriptor)
        descriptor = None
        if created:
            os.unlink(absolute)
            created = False
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(absolute)
            except OSError:
                pass
        raise ConfigError(
            f"{name} must be writable / 输出文件或其父目录不可写") from exc


def validate_output_paths(cfg: Config, *, record_only: bool,
                          log_file_active: bool) -> None:
    if record_only:
        outputs = [
            ("recorder.csv", cfg.recorder_csv),
            ("recorder.signal_csv", cfg.recorder_signal_csv),
        ]
    else:
        outputs = [("logging.trades_csv", cfg.trades_csv)]
        if cfg.recorder_enabled:
            outputs.append(("recorder.csv", cfg.recorder_csv))
    if log_file_active:
        outputs.append(("logging.file", cfg.log_file))
    if cfg.strategy_mode == "residual_dynamic":
        identity = MarketIdentity(
            cfg.entropy.symbol,
            cfg.entropy.hl_dex,
            cfg.hedge.symbol,
            cfg.hedge_venue,
        )
        paths = strategy_paths(
            cfg.strategy_state_file,
            cfg.strategy_event_csv,
            identity,
            shadow=record_only,
        )
        outputs.extend((
            ("strategy.state_file", str(paths.campaign)),
            ("strategy.event_csv", str(paths.events)),
        ))
        if paths.pending is not None:
            outputs.append(("strategy.pending_file", str(paths.pending)))
    for name, path in outputs:
        if not path or not path.strip():
            raise ConfigError(
                f"{name} must not be empty / 输出文件路径不能为空")
    for index, (left_name, left_path) in enumerate(outputs):
        for right_name, right_path in outputs[index + 1:]:
            if _output_paths_conflict(left_path, right_path):
                raise ConfigError(
                    f"{left_name} and {right_name} must use different "
                    "paths / 各输出文件路径必须不同")
    for name, path in outputs:
        _validate_output_target(name, path)


def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                 symbol: str, hedge_venue: str,
                 hedge_symbol: Optional[str] = None,
                 record_only: bool = False,
                 validate_outputs: bool = True) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    _validate_identity("--symbol", symbol)
    effective_hedge_symbol = (
        symbol if hedge_symbol is None else hedge_symbol.strip())
    if not effective_hedge_symbol:
        raise ConfigError("--hedge-symbol must not be empty / "
                          "--hedge-symbol 不得为空")
    _validate_identity("--hedge-symbol", effective_hedge_symbol)
    if hedge_venue not in HEDGE_VENUES:
        raise ConfigError(
            f"--hedge must be one of {list(HEDGE_VENUES)}, got "
            f"{hedge_venue!r} / --hedge 必须是 {list(HEDGE_VENUES)} 之一")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    midline = float(thr["midline_bps"])
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if not all(math.isfinite(v) for v in (midline, upper, lower)):
        raise ConfigError("threshold values must be finite numbers")
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not math.isfinite(take_fraction) or not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    entropy_dex = _get(raw, "entropy", "dex", "io")
    _validate_identity("entropy.dex", entropy_dex)
    if hedge_venue == "tradexyz" and entropy_dex == "xyz":
        raise ConfigError("entropy.dex 'xyz' with hedge_venue 'tradexyz' is "
                          "the same market on both legs / 两条腿是同一个市场")

    entropy_hl_creds = HLCreds(_env_s("HL_PRIVATE_KEY"),
                               _env_s("HL_ACCOUNT_ADDRESS"))
    entropy = VenueConf(
        key="entropy", kind="hl", label="ENTROPY",
        symbol=symbol,
        fee_bps=float(_get(raw, "entropy", "taker_fee_bps", 0.9)),
        cap_usd=float(_get(raw, "entropy", "max_position_usd", 1000.0)),
        orders_per_min=int(_get(raw, "entropy", "max_orders_per_min", 120)),
        hl_dex=entropy_dex,
        hl_creds=entropy_hl_creds,
    )

    if hedge_venue == "tradexyz":
        hedge = VenueConf(
            key="hedge", kind="hl", label="XYZ",
            symbol=effective_hedge_symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 1.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 120)),
            hl_dex="xyz",
            hl_creds=HLCreds(
                _env_s("HL_PRIVATE_KEY_XYZ") or _env_s("HL_PRIVATE_KEY"),
                _env_s("HL_ACCOUNT_ADDRESS_XYZ") or _env_s("HL_ACCOUNT_ADDRESS")),
        )
    else:
        hedge = VenueConf(
            key="hedge", kind="lighter",
            label="LIGHTER" if hedge_venue == "lighter" else "RH",
            symbol=effective_hedge_symbol,
            fee_bps=float(_get(raw, "hedge", "taker_fee_bps", 0.0)),
            cap_usd=float(_get(raw, "hedge", "max_position_usd", 1000.0)),
            orders_per_min=int(_get(raw, "hedge", "max_orders_per_min", 30)),
            lighter_profile=LIGHTER_PROFILES[hedge_venue],
            lighter_creds=LighterCreds(_env_i("LIGHTER_ACCOUNT_INDEX"),
                                       _env_i("LIGHTER_API_KEY_INDEX"),
                                       _env_s("LIGHTER_API_PRIVATE_KEY")),
        )

    cfg = Config(
        symbol=symbol,
        hedge_venue=hedge_venue,
        entropy=entropy,
        hedge=hedge,
        midline_bps=midline,
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=_get(raw, "recorder", "csv", "logs/minutes.csv"),
        recorder_signal_csv=_get(
            raw, "recorder", "signal_csv", "logs/signals.csv"),
        recorder_signal_rotate_daily=bool(_get(
            raw, "recorder", "signal_rotate_daily", True)),
        reference_rest_recovery_sec=float(_get(
            raw, "reference", "rest_recovery_sec", 15.0)),
        reference_stale_sec=float(_get(
            raw, "reference", "stale_sec", 60.0)),
        reference_residual_alert_bps=float(_get(
            raw, "reference", "residual_alert_bps", 20.0)),
        reference_residual_persist_sec=float(_get(
            raw, "reference", "residual_persist_sec", 30.0)),
        strategy_mode=str(_get(
            raw, "strategy", "mode", "fixed_premium")),
        strategy_live_enabled=bool(_get(
            raw, "strategy", "live_enabled", False)),
        strategy_window_minutes=int(_get(
            raw, "strategy", "window_minutes", 180)),
        strategy_min_samples=int(_get(
            raw, "strategy", "min_samples", 120)),
        strategy_lower_quantile=float(_get(
            raw, "strategy", "lower_quantile", 0.10)),
        strategy_upper_quantile=float(_get(
            raw, "strategy", "upper_quantile", 0.90)),
        strategy_regime_window_minutes=int(_get(
            raw, "strategy", "regime_window_minutes", 60)),
        strategy_regime_recovery_minutes=int(_get(
            raw, "strategy", "regime_recovery_minutes", 15)),
        strategy_exit_band_fraction=float(_get(
            raw, "strategy", "exit_band_fraction", 0.25)),
        strategy_min_exit_band_bps=float(_get(
            raw, "strategy", "min_exit_band_bps", 0.5)),
        strategy_min_expected_profit_bps=float(_get(
            raw, "strategy", "min_expected_profit_bps", 2.0)),
        strategy_soft_hold_minutes=int(_get(
            raw, "strategy", "soft_hold_minutes", 60)),
        strategy_hard_hold_minutes=int(_get(
            raw, "strategy", "hard_hold_minutes", 360)),
        strategy_entry_reference_max_age_sec=float(_get(
            raw, "strategy", "entry_reference_max_age_sec", 15.0)),
        strategy_entry_reference_max_skew_sec=float(_get(
            raw, "strategy", "entry_reference_max_skew_sec", 15.0)),
        strategy_state_file=str(_get(
            raw, "strategy", "state_file", "logs/campaign-state.json")),
        strategy_event_csv=str(_get(
            raw, "strategy", "event_csv", "logs/strategy-events.csv")),
        slippage_bootstrap_bps=float(_get(
            raw, "slippage", "bootstrap_bps", 5.0)),
        slippage_min_bps=float(_get(
            raw, "slippage", "min_bps", 1.0)),
        slippage_safety_bps=float(_get(
            raw, "slippage", "safety_bps", 1.0)),
        slippage_hard_max_bps=float(_get(
            raw, "slippage", "hard_max_bps", 20.0)),
        slippage_max_edge_fraction=float(_get(
            raw, "slippage", "max_edge_fraction", 0.25)),
        slippage_min_live_samples=int(_get(
            raw, "slippage", "min_live_samples", 10)),
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
    )

    if validate_outputs:
        validate_output_paths(
            cfg, record_only=record_only, log_file_active=cfg.dashboard)

    nonnegative = (
        ("entropy.taker_fee_bps", cfg.entropy.fee_bps),
        ("hedge.taker_fee_bps", cfg.hedge.fee_bps),
        ("inventory.scale_bps", cfg.inventory_scale_bps),
        ("execution.premium_persist_sec", cfg.premium_persist_sec),
        ("execution.cooldown_sec", cfg.cooldown_sec),
        ("execution.leg_slippage_bps", cfg.leg_slippage_bps),
        ("execution.hedge_slippage_bps", cfg.hedge_slippage_bps),
        ("execution.net_tolerance_base", cfg.net_tolerance_base),
        ("execution.rate_limit_pause_sec", cfg.rate_limit_pause_sec),
        ("execution.http_keepalive_sec", cfg.http_keepalive_sec),
        ("reference.residual_alert_bps",
         cfg.reference_residual_alert_bps),
        ("reference.residual_persist_sec",
         cfg.reference_residual_persist_sec),
        ("strategy.min_exit_band_bps", cfg.strategy_min_exit_band_bps),
        ("strategy.min_expected_profit_bps",
         cfg.strategy_min_expected_profit_bps),
        ("slippage.bootstrap_bps", cfg.slippage_bootstrap_bps),
        ("slippage.min_bps", cfg.slippage_min_bps),
        ("slippage.safety_bps", cfg.slippage_safety_bps),
        ("slippage.hard_max_bps", cfg.slippage_hard_max_bps),
    )
    positive = (
        ("entropy.max_position_usd", cfg.entropy.cap_usd),
        ("hedge.max_position_usd", cfg.hedge.cap_usd),
        ("entropy.max_orders_per_min", cfg.entropy.orders_per_min),
        ("hedge.max_orders_per_min", cfg.hedge.orders_per_min),
        ("sizing.max_order_notional_usd", cfg.max_order_notional),
        ("sizing.min_order_notional_usd", cfg.min_order_notional),
        ("execution.settle_timeout_sec", cfg.settle_timeout_sec),
        ("execution.max_consecutive_errors", cfg.max_consecutive_errors),
        ("execution.staleness_sec", cfg.staleness_sec),
        ("execution.reconcile_sec", cfg.reconcile_sec),
        ("execution.venue_probe_sec", cfg.venue_probe_sec),
        ("reference.rest_recovery_sec", cfg.reference_rest_recovery_sec),
        ("reference.stale_sec", cfg.reference_stale_sec),
        ("strategy.window_minutes", cfg.strategy_window_minutes),
        ("strategy.min_samples", cfg.strategy_min_samples),
        ("strategy.regime_window_minutes",
         cfg.strategy_regime_window_minutes),
        ("strategy.regime_recovery_minutes",
         cfg.strategy_regime_recovery_minutes),
        ("strategy.exit_band_fraction",
         cfg.strategy_exit_band_fraction),
        ("strategy.soft_hold_minutes", cfg.strategy_soft_hold_minutes),
        ("strategy.hard_hold_minutes", cfg.strategy_hard_hold_minutes),
        ("strategy.entry_reference_max_age_sec",
         cfg.strategy_entry_reference_max_age_sec),
        ("strategy.entry_reference_max_skew_sec",
         cfg.strategy_entry_reference_max_skew_sec),
        ("slippage.max_edge_fraction", cfg.slippage_max_edge_fraction),
        ("slippage.min_live_samples", cfg.slippage_min_live_samples),
        ("logging.status_interval_sec", cfg.status_interval_sec),
    )
    for name, value in nonnegative:
        if not math.isfinite(value):
            raise ConfigError(f"'{name}' must be finite, got {value!r}")
        if value < 0:
            raise ConfigError(f"'{name}' must be >= 0, got {value!r}")
    for name, value in positive:
        if not math.isfinite(value):
            raise ConfigError(f"'{name}' must be finite, got {value!r}")
        if value <= 0:
            raise ConfigError(f"'{name}' must be > 0, got {value!r}")
    if cfg.min_order_notional > cfg.max_order_notional:
        raise ConfigError("'sizing.min_order_notional_usd' must be <= "
                          "'sizing.max_order_notional_usd'")
    if cfg.strategy_mode not in {"fixed_premium", "residual_dynamic"}:
        raise ConfigError("'strategy.mode' must be fixed_premium or "
                          "residual_dynamic")
    if not (math.isfinite(cfg.strategy_lower_quantile)
            and math.isfinite(cfg.strategy_upper_quantile)
            and 0 <= cfg.strategy_lower_quantile < 0.5
            < cfg.strategy_upper_quantile <= 1):
        raise ConfigError("strategy quantiles must satisfy "
                          "0 <= lower < 0.5 < upper <= 1")
    if cfg.strategy_min_samples > cfg.strategy_window_minutes:
        raise ConfigError("'strategy.min_samples' must be <= window_minutes")
    if cfg.strategy_regime_window_minutes > cfg.strategy_window_minutes:
        raise ConfigError("'strategy.regime_window_minutes' must be <= "
                          "window_minutes")
    if cfg.strategy_soft_hold_minutes >= cfg.strategy_hard_hold_minutes:
        raise ConfigError("'strategy.soft_hold_minutes' must be < "
                          "hard_hold_minutes")
    if cfg.strategy_exit_band_fraction > 1:
        raise ConfigError("'strategy.exit_band_fraction' must be <= 1")
    if cfg.slippage_min_bps > cfg.slippage_hard_max_bps:
        raise ConfigError("'slippage.min_bps' must be <= hard_max_bps")
    if cfg.slippage_max_edge_fraction > 1:
        raise ConfigError("'slippage.max_edge_fraction' must be <= 1")
    if cfg.slippage_min_live_samples > 50:
        raise ConfigError("'slippage.min_live_samples' must be <= 50")
    if (cfg.strategy_mode == "residual_dynamic"
            and cfg.strategy_live_enabled
            and cfg.premium_persist_sec <= 0):
        raise ConfigError("residual live requires "
                          "execution.premium_persist_sec > 0")
    if (cfg.strategy_mode == "residual_dynamic"
            and not record_only and not cfg.strategy_live_enabled):
        raise ConfigError("residual live requires "
                          "strategy.live_enabled=true")
    for name, path in (
            ("strategy.state_file", cfg.strategy_state_file),
            ("strategy.event_csv", cfg.strategy_event_csv)):
        if not path.strip():
            raise ConfigError(f"'{name}' must not be empty")
    if not math.isfinite(cfg.inventory_floor_frac) \
            or not 0 <= cfg.inventory_floor_frac < 1:
        raise ConfigError("'inventory.floor_frac' must be in [0, 1)")
    if cfg.log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
        raise ConfigError("'logging.level' must be one of CRITICAL, ERROR, "
                          "WARNING, INFO, DEBUG")
    if cfg.hedge.kind == "lighter":
        api_key_index = cfg.hedge.lighter_creds.api_key_index
        if api_key_index is not None and not 2 <= api_key_index <= 254:
            raise ConfigError(
                "LIGHTER_API_KEY_INDEX must be in [2, 254] / "
                "Lighter 签名密钥索引必须在 [2, 254] 范围内")
    return cfg
