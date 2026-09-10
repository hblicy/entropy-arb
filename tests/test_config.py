"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.config as config_module  # noqa: E402
from entropy_arb.config import (  # noqa: E402
    ConfigError,
    load_config,
    validate_output_paths,
)

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


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh",
         record_only=False, hedge_symbol=None):
    kwargs = {"symbol": symbol, "hedge_venue": hedge}
    if hedge_symbol is not None:
        kwargs["hedge_symbol"] = hedge_symbol
    if record_only:
        kwargs["record_only"] = True
    return load_config(write_tmp(yaml_text), NO_ENV, **kwargs)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.entropy.fee_bps == 0.9
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.recorder_signal_csv == "logs/signals.csv"
    assert cfg.dashboard and cfg.log_file


def test_hedge_symbol_can_differ_from_entropy_symbol():
    cfg = load_config(
        EXAMPLE, NO_ENV, symbol="ANTH", hedge_symbol="ANTHROPIC",
        hedge_venue="lighter-rh")

    assert cfg.symbol == "ANTH"
    assert cfg.entropy.symbol == "ANTH"
    assert cfg.hedge.symbol == "ANTHROPIC"


def test_hedge_symbol_is_stripped():
    cfg = load(MINIMAL, symbol="ANTH", hedge_symbol="  ANTHROPIC  ")

    assert cfg.hedge.symbol == "ANTHROPIC"


@pytest.mark.parametrize("hedge_symbol", ["", "   "])
def test_explicit_blank_hedge_symbol_is_rejected(hedge_symbol):
    with pytest.raises(ConfigError, match="--hedge-symbol"):
        load_config(
            EXAMPLE, NO_ENV, symbol="ANTH", hedge_symbol=hedge_symbol,
            hedge_venue="lighter-rh")


def test_hedge_symbol_rejects_embedded_control_characters():
    with pytest.raises(ConfigError, match="control characters"):
        load(
            MINIMAL, symbol="ANTH",
            hedge_symbol="ANTHROPIC\nforged-market")


def test_example_config_uses_venue_specific_hedge_fee_defaults():
    lighter = load_config(EXAMPLE, NO_ENV,
                          symbol="SNDK", hedge_venue="lighter-rh")
    tradexyz = load_config(EXAMPLE, NO_ENV,
                           symbol="SNDK", hedge_venue="tradexyz")

    assert lighter.hedge.fee_bps == 0.0
    assert tradexyz.hedge.fee_bps == 1.0
    assert tradexyz.entropy.fee_bps + tradexyz.hedge.fee_bps == 1.9


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
    assert cfg.entropy.fee_bps == 0.9
    assert cfg.recorder_enabled is True
    assert cfg.recorder_signal_csv == "logs/signals.csv"
    assert cfg.recorder_signal_rotate_daily is True
    assert cfg.reference_rest_recovery_sec == 15.0
    assert cfg.reference_stale_sec == 60.0
    assert cfg.reference_residual_alert_bps == 20.0
    assert cfg.reference_residual_persist_sec == 30.0


def test_reference_and_signal_rotation_can_be_overridden():
    cfg = load(
        MINIMAL
        + "\nreference:\n"
        + "  rest_recovery_sec: 5\n"
        + "  stale_sec: 30\n"
        + "  residual_alert_bps: 12.5\n"
        + "  residual_persist_sec: 8\n"
        + "recorder:\n"
        + "  signal_rotate_daily: false\n"
    )

    assert cfg.reference_rest_recovery_sec == 5.0
    assert cfg.reference_stale_sec == 30.0
    assert cfg.reference_residual_alert_bps == 12.5
    assert cfg.reference_residual_persist_sec == 8.0
    assert cfg.recorder_signal_rotate_daily is False


def test_recorder_signal_csv_can_be_overridden():
    cfg = load(
        MINIMAL + "\nrecorder:\n  signal_csv: data/custom-signals.csv\n",
    )
    assert cfg.recorder_signal_csv == "data/custom-signals.csv"


