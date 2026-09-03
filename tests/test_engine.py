"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import csv
import os
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.engine as engine_module  # noqa: E402
from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.models import OrderResult  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash, self.volume_usd = 0.0, 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


class ExecutingVenue(StubVenue):
    def __init__(self, key, label, result):
        super().__init__(key, label)
        self.result = result

    def px_round(self, px, round_up):
        return px

    async def send_taker(self, **_kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class LifecycleVenue(StubVenue):
    def __init__(self, key, label):
        super().__init__(key, label)
        self.conf = SimpleNamespace(symbol="SNDK")
        self.closed = False

    async def load_market(self):
        if self.key == "entropy":
            self.set_book(100.10, 100.11)
        else:
            self.set_book(99.99, 100.00)

    def configure_peer(self, _other):
        return

    def start_tasks(self, _stop, _notify, _live):
        return []

    async def close(self):
        self.closed = True


class BurstVenue(LifecycleVenue):
    async def load_market(self):
        if self.key == "entropy":
            self.set_book(100.00, 100.01)
        else:
            self.set_book(99.99, 100.00)

    def start_tasks(self, stop, notify, _live):
        if self.key != "entropy":
            return []

        async def burst():
            self.set_book(100.10, 100.11)
            notify()
            self.set_book(100.00, 100.01)
            notify()
            await asyncio.sleep(0.01)
            stop.set()
            notify()

        return [asyncio.create_task(burst(), name="burst-entropy")]


class InvalidBurstVenue(LifecycleVenue):
    def start_tasks(self, stop, notify, _live):
        if self.key != "entropy":
            return []

        async def burst():
            try:
                self.set_book(100.00, 0.0)
                notify()
            finally:
                self.set_book(100.00, 100.01)
                stop.set()

        return [asyncio.create_task(burst(), name="invalid-burst-entropy")]


def execution_plan():
    return ArbPlan(
        qty=0.5,
        buy_limit=100.0,
        sell_limit=100.2,
        buy_notional=50.0,
        sell_notional=50.1,
        q_max=0.5,
        q_max_notional=50.0,
        top_premium_bps=20.0,
        marginal_premium_bps=20.0,
        buy_fee=0.0,
        sell_fee=0.0,
    )


def make_engine(record_only=False, **thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg, record_only=record_only)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=e) + eng._eff_threshold(buy=e, sell=h)
        approx(total, 7.0)


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    e, h = eng.entropy, eng.hedge
    e.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(e, h), 0.0)          # flat: dead zone
    e.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(e, h)                    # buying entropy adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, e), 0.0)           # selling entropy reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(e, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps rich = exactly on the midline: inside the band, no trade
    eng.entropy.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_entropy_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy entropy
    eng.entropy.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position = -100.0   # entropy already short at its cap
    eng.entropy.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


def test_execute_consumes_typed_order_results():
    eng = make_engine()
    buy = ExecutingVenue(
        "hedge", "RH",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    unresolved = asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert unresolved is False
    assert buy.position == 0.5
    assert sell.position == -0.5
    assert eng.trades == 1
    approx(eng.total_fill_edge, 0.1)


def test_execute_converts_raised_leg_exception_to_failure():
    eng = make_engine()
    buy = ExecutingVenue("hedge", "RH", RuntimeError("send exploded"))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    unresolved = asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert unresolved is False
    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["status"] == "send-failed/filled"


def test_execute_does_not_count_mismatched_terminal_fills_as_success():
    eng = make_engine()
    buy = ExecutingVenue(
        "hedge", "RH", OrderResult(status="canceled", filled_base=0.0))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    unresolved = asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert unresolved is False
    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["ok"] is False


def test_execute_does_not_count_two_zero_fills_as_success():
    eng = make_engine()
    buy = ExecutingVenue(
        "hedge", "RH", OrderResult(status="canceled", filled_base=0.0))
    sell = ExecutingVenue(
        "entropy", "ENTROPY", OrderResult(status="canceled", filled_base=0.0))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["ok"] is False


def test_shutdown_drain_waits_for_execution_without_cancelling_it():
    async def go():
        eng = make_engine()

        async def settle_later():
            await asyncio.sleep(0.03)
            return "settled"

        task = asyncio.create_task(settle_later())
        eng._exec_tasks.add(task)
        task.add_done_callback(eng._exec_tasks.discard)

        await eng._drain_executions(poll_sec=0.005)

        assert task.done()
        assert task.cancelled() is False
        assert task.result() == "settled"

    asyncio.run(go())


def test_shutdown_drain_reconciles_an_unknown_inflight_outcome():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        reconciles = []

        async def reconcile(*, hedge, strict=False):
            reconciles.append((hedge, strict))

        eng._reconcile_positions = reconcile
        eng._shutdown_reconcile_required = True

        await eng._drain_executions(poll_sec=0.005)

        assert reconciles == [(True, True)]
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_reconcile_skipped_during_grace_is_rescheduled():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.03
        eng.entropy.last_traded_ts = time.time()
        eng.hedge.last_traded_ts = time.time()

        await eng._reconcile_positions(hedge=False)

        assert eng._reconcile_evt.is_set() is False
        await asyncio.sleep(0.05)
        assert eng._reconcile_evt.is_set() is True

    asyncio.run(go())


def test_signal_recorder_starts_only_in_record_only_mode():
    async def go():
        record_engine = make_engine(record_only=True)
        record_dir = tempfile.mkdtemp()
        record_engine.cfg.recorder_csv = os.path.join(
            record_dir, "minutes.csv")
        record_engine.cfg.recorder_signal_csv = os.path.join(
            record_dir, "signals.csv")
        record_tasks = []

        record_engine._start_recorders(record_tasks)

        assert record_engine.signal_recorder is not None
        assert any(task.get_name() == "signal-recorder"
                   for task in record_tasks)
        record_engine.request_stop()
        await asyncio.gather(*record_tasks)

        live_engine = make_engine(record_only=False)
        live_engine.cfg.recorder_enabled = False
        live_tasks = []

        live_engine._start_recorders(live_tasks)

        assert live_engine.signal_recorder is None
        assert all(task.get_name() != "signal-recorder"
                   for task in live_tasks)

    asyncio.run(go())


def test_signal_recorder_io_failure_propagates_from_engine():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        blocker = os.path.join(directory, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("block")
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(blocker, "signals.csv")
        venues = {
            "entropy": LifecycleVenue("entropy", "ENTROPY"),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original_create_venue = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(OSError):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original_create_venue

        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_record_only_captures_burst_signal_before_event_coalesces():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": BurstVenue("entropy", "ENTROPY"),
            "hedge": BurstVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original_create_venue = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            await eng._run_inner()
        finally:
            engine_module.create_venue = original_create_venue

        assert os.path.exists(cfg.recorder_signal_csv)
        with open(cfg.recorder_signal_csv, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert [(row["direction"], row["event"]) for row in rows] == [
            ("sell_entropy", "start"),
            ("sell_entropy", "end"),
        ]

    asyncio.run(go())


def test_record_only_propagates_synchronous_signal_calculation_error():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": InvalidBurstVenue("entropy", "ENTROPY"),
            "hedge": InvalidBurstVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original_create_venue = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(ZeroDivisionError):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original_create_venue

        assert all(venue.closed for venue in venues.values())
        with open(cfg.recorder_signal_csv, newline="",
                  encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        start_ids = {row["event_id"] for row in rows
                     if row["event"] == "start"}
        end_ids = {row["event_id"] for row in rows
                   if row["event"] == "end"}
        assert end_ids <= start_ids

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
