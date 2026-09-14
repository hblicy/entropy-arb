import csv
import gzip
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.recorder import SIGNAL_HEADER  # noqa: E402
from tools.replay_strategy import replay_files  # noqa: E402


MINUTE_HEADER = [
    "minute_ts", "entropy_symbol", "entropy_dex", "hedge_symbol",
    "hedge_venue", "entropy_reference_age_ms", "hedge_reference_age_ms",
    "reference_update_skew_ms", "residual_close_bps",
]


def write_minutes(path, residuals):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(MINUTE_HEADER)
        for minute, residual in enumerate(residuals):
            writer.writerow([
                minute * 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
                100, 100, 0, residual,
            ])
    return path


def signal_row(ts_ms, direction, residual, event_id):
    hedge_bid, hedge_ask = 99.99, 100.0
    entropy_bid = hedge_ask * (1 + residual / 1e4)
    entropy_ask = entropy_bid + 0.01
    row = {field: "" for field in SIGNAL_HEADER}
    row.update({
        "ts_ms": ts_ms,
        "time_utc": "1970-01-01T00:00:00Z",
        "entropy_symbol": "ANTH",
        "entropy_dex": "io",
        "hedge_symbol": "ANTHROPIC",
        "hedge_venue": "lighter-rh",
        "event_id": event_id,
        "event": "sample",
        "direction": direction,
        "entropy_bid": entropy_bid,
        "entropy_ask": entropy_ask,
        "hedge_bid": hedge_bid,
        "hedge_ask": hedge_ask,
        "entropy_book_age_ms": 100,
        "hedge_book_age_ms": 100,
        "entropy_oracle_px": 100,
        "hedge_index_px": 100,
        "entropy_reference_age_ms": 100,
        "hedge_reference_age_ms": 100,
        "reference_update_skew_ms": 0,
        "planned_notional_usd": 1000,
        "crossable_notional_usd": 1000,
    })
    return row


def write_signals(path, rows):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SIGNAL_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def replay_config(**overrides):
    values = dict(
        strategy_window_minutes=10,
        strategy_min_samples=4,
        strategy_lower_quantile=.10,
        strategy_upper_quantile=.90,
        strategy_regime_window_minutes=2,
        strategy_regime_recovery_minutes=1,
        strategy_entry_reference_max_age_sec=15.0,
        strategy_entry_reference_max_skew_sec=15.0,
        strategy_exit_band_fraction=.25,
        strategy_min_exit_band_bps=.5,
        strategy_min_expected_profit_bps=2.0,
        strategy_soft_hold_minutes=60,
        strategy_hard_hold_minutes=360,
        slippage_bootstrap_bps=5.0,
        slippage_min_bps=1.0,
        slippage_safety_bps=1.0,
        slippage_hard_max_bps=20.0,
        slippage_max_edge_fraction=.25,
        slippage_min_live_samples=10,
        max_order_notional=500.0,
        min_order_notional=10.0,
        take_fraction=.5,
        staleness_sec=5.0,
        premium_persist_sec=0.0,
        cooldown_sec=0.0,
        entropy=SimpleNamespace(fee_bps=.9, cap_usd=1000.0),
        hedge=SimpleNamespace(fee_bps=0.0, cap_usd=1000.0),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_replay_merges_gzip_signals_and_minutes_chronologically(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    first = write_signals(tmp_path / "signals-1.csv.gz", [
        signal_row(300_000, "buy_entropy", 30, "b1"),
        signal_row(301_000, "buy_entropy", 31, "b2"),
    ])
    second_rows = [
        signal_row(302_000, "sell_entropy", 45, "s1"),
        signal_row(303_000, "sell_entropy", 10, "s2"),
    ]
    second = write_signals(tmp_path / "signals-2.csv.gz", second_rows)
    # An exact duplicate across rotated/current files must be counted once.
    duplicate = write_signals(
        tmp_path / "signals-duplicate.csv.gz", [second_rows[0]])

    result = replay_files(
        minutes_path=str(minutes),
        signal_paths=[str(second), str(first), str(duplicate)],
        config=replay_config(),
        now_ts=400.0,
    )

    assert result.signal_rows == 4
    assert result.timestamps_monotonic
    assert result.approximation == (
        "threshold-censored legacy top-of-book approximation")


def test_replay_marks_legacy_signal_timeline_as_censored(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv", [
        signal_row(300_000, "sell_entropy", 45, "legacy-start")])

    result = replay_files(
        minutes_path=str(minutes),
        signal_paths=[str(signals)],
        config=replay_config(),
        now_ts=400,
    )

    assert result.timeline_complete is False
    assert result.coverage_end_ts == pytest.approx(300)
    assert result.requested_end_ts == pytest.approx(400)
    assert "threshold-censored legacy" in result.approximation


def test_replay_uses_snapshot_coverage_end_instead_of_now_ts(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    snapshot = signal_row(360_000, "sell_entropy", 45, "snapshot-1")
    snapshot.update(event="snapshot", direction="")
    signals = write_signals(tmp_path / "signals.csv", [snapshot])

    result = replay_files(
        minutes_path=str(minutes),
        signal_paths=[str(signals)],
        config=replay_config(),
        now_ts=600,
    )

    assert result.timeline_complete is False
    assert result.coverage_end_ts == pytest.approx(360)
    assert result.requested_end_ts == pytest.approx(600)
    assert result.raw_buy_coverage == 0
    assert result.raw_sell_coverage == 0


def test_replay_snapshot_timeline_is_complete_through_requested_end(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    snapshots = []
    for timestamp in (360_000, 361_000):
        row = signal_row(
            timestamp,
            "sell_entropy",
            45,
            f"snapshot-{timestamp}",
        )
        row.update(event="snapshot", direction="")
        snapshots.append(row)
    signals = write_signals(tmp_path / "signals.csv", snapshots)

    result = replay_files(
        minutes_path=str(minutes),
        signal_paths=[str(signals)],
        config=replay_config(),
        now_ts=361,
    )

    assert result.timeline_complete is True
    assert result.approximation == "continuous top-of-book snapshot approximation"


def test_replay_rejects_snapshot_with_direction(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    snapshot = signal_row(360_000, "sell_entropy", 45, "snapshot-1")
    snapshot["event"] = "snapshot"
    signals = write_signals(tmp_path / "signals.csv", [snapshot])

    with pytest.raises(ValueError, match="snapshot direction"):
        replay_files(
            minutes_path=str(minutes),
            signal_paths=[str(signals)],
            config=replay_config(),
            now_ts=400,
        )


def test_replay_reports_coverage_and_campaign_invariants(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-4, -2, 2, 4])
    rows = [
        signal_row(300_000 + index * 1000, "buy_entropy", 0, f"b{index}")
        for index in range(20)
    ]
    signals = write_signals(tmp_path / "signals.csv.gz", rows)

    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(), now_ts=400.0)

    assert result.raw_buy_coverage > .95
    assert result.residual_open_coverage < .20
    assert result.reverse_campaigns == 0
    assert result.max_planned_leg_notional <= 500
    assert result.max_slippage_budget_bps <= 20
    assert result.invalid_reference_entries == 0


def test_replay_requires_entry_persistence(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv.gz", [
        signal_row(300_000, "sell_entropy", 45, "s1"),
        signal_row(302_000, "sell_entropy", 45, "s2"),
    ])

    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(premium_persist_sec=3.0), now_ts=400.0)

    assert result.campaigns_opened == 0
    assert result.actions == 0


