"""Fee adjustment in the minute-data analyzer."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.analyze as analyze  # noqa: E402


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