def test_recorder_csv_paths_must_be_distinct_after_normalization():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + "  csv: logs/shared.csv\n"
            + "  signal_csv: logs/../logs/shared.csv\n",
            record_only=True,
        )


def test_live_config_allows_unused_signal_path_to_match_minute_path():
    cfg = load(
        MINIMAL
        + "\nrecorder:\n"
        + "  enabled: false\n"
        + "  csv: logs/signals.csv\n"
    )
    assert cfg.recorder_csv == cfg.recorder_signal_csv


def test_live_config_rejects_minute_and_trade_path_collision():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + "  csv: logs/shared.csv\n"
            + "logging:\n"
            + "  trades_csv: ./logs/shared.csv\n"
        )


def test_live_config_rejects_trade_and_log_path_collision():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nlogging:\n"
            + "  trades_csv: logs/shared.csv\n"
            + "  file: logs/../logs/shared.csv\n"
        )


def test_live_config_rejects_output_file_as_parent_of_recorder_path():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + "  csv: logs/out/minutes.csv\n"
            + "logging:\n"
            + "  trades_csv: logs/out\n"
        )


def test_record_only_rejects_output_file_below_another_output_path():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + "  csv: logs/out\n"
            + "  signal_csv: logs/out/signals.csv\n",
            record_only=True,
        )


def test_record_only_rejects_signal_and_log_path_collision():
    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + "  signal_csv: logs/shared.csv\n"
            + "logging:\n"
            + "  file: ./logs/shared.csv\n",
            record_only=True,
        )


def test_headless_config_ignores_inactive_log_file_collision():
    cfg = load(
        MINIMAL
        + "\nlogging:\n"
        + "  dashboard: false\n"
        + "  trades_csv: logs/shared.csv\n"
        + "  file: ./logs/shared.csv\n"
    )

    with pytest.raises(ConfigError, match="must use different paths"):
        validate_output_paths(
            cfg, record_only=False, log_file_active=True)


def test_record_only_rejects_hardlinked_output_files():
    directory = tempfile.mkdtemp()
    minute_path = os.path.join(directory, "minutes.csv")
    signal_path = os.path.join(directory, "signals.csv")
    with open(minute_path, "w", encoding="utf-8") as fh:
        fh.write("existing")
    os.link(minute_path, signal_path)
    minute_yaml = minute_path.replace("\\", "/")
    signal_yaml = signal_path.replace("\\", "/")

    with pytest.raises(ConfigError, match="must use different paths"):
        load(
            MINIMAL
            + "\nrecorder:\n"
            + f'  csv: "{minute_yaml}"\n'
            + f'  signal_csv: "{signal_yaml}"\n',
            record_only=True,
        )


def test_live_config_rejects_existing_directory_as_trade_csv(tmp_path):
    output = tmp_path / "trades.csv"
    output.mkdir()
    output_yaml = str(output).replace("\\", "/")

    with pytest.raises(ConfigError, match="regular file"):
        load(
            MINIMAL
            + "\nlogging:\n"
            + f'  trades_csv: "{output_yaml}"\n'
        )