def test_replay_applies_action_cooldown(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv.gz", [
        signal_row(300_000 + index * 1000, "sell_entropy", 45, f"s{index}")
        for index in range(10)
    ])

    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(cooldown_sec=60.0), now_ts=400.0)

    assert result.campaigns_opened == 1
    assert result.actions == 1


def test_replay_never_exceeds_accumulated_position_cap(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv.gz", [
        signal_row(300_000 + index * 1000, "sell_entropy", 45, f"s{index}")
        for index in range(20)
    ])

    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(), now_ts=400.0)

    assert result.max_accumulated_leg_notional <= 1000.0 + 1e-6


def test_replay_inserts_missing_minutes_before_signal(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv.gz", [
        signal_row(540_000, "sell_entropy", 45, "after-gap"),
    ])

    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(), now_ts=700.0)

    assert result.campaigns_opened == 0
    assert result.actions == 0
    assert result.entries_during_unstable_gap == 0


def test_replay_rejects_mixed_signal_pair_identity(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-4, -2, 2, 4])
    rows = [signal_row(300_000, "buy_entropy", 0, "b1")]
    mixed = signal_row(301_000, "buy_entropy", 0, "b2")
    mixed["hedge_symbol"] = "SNDK"
    signals = write_signals(tmp_path / "signals.csv.gz", rows + [mixed])

    with pytest.raises(ValueError, match="multiple markets"):
        replay_files(
            minutes_path=str(minutes), signal_paths=[str(signals)],
            config=replay_config(), now_ts=400.0)


def test_replay_cli_labels_top_of_book_approximation(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv.gz", [
        signal_row(300_000, "sell_entropy", 45, "s1")])

    completed = subprocess.run(
        [sys.executable, "tools/replay_strategy.py",
         "--minutes", str(minutes), "--signals", str(signals),
         "--config", "config.example.yaml", "--now-ts", "400"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        text=True, capture_output=True, check=True,
    )

    assert "top-of-book approximation" in completed.stdout
