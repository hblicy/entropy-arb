"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.recorder_signal_csv == "logs/signals.csv"
    assert cfg.dashboard and cfg.log_file


def test_utf8_config_loads_independently_of_system_locale():
    cfg = load("""
# 中文配置注释必须在 Windows 和 Linux 上一致读取
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
""")
    assert cfg.symbol == "SNDK"
    assert cfg.midline_bps == 5.0


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True
    assert cfg.recorder_signal_csv == "logs/signals.csv"


def test_recorder_signal_csv_can_be_overridden():
    cfg = load(
        MINIMAL + "\nrecorder:\n  signal_csv: data/custom-signals.csv\n",
    )
    assert cfg.recorder_signal_csv == "data/custom-signals.csv"


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


@pytest.mark.parametrize(("section", "needle"), [
    ("entropy:\n  taker_fee_bps: -1\n", "entropy.taker_fee_bps"),
    ("entropy:\n  max_position_usd: 0\n", "entropy.max_position_usd"),
    ("hedge:\n  max_orders_per_min: 0\n", "hedge.max_orders_per_min"),
    ("sizing:\n  max_order_notional_usd: 0\n", "sizing.max_order_notional_usd"),
    ("sizing:\n  max_order_notional_usd: 10\n"
     "  min_order_notional_usd: 11\n", "min_order_notional_usd"),
    ("inventory:\n  scale_bps: -1\n", "inventory.scale_bps"),
    ("inventory:\n  floor_frac: 1\n", "inventory.floor_frac"),
    ("execution:\n  premium_persist_sec: -1\n", "premium_persist_sec"),
    ("execution:\n  settle_timeout_sec: 0\n", "settle_timeout_sec"),
    ("execution:\n  leg_slippage_bps: -1\n", "leg_slippage_bps"),
    ("execution:\n  net_tolerance_base: -1\n", "net_tolerance_base"),
    ("execution:\n  max_consecutive_errors: 0\n", "max_consecutive_errors"),
    ("execution:\n  staleness_sec: 0\n", "staleness_sec"),
    ("execution:\n  reconcile_sec: 0\n", "reconcile_sec"),
    ("execution:\n  venue_probe_sec: 0\n", "venue_probe_sec"),
    ("execution:\n  http_keepalive_sec: -1\n", "http_keepalive_sec"),
    ("logging:\n  status_interval_sec: 0\n", "status_interval_sec"),
    ("logging:\n  level: LOUD\n", "logging.level"),
])
def test_invalid_runtime_boundaries_are_rejected(section, needle):
    expect_error(MINIMAL + section, needle)


def test_nonfinite_numbers_are_rejected():
    expect_error(MINIMAL + "sizing:\n  max_order_notional_usd: .nan\n",
                 "finite")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