def test_live_config_rejects_trade_csv_below_file_parent(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    output_yaml = str(blocker / "trades.csv").replace("\\", "/")

    with pytest.raises(ConfigError, match="parent path"):
        load(
            MINIMAL
            + "\nlogging:\n"
            + f'  trades_csv: "{output_yaml}"\n'
        )


def test_live_config_rejects_empty_trade_csv_path():
    with pytest.raises(ConfigError, match="must not be empty"):
        load(MINIMAL + '\nlogging:\n  trades_csv: ""\n')


@pytest.mark.parametrize("name", ["CON.csv", "nul.txt", "COM1", "lpt9.log"])
def test_live_config_rejects_windows_reserved_output_names(name, tmp_path):
    output_yaml = str(tmp_path / name).replace("\\", "/")

    with pytest.raises(ConfigError, match="reserved device"):
        load(MINIMAL + f'\nlogging:\n  trades_csv: "{output_yaml}"\n')


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_output_preflight_rejects_illegal_windows_leaf_name(tmp_path):
    output_yaml = str(tmp_path / "bad<name>.csv").replace("\\", "/")

    with pytest.raises(ConfigError, match="valid Windows path"):
        load(MINIMAL + f'\nlogging:\n  trades_csv: "{output_yaml}"\n')


@pytest.mark.skipif(os.name != "nt", reason="Windows device paths")
def test_output_preflight_rejects_reserved_intermediate_component(tmp_path):
    output_yaml = str(tmp_path / "AUX" / "trades.csv").replace("\\", "/")

    with pytest.raises(ConfigError, match="reserved device"):
        load(MINIMAL + f'\nlogging:\n  trades_csv: "{output_yaml}"\n')


def test_output_preflight_creates_and_tests_actual_parent(tmp_path):
    output = tmp_path / "new" / "nested" / "trades.csv"
    output_yaml = str(output).replace("\\", "/")

    load(MINIMAL + f'\nlogging:\n  trades_csv: "{output_yaml}"\n')

    assert output.parent.is_dir()
    assert output.exists() is False


def test_output_preflight_rejects_existing_file_that_cannot_be_opened_for_append(
        tmp_path, monkeypatch):
    output = tmp_path / "trades.csv"
    output.write_text("existing\n", encoding="utf-8")
    output_yaml = str(output).replace("\\", "/")
    cfg = load_config(
        write_tmp(MINIMAL + f'\nlogging:\n  trades_csv: "{output_yaml}"\n'),
        NO_ENV, symbol="SNDK", hedge_venue="lighter-rh",
        validate_outputs=False)
    real_open = config_module.os.open

    def deny_target(path, flags, *args, **kwargs):
        if os.path.abspath(path) == os.path.abspath(output):
            raise PermissionError("read-only output")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(config_module.os, "open", deny_target)

    with pytest.raises(ConfigError, match="writable"):
        validate_output_paths(
            cfg, record_only=False, log_file_active=False)


def test_symbol_rejects_embedded_control_characters():
    with pytest.raises(ConfigError, match="control characters"):
        load(MINIMAL, symbol="SNDK\nforged-market")


def test_entropy_dex_rejects_embedded_control_characters():
    with pytest.raises(ConfigError, match="control characters"):
        load(MINIMAL + '\nentropy:\n  dex: "io\\nforged-market"\n')


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
    expect_error(MINIMAL + "\nreference:\n  polling_sec: 5\n",
                 "reference.polling_sec")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


@pytest.mark.parametrize("api_key_index", ["1", "255", "-1"])
def test_lighter_api_key_index_must_be_a_signing_key(monkeypatch, api_key_index):
    monkeypatch.setenv("LIGHTER_API_KEY_INDEX", api_key_index)

    with pytest.raises(ConfigError, match="LIGHTER_API_KEY_INDEX"):
        load(MINIMAL, hedge="lighter")


@pytest.mark.parametrize(
    "name", ["LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX"])
def test_lighter_integer_env_reports_config_error(monkeypatch, name):
    monkeypatch.setenv(name, "not-an-integer")

    with pytest.raises(ConfigError, match=name):
        load(MINIMAL, hedge="lighter")


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
    ("reference:\n  rest_recovery_sec: 0\n", "rest_recovery_sec"),
    ("reference:\n  stale_sec: 0\n", "reference.stale_sec"),
    ("reference:\n  residual_alert_bps: -1\n", "residual_alert_bps"),
    ("reference:\n  residual_persist_sec: -1\n", "residual_persist_sec"),
    ("logging:\n  status_interval_sec: 0\n", "status_interval_sec"),
    ("logging:\n  level: LOUD\n", "logging.level"),
])
def test_invalid_runtime_boundaries_are_rejected(section, needle):
    expect_error(MINIMAL + section, needle)


def test_nonfinite_numbers_are_rejected():
    expect_error(MINIMAL + "sizing:\n  max_order_notional_usd: .nan\n",
                 "finite")
    expect_error(MINIMAL + "reference:\n  residual_alert_bps: .nan\n",
                 "finite")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
