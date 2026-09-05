"""Fee adjustment in the minute-data analyzer."""
import csv
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.analyze as analyze  # noqa: E402


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
