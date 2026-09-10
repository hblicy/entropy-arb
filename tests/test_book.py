"""plan_arb sizing math: thresholds, fees, caps, minimums.

Run:  python3 -m pytest tests/  (or  python3 tests/test_book.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.book as book_module  # noqa: E402
from entropy_arb.book import OrderBook, plan_arb  # noqa: E402


def make_book(bids, asks):
    b = OrderBook()
    b.apply_hl([[{"px": str(p), "sz": str(s)} for p, s in bids],
                [{"px": str(p), "sz": str(s)} for p, s in asks]])
    return b


def common(**over):
    kw = dict(threshold_bps=0.0, buy_fee_bps=0.0, sell_fee_bps=0.0,
              take_fraction=1.0, cap_notional=1e9, min_base=0.0,
              min_notional=0.0, size_step=1e-4)
    kw.update(over)
    return kw


def test_no_edge_below_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=6.0))
    assert plan is None and reason == "no_edge"


def test_edge_above_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=4.0))
    assert reason == "ok"
    assert abs(plan.qty - 10.0) < 1e-9
    assert abs(plan.top_premium_bps - 5.0) < 0.01
    assert plan.exp_edge_usd > 0


def test_fees_kill_marginal_edge():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps gross
    # 3 + 3 bps of fees swallow the 5 bps premium
    plan, reason = plan_arb(buy, sell, **common(buy_fee_bps=3.0,
                                                sell_fee_bps=3.0))
    assert plan is None and reason == "no_edge"


def test_take_fraction_and_cap():
    buy = make_book(bids=[(99.9, 100)], asks=[(100.0, 100)])
    sell = make_book(bids=[(100.5, 100)], asks=[(100.6, 100)])
    plan, reason = plan_arb(buy, sell, **common(take_fraction=0.5))
    assert reason == "ok" and abs(plan.qty - 50.0) < 1e-9
    plan, reason = plan_arb(buy, sell, **common(cap_notional=1000.0))
    assert reason == "ok" and abs(plan.qty - 9.9502) < 1e-6
    assert_both_legs_capped(plan, 1000.0)


def assert_both_legs_capped(plan, cap):
    assert plan.buy_notional <= cap + 1e-9
    assert plan.sell_notional <= cap + 1e-9


def test_cap_uses_actual_multilevel_buy_notional():
    buy = make_book(bids=[(99, 10)], asks=[(100, 1), (120, 10)])
    sell = make_book(bids=[(130, 20)], asks=[(131, 20)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=500, size_step=0.01))

    assert reason == "ok"
    assert_both_legs_capped(plan, 500)


def test_cap_also_limits_more_expensive_sell_leg():
    buy = make_book(bids=[(99, 10)], asks=[(100, 10)])
    sell = make_book(bids=[(150, 10)], asks=[(151, 10)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=500, size_step=0.01))

    assert reason == "ok"
    assert plan.qty == 3.33
    assert_both_legs_capped(plan, 500)


def test_cap_holds_when_direction_is_reversed():
    buy = make_book(bids=[(149, 10)], asks=[(150, 10)])
    sell = make_book(bids=[(200, 10)], asks=[(201, 10)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=500, size_step=0.01))

    assert reason == "ok"
    assert_both_legs_capped(plan, 500)


def test_cap_rounding_can_drop_plan_below_minimum_base():
    buy = make_book(bids=[(99, 1)], asks=[(100, 1)])
    sell = make_book(bids=[(110, 1)], asks=[(111, 1)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=9.99, size_step=0.1,
                            min_base=0.1))

    assert plan is None
    assert reason == "below_min_base"


def test_cap_reduction_can_drop_plan_below_minimum_notional():
    buy = make_book(bids=[(99, 1)], asks=[(100, 1)])
    sell = make_book(bids=[(110, 1)], asks=[(111, 1)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=9.0, size_step=0.01,
                            min_notional=10.0))

    assert plan is None
    assert reason == "below_min_notional"


def test_cap_handles_small_size_step_without_iterative_decrement():
    buy = make_book(bids=[(99, 1)], asks=[(100, 1)])
    sell = make_book(bids=[(123.456789, 1)], asks=[(124, 1)])

    plan, reason = plan_arb(
        buy, sell, **common(cap_notional=1.0, size_step=1e-8))

    assert reason == "ok"
    assert_both_legs_capped(plan, 1.0)


def test_min_notional():
    buy = make_book(bids=[(99.9, 0.05)], asks=[(100.0, 0.05)])
    sell = make_book(bids=[(100.5, 0.05)], asks=[(100.6, 0.05)])
    plan, reason = plan_arb(buy, sell, **common(min_notional=10.0))
    assert plan is None and reason == "below_min_notional"


def test_marginal_slice_respects_threshold():
    # second ask level only clears 2 bps — with threshold 3 the crossable
    # size must stop at the first level
    buy = make_book(bids=[(99.9, 5)], asks=[(100.0, 5), (100.08, 5)])
    sell = make_book(bids=[(100.1, 20)], asks=[(100.3, 20)])
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=3.0))
    assert reason == "ok"
    assert abs(plan.q_max - 5.0) < 1e-9


def test_lighter_diff_maintenance():
    b = make_book(bids=[(99.0, 5)], asks=[(100.0, 2), (100.1, 3)])
    # diff: the 100.0 ask level is removed server-side, a new bid appears
    b.apply_lighter({"bids": [{"price": "99.1", "size": "1"}],
                     "asks": [{"price": "100.0", "size": "0"}]},
                    snapshot=False)
    assert b.best_ask() == 100.1 and b.best_bid() == 99.1
    # a snapshot replaces the whole book
    b.apply_lighter({"bids": [{"price": "98.9", "size": "1"}],
                     "asks": [{"price": "100.2", "size": "3"}]},
                    snapshot=True)
    assert b.best_bid() == 98.9 and b.best_ask() == 100.2


def test_feed_freshness_uses_monotonic_time_when_wall_clock_rolls_back(
        monkeypatch):
    wall_clock = [100.0]
    monotonic_clock = [10.0]
    monkeypatch.setattr(book_module.time, "time", lambda: wall_clock[0])
    monkeypatch.setattr(
        book_module.time, "monotonic", lambda: monotonic_clock[0])

    book = make_book(bids=[(99.9, 1)], asks=[(100.0, 1)])
    wall_clock[0] = 5.0
    monotonic_clock[0] = 16.0

    assert book.is_fresh(5.0) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
