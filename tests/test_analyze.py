"""Fee adjustment in the minute-data analyzer."""
import csv
import gzip
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.analyze as analyze  # noqa: E402
from entropy_arb.strategy_recorder import (  # noqa: E402
    STRATEGY_EVENT_HEADER,
)


ANALYZE_FIELDS = [
    "minute_ts", "samples", "entropy_symbol", "entropy_dex",
    "hedge_symbol", "hedge_venue", "premium_close_bps",
    "premium_mean_bps", "sell_edge_max_bps", "buy_edge_max_bps",
    "reference_basis_close_bps", "residual_close_bps",
    "funding_diff_close_bps_per_hour",
]


def write_analyze_rows(path, rows, *, compressed=False):
    opener = gzip.open if compressed else open
    with opener(path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(ANALYZE_FIELDS)
        writer.writerows(rows)


def test_validate_single_market_rejects_mixed_minute_rows():
    with pytest.raises(ValueError, match="multiple markets"):
        analyze.validate_single_market([
            {"entropy_symbol": "ANTH", "entropy_dex": "io",
             "hedge_symbol": "ANTHROPIC", "hedge_venue": "lighter-rh"},
            {"entropy_symbol": "ANTH", "entropy_dex": "io",
             "hedge_symbol": "ANTH", "hedge_venue": "lighter-rh"},
        ])


def test_validate_single_market_accepts_legacy_rows_without_identity():
    market = analyze.validate_single_market([
        {"entropy_symbol": "", "entropy_dex": "", "hedge_symbol": "",
         "hedge_venue": ""},
        {"entropy_symbol": "", "entropy_dex": "", "hedge_symbol": "",
         "hedge_venue": ""},
    ])
    assert market == ("", "", "", "")


def test_validate_single_market_normalizes_legacy_identity():
    market = analyze.validate_single_market([
        {"symbol": "SNDK", "entropy_dex": "io",
         "hedge_venue": "lighter-rh"},
    ])

    assert market == ("SNDK", "io", "SNDK", "lighter-rh")


def test_fee_adjusted_rooms_match_execution_formula_in_both_directions():
    rows = [{"sell_max": 8.0, "buy_max": 7.0}]
    entropy_fee = 0.9
    hedge_fee = 1.0

    sell_room, buy_room = analyze.fee_adjusted_rooms(
        rows, midline=-0.8, entropy_fee_bps=entropy_fee,
        hedge_fee_bps=hedge_fee)

    sell_net = sell_room[0] - 0.8
    buy_net = buy_room[0] + 0.8
    assert (1.0 + 8.0 / 1e4) * (1.0 - entropy_fee / 1e4) == \
        pytest.approx(
            (1.0 + hedge_fee / 1e4) * (1.0 + sell_net / 1e4))
    assert (1.0 + 7.0 / 1e4) * (1.0 - hedge_fee / 1e4) == \
        pytest.approx(
            (1.0 + entropy_fee / 1e4) * (1.0 + buy_net / 1e4))
    assert sell_room[0] != pytest.approx(8.0 + 0.8 - 1.9)
    assert buy_room[0] != pytest.approx(7.0 - 0.8 - 1.9)


def test_load_rows_merges_duplicate_minute_fragments_before_filtering(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol", "entropy_dex", "hedge_venue",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    rows = [
        [120, 6, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
        [60, 10, "SNDK", "io", "lighter-rh", -1, -1, 1, 1],
        [120, 6, "SNDK", "io", "lighter-rh", 5, 3, 4, 2],
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows(rows)

    loaded = analyze.load_rows(str(path), hours=0.0, min_samples=10)

    assert [row["ts"] for row in loaded] == [60.0, 120.0]
    merged = loaded[1]
    assert merged["samples"] == 12
    assert merged["prem"] == 5.0
    assert merged["prem_mean"] == pytest.approx(2.0)
    assert merged["sell_max"] == 4.0
    assert merged["buy_max"] == 3.0


def test_load_rows_accepts_distinct_symbols_in_new_schema(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "entropy_symbol", "entropy_dex",
        "hedge_symbol", "hedge_venue", "premium_close_bps",
        "premium_mean_bps", "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerow(
            [60, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
             1, 1, 2, 3])

    loaded = analyze.load_rows(str(path), hours=0.0, min_samples=10)

    assert len(loaded) == 1
    assert (
        loaded[0]["entropy_symbol"], loaded[0]["entropy_dex"],
        loaded[0]["hedge_symbol"], loaded[0]["hedge_venue"],
    ) == ("ANTH", "io", "ANTHROPIC", "lighter-rh")


def test_load_rows_reads_gzip_identically_to_plain_csv(tmp_path):
    row = [
        60, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
        1.0, 1.0, 2.0, 3.0, 0.5, 0.5, 0.08,
    ]
    plain = tmp_path / "minutes.csv"
    compressed = tmp_path / "minutes.csv.gz"
    write_analyze_rows(plain, [row])
    write_analyze_rows(compressed, [row], compressed=True)

    assert analyze.load_rows(
        str(compressed), hours=0.0, min_samples=10) == analyze.load_rows(
        str(plain), hours=0.0, min_samples=10)


def test_load_rows_parses_optional_reference_close_metrics(tmp_path):
    path = tmp_path / "minutes.csv"
    rows = [
        [60, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         1, 1, 2, 3, 0.5, -0.25, 0.08],
        [120, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         1, 1, 2, 3, "", "nan", "inf"],
    ]
    write_analyze_rows(path, rows)

    loaded = analyze.load_rows(str(path), hours=0.0, min_samples=10)

    assert loaded[0]["reference_basis"] == 0.5
    assert loaded[0]["residual"] == -0.25
    assert loaded[0]["funding_diff"] == 0.08
    assert loaded[1]["reference_basis"] is None
    assert loaded[1]["residual"] is None
    assert loaded[1]["funding_diff"] is None


def test_duplicate_minute_uses_last_reference_close_without_sample_weighting(
        tmp_path):
    path = tmp_path / "minutes.csv"
    rows = [
        [60, 59, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         1, 1, 2, 3, 0.5, 10.0, 0.08],
        [60, 1, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         2, 2, 4, 5, 0.6, 20.0, 0.09],
    ]
    write_analyze_rows(path, rows)

    loaded = analyze.load_rows(str(path), hours=0.0, min_samples=10)

    assert len(loaded) == 1
    assert loaded[0]["samples"] == 60
    assert loaded[0]["residual"] == 20.0
    assert loaded[0]["reference_basis"] == 0.6
    assert loaded[0]["funding_diff"] == 0.09


def test_main_prints_reference_distributions_only_when_values_exist(
        monkeypatch, capsys, tmp_path):
    new_path = tmp_path / "new.csv"
    old_path = tmp_path / "old.csv"
    rows = [
        [60 * index, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         index / 10, index / 10, 2 + index / 10, 3 + index / 10,
         0.5 + index / 100, -0.25 + index / 100, 0.08]
        for index in range(1, 31)
    ]
    write_analyze_rows(new_path, rows)
    with old_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(ANALYZE_FIELDS[:10])
        writer.writerows(row[:10] for row in rows)

    monkeypatch.setattr(
        sys, "argv", ["analyze.py", "--csv", str(new_path),
                      "--min-samples", "1"])
    analyze.main()
    output = capsys.readouterr().out
    assert "reference basis, minute close (bps)" in output
    assert "signed residual, minute close (bps)" in output
    assert "funding difference, minute close (bps/hour)" in output

    monkeypatch.setattr(
        sys, "argv", ["analyze.py", "--csv", str(old_path),
                      "--min-samples", "1"])
    analyze.main()
    legacy_output = capsys.readouterr().out
    assert "reference basis, minute close (bps)" not in legacy_output
    assert "signed residual, minute close (bps)" not in legacy_output
    assert "funding difference, minute close (bps/hour)" not in legacy_output


def test_describe_returns_population_distribution():
    assert analyze.describe([1.0, 2.0, 3.0]) == pytest.approx(
        (2.0, (2.0 / 3.0) ** 0.5, 2.0, 1.1, 2.9))


def test_load_rows_rejects_partial_new_market_identity_schema(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "entropy_symbol", "entropy_dex",
        "hedge_venue", "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerow(
            [60, 60, "ANTH", "io", "lighter-rh", 1, 1, 2, 3])

    with pytest.raises(ValueError, match="market identity columns"):
        analyze.load_rows(str(path), hours=0.0, min_samples=10)


@pytest.mark.parametrize("hidden_by", ["hours", "min_samples"])
def test_load_rows_rejects_mixed_market_before_filters(tmp_path, hidden_by):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol", "entropy_dex", "hedge_venue",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    now = time.time()
    second_ts = now - 7200 if hidden_by == "hours" else now
    second_samples = 60 if hidden_by == "hours" else 1
    rows = [
        [now, 60, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
        [second_ts, second_samples, "SNDK", "io", "tradexyz", 1, 1, 2, 3],
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows(rows)

    with pytest.raises(ValueError, match="multiple markets"):
        analyze.load_rows(
            str(path), hours=1.0 if hidden_by == "hours" else 0.0,
            min_samples=10)


def test_load_rows_rejects_mixed_market_even_when_metrics_are_invalid(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol", "entropy_dex", "hedge_venue",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerow(
            [60, 60, "SNDK", "io", "lighter-rh", 1, 1, 2, 3])
        writer.writerow(
            [120, 60, "SNDK", "io", "tradexyz", "nan", 1, 2, 3])

    with pytest.raises(ValueError, match="multiple markets"):
        analyze.load_rows(str(path), hours=0.0, min_samples=10)


def test_load_rows_rejects_partial_market_identity_schema(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerow([120, 60, "SNDK", 1, 1, 2, 3])

    with pytest.raises(ValueError, match="market identity columns"):
        analyze.load_rows(str(path), hours=0.0, min_samples=10)


def test_load_rows_rejects_empty_identity_in_current_schema(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol", "entropy_dex", "hedge_venue",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerow([120, 60, "SNDK", "io", "", 1, 1, 2, 3])

    with pytest.raises(ValueError, match="market identity values"):
        analyze.load_rows(str(path), hours=0.0, min_samples=10)


def test_load_rows_skips_non_finite_and_non_positive_samples(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = [
        "minute_ts", "samples", "symbol", "entropy_dex", "hedge_venue",
        "premium_close_bps", "premium_mean_bps",
        "sell_edge_max_bps", "buy_edge_max_bps",
    ]
    rows = [
        [60, 10, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
        ["nan", 10, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
        [120, 10, "SNDK", "io", "lighter-rh", "inf", 1, 2, 3],
        [180, 10, "SNDK", "io", "lighter-rh", 1, "-inf", 2, 3],
        [240, 10, "SNDK", "io", "lighter-rh", 1, 1, "nan", 3],
        [300, 10, "SNDK", "io", "lighter-rh", 1, 1, 2, "inf"],
        [360, 0, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
        [420, -1, "SNDK", "io", "lighter-rh", 1, 1, 2, 3],
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows(rows)

    loaded = analyze.load_rows(str(path), hours=0.0, min_samples=0)

    assert [row["ts"] for row in loaded] == [60.0]


@pytest.mark.parametrize("flag", ["--entropy-fee-bps", "--hedge-fee-bps"])
def test_exact_fee_arguments_must_be_supplied_as_a_pair(
        flag, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys, "argv",
        ["analyze.py", flag, "0.9", "--csv", str(tmp_path / "missing.csv")])

    with pytest.raises(SystemExit) as exc:
        analyze.main()

    assert exc.value.code == 2
    assert "must be supplied together" in capsys.readouterr().err


def write_strategy_rows(path, rows, *, compressed=False):
    opener = gzip.open if compressed else open
    with opener(path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STRATEGY_EVENT_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def strategy_row(*, ts_ms, intent, campaign_id, event="decision",
                 direction="sell_entropy", hold_seconds="",
                 realized_pnl_usd="", reason=""):
    return {
        "ts_ms": ts_ms,
        "time_utc": "1970-01-01T00:00:00Z",
        "mode": "shadow",
        "event": event,
        "intent": intent,
        "reason": reason,
        "campaign_id": campaign_id,
        "entropy_symbol": "ANTH",
        "entropy_dex": "io",
        "hedge_symbol": "ANTHROPIC",
        "hedge_venue": "lighter-rh",
        "direction": direction,
        "hold_seconds": hold_seconds,
        "realized_pnl_usd": realized_pnl_usd,
    }


def test_strategy_summary_reports_closed_open_and_hold_windows(tmp_path):
    path = tmp_path / "strategy-events.csv"
    write_strategy_rows(path, [
        strategy_row(ts_ms=1_000, intent="OPEN", campaign_id="c1"),
        strategy_row(ts_ms=2_000, intent="CLOSE", campaign_id="c1",
                     event="campaign_closed", hold_seconds=1800,
                     realized_pnl_usd=1.25),
        strategy_row(ts_ms=3_000, intent="OPEN", campaign_id="c2",
                     direction="buy_entropy"),
        strategy_row(ts_ms=4_000, intent="FORCED_CLOSE", campaign_id="c2",
                     event="campaign_closed", direction="buy_entropy",
                     hold_seconds=7200, realized_pnl_usd=-0.5),
        strategy_row(ts_ms=5_000, intent="OPEN", campaign_id="c3"),
    ])

    rows = analyze.load_strategy_events(str(path))
    summary = analyze.summarize_strategy_events(rows)

    assert summary["completed_campaigns"] == 2
    assert summary["within_1h"] == 1
    assert summary["within_6h"] == 2
    assert summary["still_open"] == 1
    assert summary["forced_closes"] == 1
    assert summary["realized_pnl_usd"] == pytest.approx(0.75)


def test_strategy_events_reject_mixed_pair_identity(tmp_path):
    path = tmp_path / "strategy-events.csv"
    second = strategy_row(ts_ms=2_000, intent="OPEN", campaign_id="c2")
    second["hedge_symbol"] = "SNDK"
    write_strategy_rows(path, [
        strategy_row(ts_ms=1_000, intent="OPEN", campaign_id="c1"),
        second,
    ])

    with pytest.raises(ValueError, match="multiple markets"):
        analyze.load_strategy_events(str(path))


def test_strategy_events_support_gzip(tmp_path):
    path = tmp_path / "strategy-events.csv.gz"
    write_strategy_rows(path, [
        strategy_row(ts_ms=1_000, intent="OPEN", campaign_id="c1"),
    ], compressed=True)

    assert len(analyze.load_strategy_events(str(path))) == 1


def test_main_prints_optional_strategy_summary(
        monkeypatch, capsys, tmp_path):
    minute_path = tmp_path / "minutes.csv"
    minute_rows = [
        [60 * index, 60, "ANTH", "io", "ANTHROPIC", "lighter-rh",
         1, 1, 2, 3, 0.5, -0.25, 0.08]
        for index in range(1, 31)
    ]
    write_analyze_rows(minute_path, minute_rows)
    event_path = tmp_path / "strategy-events.csv"
    write_strategy_rows(event_path, [
        strategy_row(ts_ms=1_000, intent="OPEN", campaign_id="c1"),
        strategy_row(ts_ms=2_000, intent="CLOSE", campaign_id="c1",
                     event="campaign_closed", hold_seconds=1800),
    ])
    monkeypatch.setattr(sys, "argv", [
        "analyze.py", "--csv", str(minute_path), "--min-samples", "1",
        "--strategy-csv", str(event_path),
    ])

    analyze.main()

    output = capsys.readouterr().out
    assert "completed campaigns: 1" in output
    assert "within 1h: 1" in output
    assert "within 6h: 1" in output
    assert "still open: 0" in output
