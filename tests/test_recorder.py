"""Minute recorder: aggregation, rollover, CSV output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import csv
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook, plan_arb  # noqa: E402
from entropy_arb.recorder import (  # noqa: E402
    HEADER,
    MinuteRecorder,
    SignalRecorder,
)


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


class SignalVenue:
    def __init__(self, name: str, fee_bps: float = 0.0):
        self.name = name
        self.book = OrderBook()
        self.fee_bps = fee_bps


def set_signal_book(venue, *, bid, ask, ts):
    venue.book.apply_hl([
        [{"px": str(bid), "sz": "100"}],
        [{"px": str(ask), "sz": "100"}],
    ])
    venue.book.last_update_ts = ts
    venue.book.alive_ts = ts


def set_signal_levels(venue, *, bids, asks, ts):
    venue.book.apply_hl([
        [{"px": str(px), "sz": str(size)} for px, size in bids],
        [{"px": str(px), "sz": str(size)} for px, size in asks],
    ])
    venue.book.last_update_ts = ts
    venue.book.alive_ts = ts


def make_signal_recorder(path):
    entropy = SignalVenue("entropy")
    hedge = SignalVenue("hedge")
    set_signal_book(entropy, bid=100.10, ask=100.11, ts=1000.0)
    set_signal_book(hedge, bid=99.99, ask=100.00, ts=1000.0)
    rec = SignalRecorder(
        path, entropy, hedge,
        midline_bps=0.0, upper_bps=5.0, lower_bps=5.0,
        take_fraction=1.0, max_order_notional=100_000.0,
        min_base=0.0, min_notional=0.0, size_step=0.001,
        leg_slippage_bps=20.0, staleness_sec=3.0, sample_sec=1.0,
    )
    return rec, entropy, hedge


def read_signal_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: entropy 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [*rows[0]] == HEADER
    assert len(rows) == 2
    m1, m2 = rows
    assert int(m1["samples"]) == 2 and int(m2["samples"]) == 1
    assert abs(float(m1["premium_open_bps"]) - 10.0) < 0.2
    assert abs(float(m1["premium_high_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_close_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_mean_bps"]) - 15.0) < 0.2
    # executable edges: sell = bid_e/ask_h - 1, buy = bid_h/ask_e - 1
    assert abs(float(m2["sell_edge_max_bps"])
               - ((100.09 / 100.01 - 1) * 1e4)) < 0.05
    assert abs(float(m2["buy_edge_max_bps"])
               - ((99.99 / 100.11 - 1) * 1e4)) < 0.05
    # closes carry the last books
    assert float(m2["entropy_bid"]) == 100.09
    assert float(m2["hedge_ask"]) == 100.01


def test_stale_books_are_skipped():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(e_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # no row, no file


def test_append_keeps_single_header():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    for start in (1_700_000_000.0, 1_700_000_060.0):
        rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
        rec.sample(start)
        rec.close()
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3             # one header + two rows
    assert lines[0].startswith("minute_ts,")


def test_signal_lifecycle_writes_start_sample_and_end():
    path = os.path.join(tempfile.mkdtemp(), "signals.csv")
    rec, entropy, _ = make_signal_recorder(path)

    rec.observe(now=1000.0)
    rec.observe(now=1000.5)
    rec.observe(now=1001.0)
    set_signal_book(entropy, bid=100.00, ask=100.01, ts=1001.2)
    rec.observe(now=1001.2)
    rec.close(now=1001.2)

    rows = read_signal_rows(path)
    assert [row["event"] for row in rows] == ["start", "sample", "end"]
    assert {row["direction"] for row in rows} == {"sell_entropy"}
    assert len({row["event_id"] for row in rows}) == 1
    assert [int(row["elapsed_ms"]) for row in rows] == [0, 1000, 1200]
    assert rows[-1]["end_reason"] == "edge_below_threshold"


def test_signal_shutdown_closes_active_event_once():
    path = os.path.join(tempfile.mkdtemp(), "signals.csv")
    rec, _, _ = make_signal_recorder(path)

    rec.observe(now=1000.0)
    rec.close(now=1002.0)
    rec.close(now=1003.0)

    rows = read_signal_rows(path)
    assert [row["event"] for row in rows] == ["start", "end"]
    assert rows[-1]["elapsed_ms"] == "2000"
    assert rows[-1]["end_reason"] == "shutdown"


def test_signal_directions_have_independent_lifecycles():
    path = os.path.join(tempfile.mkdtemp(), "signals.csv")
    rec, entropy, hedge = make_signal_recorder(path)

    rec.observe(now=1000.0)
    set_signal_book(entropy, bid=99.90, ask=99.91, ts=1000.2)
    set_signal_book(hedge, bid=100.02, ask=100.03, ts=1000.2)
    rec.observe(now=1000.2)
    set_signal_book(entropy, bid=100.00, ask=100.01, ts=1000.4)
    set_signal_book(hedge, bid=99.99, ask=100.00, ts=1000.4)
    rec.observe(now=1000.4)
    rec.close(now=1000.4)

    rows = read_signal_rows(path)
    assert [(row["direction"], row["event"]) for row in rows] == [
        ("sell_entropy", "start"),
        ("sell_entropy", "end"),
        ("buy_entropy", "start"),
        ("buy_entropy", "end"),
    ]
    sell_id, buy_id = rows[0]["event_id"], rows[2]["event_id"]
    assert sell_id.startswith("sell_entropy-")
    assert buy_id.startswith("buy_entropy-")
    assert sell_id != buy_id


def test_signal_metrics_use_plan_and_book_update_times():
    path = os.path.join(tempfile.mkdtemp(), "signals.csv")
    entropy = SignalVenue("entropy", fee_bps=0.3)
    hedge = SignalVenue("hedge", fee_bps=0.6)
    set_signal_levels(
        entropy,
        bids=[(100.20, 0.5), (100.15, 1.5)],
        asks=[(100.21, 2.0)],
        ts=2000.8,
    )
    set_signal_levels(
        hedge,
        bids=[(99.99, 2.0)],
        asks=[(100.00, 0.5), (100.05, 1.5)],
        ts=2000.5,
    )
    entropy.book.alive_ts = hedge.book.alive_ts = 2000.99
    rec = SignalRecorder(
        path, entropy, hedge,
        midline_bps=0.0, upper_bps=5.0, lower_bps=5.0,
        take_fraction=1.0, max_order_notional=150.0,
        min_base=0.0, min_notional=0.0, size_step=0.001,
        leg_slippage_bps=20.0, staleness_sec=3.0,
    )

    rec.observe(now=2001.0)
    rec.close(now=2001.0)

    row = read_signal_rows(path)[0]
    expected_plan, reason = plan_arb(
        hedge.book, entropy.book,
        threshold_bps=5.0,
        buy_fee_bps=hedge.fee_bps,
        sell_fee_bps=entropy.fee_bps,
        take_fraction=1.0,
        cap_notional=150.0,
        min_base=0.0,
        min_notional=0.0,
        size_step=0.001,
    )
    assert reason == "ok" and expected_plan is not None
    assert float(row["entropy_book_age_ms"]) == pytest.approx(200.0)
    assert float(row["hedge_book_age_ms"]) == pytest.approx(500.0)
    assert float(row["book_update_skew_ms"]) == pytest.approx(300.0)
    assert float(row["top_edge_bps"]) == pytest.approx(
        (100.20 / 100.00 - 1.0) * 1e4
    )
    assert float(row["net_threshold_bps"]) == 5.0
    assert float(row["total_fee_bps"]) == pytest.approx(0.9)
    assert row["plan_status"] == "ok"
    assert float(row["qty"]) == pytest.approx(expected_plan.qty)
    assert float(row["buy_limit"]) == pytest.approx(expected_plan.buy_limit)
    assert float(row["sell_limit"]) == pytest.approx(expected_plan.sell_limit)
    assert float(row["planned_notional_usd"]) == pytest.approx(
        expected_plan.buy_notional
    )
    assert float(row["crossable_notional_usd"]) == pytest.approx(
        expected_plan.q_max_notional
    )
    assert float(row["buy_depth_slippage_bps"]) == pytest.approx(
        (expected_plan.buy_limit / 100.00 - 1.0) * 1e4
    )
    assert float(row["sell_depth_slippage_bps"]) == pytest.approx(
        (100.20 / expected_plan.sell_limit - 1.0) * 1e4
    )
    assert float(row["leg_slippage_limit_bps"]) == 20.0
    assert float(row["expected_edge_usd"]) == pytest.approx(
        expected_plan.exp_edge_usd
    )


def test_signal_below_minimum_plan_is_still_recorded():
    path = os.path.join(tempfile.mkdtemp(), "signals.csv")
    entropy = SignalVenue("entropy")
    hedge = SignalVenue("hedge")
    set_signal_book(entropy, bid=100.20, ask=100.21, ts=3000.0)
    set_signal_levels(
        hedge,
        bids=[(99.99, 0.1)],
        asks=[(100.00, 0.1)],
        ts=3000.0,
    )
    rec = SignalRecorder(
        path, entropy, hedge,
        midline_bps=0.0, upper_bps=5.0, lower_bps=5.0,
        take_fraction=1.0, max_order_notional=100_000.0,
        min_base=0.0, min_notional=100.0, size_step=0.001,
        leg_slippage_bps=20.0, staleness_sec=3.0,
    )

    rec.observe(now=3000.0)
    rec.close(now=3000.0)

    row = read_signal_rows(path)[0]
    assert row["event"] == "start"
    assert row["plan_status"] == "below_min_notional"
    for field in (
        "qty", "buy_limit", "sell_limit", "planned_notional_usd",
        "crossable_notional_usd", "buy_depth_slippage_bps",
        "sell_depth_slippage_bps", "expected_edge_usd",
    ):
        assert row[field] == ""
    assert float(row["top_edge_bps"]) > 5.0
    assert row["entropy_bid"] and row["hedge_ask"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
