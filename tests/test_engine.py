"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import csv
import logging
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
from entropy_arb.reference import (  # noqa: E402
    ReferenceAlertState,
    ReferenceState,
    ReferenceUpdate,
)
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402

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
        self.reference = ReferenceState()
        self.reference_refreshes = 0

    async def refresh_reference_rest(self):
        self.reference_refreshes += 1
        return False

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


class PositionVenue(ExecutingVenue):
    def __init__(self, key, label, result, chain_position=0.0):
        super().__init__(key, label, result)
        self.chain_position = chain_position
        self.send_calls = 0

    async def fetch_position(self):
        return self.chain_position

    async def send_taker(self, **kwargs):
        self.send_calls += 1
        return await super().send_taker(**kwargs)


class ConfirmingVenue(PositionVenue):
    def __init__(self, key, label, result, chain_position=0.0):
        super().__init__(key, label, result, chain_position)
        self.terminal_results = {}
        self.fetch_calls = 0

    async def resolve_order(self, order_ref):
        return self.terminal_results.get(order_ref)

    async def fetch_position(self):
        self.fetch_calls += 1
        return await super().fetch_position()


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


class LiveLifecycleVenue(LifecycleVenue):
    def __init__(self, key, label, chain_position):
        super().__init__(key, label)
        self.chain_position = chain_position
        self.send_calls = 0

    def init_signer(self):
        return

    async def fetch_position(self):
        return self.chain_position

    async def fetch_equity(self):
        return None

    async def warm_http(self):
        return

    def px_round(self, px, round_up):
        return px

    async def send_taker(self, **_kwargs):
        self.send_calls += 1
        return OrderResult(status="filled", filled_base=0.0)


class BurstVenue(LifecycleVenue):
    def __init__(self, key, label):
        super().__init__(key, label)
        self.finished = asyncio.Event()

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
            self.finished.set()
            await stop.wait()

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


class CloseFailVenue(InvalidBurstVenue):
    async def close(self):
        self.closed = True
        raise OSError(f"{self.key} close failed")


class BackgroundOutcomeVenue(LifecycleVenue):
    def __init__(self, key, label, outcome):
        super().__init__(key, label)
        self.outcome = outcome

    def start_tasks(self, _stop, _notify, _live):
        if self.key != "entropy":
            return []

        async def finish():
            await asyncio.sleep(0)
            if isinstance(self.outcome, BaseException):
                raise self.outcome

        return [asyncio.create_task(finish(), name="book-entropy")]


class BackgroundCloseFailVenue(BackgroundOutcomeVenue):
    async def close(self):
        self.closed = True
        raise OSError("close failed")


class LoadFailVenue(LifecycleVenue):
    async def load_market(self):
        if self.key == "entropy":
            raise RuntimeError("market load failed")
        await asyncio.sleep(0)
        await super().load_market()


class StartFailVenue(LifecycleVenue):
    def __init__(self, key, label):
        super().__init__(key, label)
        self.started_task = None

    def start_tasks(self, _stop, _notify, _live):
        if self.key == "hedge":
            raise RuntimeError("task startup failed")

        async def wait_forever():
            await asyncio.Event().wait()

        self.started_task = asyncio.create_task(
            wait_forever(), name="book-entropy")
        return [self.started_task]


class CleanupBlockingVenue(LifecycleVenue):
    def __init__(self, key, label, fail=False):
        super().__init__(key, label)
        self.fail = fail
        self.cancellation_started = asyncio.Event()
        self.release_cancellation = asyncio.Event()

    def start_tasks(self, _stop, _notify, _live):
        if self.key != "entropy":
            return []

        async def block_during_cancellation():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_started.set()
                await self.release_cancellation.wait()
                raise

        tasks = [asyncio.create_task(
            block_during_cancellation(), name="slow-cancel-entropy")]
        if self.fail:
            async def fail_after_start():
                await asyncio.sleep(0)
                raise RuntimeError("background failed")

            tasks.append(asyncio.create_task(
                fail_after_start(), name="fail-entropy"))
        return tasks


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


def make_dynamic_engine(tmp_path, *, reference=True):
    cfg = make_cfg()
    cfg.strategy_mode = "residual_dynamic"
    cfg.strategy_window_minutes = 10
    cfg.strategy_min_samples = 4
    cfg.strategy_regime_window_minutes = 2
    cfg.strategy_regime_recovery_minutes = 1
    cfg.strategy_state_file = str(tmp_path / "campaign-state.json")
    cfg.strategy_event_csv = str(tmp_path / "strategy-events.csv")
    cfg.recorder_csv = str(tmp_path / "minutes.csv")
    cfg.recorder_signal_csv = str(tmp_path / "signals.csv")
    cfg.premium_persist_sec = 0.0
    cfg.cooldown_sec = 0.0
    cfg.max_order_notional = 500.0
    eng = Engine(cfg, record_only=True)
    filled = OrderResult(status="filled", filled_base=0.0)
    eng.entropy = PositionVenue("entropy", "ENTROPY", filled)
    eng.hedge = PositionVenue("hedge", "RH", filled)
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 0.01, 0.01, 10.0
    if reference:
        now = time.monotonic()
        eng.entropy.reference.apply(
            ReferenceUpdate(oracle_px=100.0), source="websocket",
            received_mono=now)
        eng.hedge.reference.apply(
            ReferenceUpdate(index_px=100.0), source="websocket",
            received_mono=now)
    eng._initialize_dynamic_strategy(now_wall=time.time())
    return eng


def seed_dynamic_model(eng):
    now_minute = int(time.time() // 60)
    for offset, value in enumerate((-20.0, 0.0, 20.0, 40.0)):
        eng.residual_model.observe(
            minute=now_minute - 3 + offset,
            residual_bps=value,
            valid=True)


def set_dynamic_sell_market(eng, residual_bps):
    hedge_bid, hedge_ask = 99.99, 100.0
    entropy_bid = hedge_ask * (1.0 + residual_bps / 1e4)
    eng.entropy.set_book(entropy_bid, entropy_bid + 0.01, sz=20.0)
    eng.hedge.set_book(hedge_bid, hedge_ask, sz=20.0)


def test_residual_record_only_shadow_open_and_close_never_sends_orders(
        tmp_path):
    async def go():
        eng = make_dynamic_engine(tmp_path)
        try:
            seed_dynamic_model(eng)
            set_dynamic_sell_market(eng, 45.0)

            await eng._evaluate()

            assert eng.campaign is not None
            assert eng.campaign.mode == "shadow"
            assert eng.entropy.send_calls == 0
            assert eng.hedge.send_calls == 0
            eng.cfg.cooldown_sec = 3600.0
            set_dynamic_sell_market(eng, 10.0)

            await eng._evaluate()

            assert eng.campaign is None
            assert eng.entropy.send_calls == 0
            assert eng.hedge.send_calls == 0
            assert eng.trades == 0
            assert eng.entropy.position == 0
            assert eng.hedge.position == 0
        finally:
            eng._close_dynamic_strategy()

    asyncio.run(go())


@pytest.mark.parametrize("ready,reference", [(False, True), (True, False)])
def test_shadow_model_and_reference_gates_never_create_campaign(
        tmp_path, ready, reference):
    async def go():
        eng = make_dynamic_engine(tmp_path, reference=reference)
        try:
            if ready:
                seed_dynamic_model(eng)
            set_dynamic_sell_market(eng, 45.0)

            await eng._evaluate()

            assert eng.campaign is None
            assert eng.entropy.send_calls == 0
            assert eng.hedge.send_calls == 0
        finally:
            eng._close_dynamic_strategy()

    asyncio.run(go())


def test_shadow_does_not_require_live_account_readiness(tmp_path):
    async def go():
        eng = make_dynamic_engine(tmp_path)
        try:
            seed_dynamic_model(eng)
            set_dynamic_sell_market(eng, 45.0)
            eng.entropy.ready_to_trade = lambda: False
            eng.hedge.ready_to_trade = lambda: False

            await eng._evaluate()

            assert eng.campaign is not None
            assert eng.entropy.send_calls == 0
            assert eng.hedge.send_calls == 0
        finally:
            eng._close_dynamic_strategy()

    asyncio.run(go())


def test_shadow_state_uses_separate_file_and_survives_restart(tmp_path):
    async def go():
        first = make_dynamic_engine(tmp_path)
        try:
            seed_dynamic_model(first)
            set_dynamic_sell_market(first, 45.0)
            await first._evaluate()
            campaign_id = first.campaign.campaign_id
        finally:
            first._close_dynamic_strategy()

        assert not (tmp_path / "campaign-state.json").exists()
        assert (tmp_path / "campaign-state.shadow.json").exists()

        second = make_dynamic_engine(tmp_path)
        try:
            assert second.campaign.campaign_id == campaign_id
        finally:
            second._close_dynamic_strategy()

    asyncio.run(go())


def test_dynamic_minute_observation_commits_previous_close_and_gaps(
        tmp_path):
    eng = make_dynamic_engine(tmp_path)
    try:
        set_dynamic_sell_market(eng, 12.0)
        expected_close = (
            eng.entropy.book.mid() / eng.hedge.book.mid() - 1.0) * 1e4
        base = 60_000.0
        now_mono = time.monotonic()
        eng._advance_dynamic_model(now_wall=base, now_mono=now_mono)
        set_dynamic_sell_market(eng, 18.0)
        eng._advance_dynamic_model(
            now_wall=base + 60.0 * 3, now_mono=now_mono)

        assert eng.residual_model.snapshot(
            now_minute=int(base // 60)).samples == 1
        assert eng.residual_model.snapshot(
            now_minute=int(base // 60)).median_bps == pytest.approx(
                expected_close)
        assert eng.residual_model.snapshot(
            now_minute=int(base // 60) + 2).samples == 1
    finally:
        eng._close_dynamic_strategy()


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_reference_recovery_repeats_until_websocket_is_fresh():
    async def go():
        eng = make_engine(record_only=True)
        eng.cfg.reference_rest_recovery_sec = 0.01
        eng.cfg.reference_stale_sec = 0.1
        venue = eng.entropy
        venue.reference.apply(
            ReferenceUpdate(oracle_px=100.0), source="rest",
            received_mono=time.monotonic() - 1.0)
        websocket_restored = asyncio.Event()

        async def refresh():
            venue.reference_refreshes += 1
            if venue.reference_refreshes == 2:
                venue.reference.apply(
                    ReferenceUpdate(oracle_px=100.1), source="websocket")
                websocket_restored.set()
            return True

        venue.refresh_reference_rest = refresh
        task = asyncio.create_task(eng._reference_recovery_loop(venue))
        await asyncio.wait_for(websocket_restored.wait(), timeout=0.2)
        await asyncio.sleep(0.03)
        assert venue.reference_refreshes == 2
        eng.stop.set()
        await asyncio.wait_for(task, timeout=0.2)

    asyncio.run(go())


def test_expected_reference_rest_failure_does_not_stop_engine(caplog):
    async def go():
        eng = make_engine(record_only=True)
        eng.cfg.reference_rest_recovery_sec = 0.01
        venue = eng.entropy
        attempts = 0

        async def refresh():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise engine_module.aiohttp.ClientConnectionError("offline")
            eng.stop.set()
            return True

        venue.refresh_reference_rest = refresh
        await asyncio.wait_for(
            eng._reference_recovery_loop(venue), timeout=0.2)

        assert attempts == 2
        assert "reference REST recovery failed" in caplog.text

    asyncio.run(go())


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


def test_reference_state_does_not_change_trade_plan():
    eng = make_engine()
    eng.entropy.set_book(100.20, 100.21)
    eng.hedge.set_book(99.99, 100.00)

    before = eng._plan(eng.hedge, eng.entropy, 500.0)
    old = time.monotonic() - 3600.0
    eng.entropy.reference.apply(
        ReferenceUpdate(oracle_px=1000.0), source="rest",
        received_mono=old)
    eng.hedge.reference.apply(
        ReferenceUpdate(index_px=1.0), source="rest",
        received_mono=old)
    after = eng._plan(eng.hedge, eng.entropy, 500.0)

    assert after == before


def test_engine_reference_logs_are_stateful_and_observation_only(caplog):
    caplog.set_level(logging.INFO)
    eng = make_engine(record_only=True)
    eng._reference_alerts = ReferenceAlertState(
        alert_bps=20.0, persist_sec=0.0)

    eng._observe_reference(now_mono=10.0)
    eng._observe_reference(now_mono=10.1)
    assert caplog.text.count("reference data stale or incomplete") == 1

    eng.entropy.reference.apply(
        ReferenceUpdate(oracle_px=100.0), source="rest",
        received_mono=10.2)
    eng.hedge.reference.apply(
        ReferenceUpdate(index_px=100.0), source="rest",
        received_mono=10.2)
    eng.entropy.set_book(101.0, 101.1)
    eng.hedge.set_book(99.9, 100.0)
    eng._observe_reference(now_mono=10.2)
    eng._observe_reference(now_mono=10.3)

    assert caplog.text.count("reference data recovered") == 1
    assert caplog.text.count("sell_entropy reference residual alert") == 1
    assert eng.stop.is_set() is False


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
        eng._scan(__import__("time").monotonic())
        return eng._scan(__import__("time").monotonic())
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


def test_scan_accepts_post_trade_books_when_wall_clock_rolls_back(monkeypatch):
    wall_clock = [100.0]
    monotonic_clock = [10.0]
    monkeypatch.setattr(engine_module.time, "time", lambda: wall_clock[0])
    monkeypatch.setattr(
        engine_module.time, "monotonic", lambda: monotonic_clock[0])
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.entropy.last_traded_ts = eng.hedge.last_traded_ts = 10.0

    wall_clock[0] = 5.0
    monotonic_clock[0] = 11.0
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)

    async def scan_twice():
        eng._scan(monotonic_clock[0])
        return eng._scan(monotonic_clock[0])

    assert asyncio.run(scan_twice()) is not None


@pytest.mark.parametrize("interruption", ["stale", "unready", "down"])
def test_scan_restarts_persistence_after_market_continuity_break(interruption):
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.cfg.premium_persist_sec = 0.3
    eng._schedule_poke = lambda _delay: None
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)

    assert eng._scan(10.0) is None
    assert eng._armed["sell_entropy"] == pytest.approx(10.0)

    if interruption == "stale":
        eng.entropy.book.alive_mono -= eng.cfg.staleness_sec + 1.0
    elif interruption == "unready":
        eng.entropy.ready_to_trade = lambda: False
    else:
        eng._venue_down["entropy"] = time.monotonic()
    assert eng._scan(11.0) is None

    if interruption == "stale":
        eng.entropy.set_book(100.14, 100.16)
    elif interruption == "unready":
        eng.entropy.ready_to_trade = lambda: True
    else:
        eng._venue_down.clear()

    assert eng._scan(11.01) is None
    assert eng._armed["sell_entropy"] == pytest.approx(11.01)
    assert eng._scan(11.32) is not None


def test_entering_recovery_clears_armed_persistence():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    eng.cfg.premium_persist_sec = 0.3
    eng._schedule_poke = lambda _delay: None
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)

    assert eng._scan(10.0) is None
    assert eng._armed["sell_entropy"] == pytest.approx(10.0)

    eng._enter_recovery("test continuity break")

    assert all(armed is None for armed in eng._armed.values())
    eng._recovery_required = False
    assert eng._scan(11.0) is None
    assert eng._armed["sell_entropy"] == pytest.approx(11.0)


def test_venue_outage_clears_armed_persistence():
    async def go():
        eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
        eng.RECONCILE_GRACE_SEC = 0.0
        eng._armed["sell_entropy"] = 10.0

        async def unavailable():
            raise OSError("venue unavailable")

        eng.entropy.fetch_position = unavailable
        for _ in range(3):
            assert await eng._reconcile_venue(
                eng.entropy, strict=False) is False

        assert "entropy" in eng._venue_down
        assert all(armed is None for armed in eng._armed.values())

    asyncio.run(go())


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


def test_scan_blocks_while_position_recovery_is_required():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng._recovery_required = True

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


def test_execute_treats_raised_leg_exception_as_unknown():
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

    assert unresolved is True
    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["status"] == "adapter-exception/filled"


def test_unreferenced_unknown_stops_without_snapshot_recovery():
    async def go():
        eng = make_engine()
        buy = ConfirmingVenue(
            "hedge", "RH", OrderResult.unknown("accepted-unknown"),
            chain_position=0.0)
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())

        assert eng.stop.is_set()
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False
        assert eng._pending_snapshot_venues == set()
        assert buy.fetch_calls == 0
        assert isinstance(eng._primary_error, RuntimeError)
        assert "no order reference" in str(eng._primary_error)

    asyncio.run(go())


def test_unreferenced_unknown_still_drains_referenced_leg():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="hedge-42"))
        sell = ConfirmingVenue(
            "entropy", "ENTROPY", OrderResult.unknown("adapter-exception"))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())
        buy.terminal_results["hedge-42"] = OrderResult(
            status="canceled", filled_base=0.0)
        await asyncio.wait_for(
            eng._drain_executions(poll_sec=0.001), timeout=0.1)

        assert eng.stop.is_set()
        assert eng._pending_order_confirmations == []
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True

    asyncio.run(go())


def test_strategy_loop_propagates_unexpected_evaluate_error():
    async def go():
        eng = make_engine()

        async def broken_evaluate():
            raise RuntimeError("evaluate exploded")

        eng._evaluate = broken_evaluate
        eng._update_evt.set()
        with pytest.raises(RuntimeError, match="evaluate exploded"):
            await asyncio.wait_for(eng._strategy_loop(), timeout=0.05)

    asyncio.run(go())


def test_execute_locked_enters_recovery_and_propagates_unexpected_error():
    async def go():
        eng = make_engine()
        buy, sell = eng.hedge, eng.entropy
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        async def broken_execute(*_args):
            raise RuntimeError("settlement exploded")

        eng._execute = broken_execute
        with pytest.raises(RuntimeError, match="settlement exploded"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True
        assert not eng._vlock(buy.key).locked()
        assert not eng._vlock(sell.key).locked()

    asyncio.run(go())


def test_invalid_dual_leg_result_starts_settlement_grace_before_recovery():
    async def go():
        eng = make_engine()
        buy = ExecutingVenue("hedge", "RH", {"status": "filled"})
        sell = ExecutingVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        dispatched_at = time.monotonic()

        with pytest.raises(TypeError, match="expected OrderResult"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert buy.last_traded_ts >= dispatched_at
        assert sell.last_traded_ts == buy.last_traded_ts
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True

    asyncio.run(go())


def test_detached_execution_failure_is_supervised_after_strategy_cancel():
    async def go():
        eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
        eng.entropy.set_book(100.14, 100.16)
        eng.hedge.set_book(99.99, 100.01)
        eng._armed["sell_entropy"] = 0.0

        async def fail_after_strategy_cancel(*_args):
            await asyncio.sleep(0.02)
            raise RuntimeError("detached execution failed")

        eng._execute_locked = fail_after_strategy_cancel
        strategy_waiter = asyncio.create_task(eng._evaluate())
        while not eng._exec_tasks:
            await asyncio.sleep(0)
        strategy_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await strategy_waiter
        await asyncio.wait_for(eng.stop.wait(), timeout=0.2)

        assert isinstance(eng._primary_error, RuntimeError)
        assert str(eng._primary_error) == "detached execution failed"
        assert eng.stop.is_set()

    asyncio.run(go())


def test_successful_strict_recovery_unblocks_trading():
    async def go():
        eng = make_engine()
        eng._recovery_required = True
        eng._shutdown_reconcile_required = True
        calls = []

        async def reconcile(*, hedge, strict=False):
            calls.append((hedge, strict))
            return True

        eng._reconcile_positions = reconcile

        recovered = await eng._recover_positions(strict=True)

        assert recovered is True
        assert calls == [(False, True)]
        assert eng._recovery_required is False
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


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
            return True

        eng._reconcile_positions = reconcile
        eng._shutdown_reconcile_required = True

        await eng._drain_executions(poll_sec=0.005)

        assert reconciles == [(False, True)]
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_shutdown_drain_backs_off_while_order_is_still_pending():
    class PendingVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_calls = 0
            self.first_call = asyncio.Event()
            self.second_call = asyncio.Event()

        async def resolve_order(self, _order_ref):
            self.resolve_calls += 1
            (self.first_call if self.resolve_calls == 1
             else self.second_call).set()
            await asyncio.sleep(0)
            return None

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        pending = PendingVenue()
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.05))
        try:
            await asyncio.wait_for(pending.first_call.wait(), timeout=0.1)
            for _ in range(5):
                eng._live_progress_update()
                await asyncio.sleep(0)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    pending.second_call.wait(), timeout=0.01)
            assert pending.resolve_calls == 1
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

    asyncio.run(go())


def test_pending_terminal_notification_wakes_shutdown_drain_immediately():
    class TerminalVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_calls = 0
            self.first_call = asyncio.Event()
            self.terminal = False

        async def resolve_order(self, _order_ref):
            self.resolve_calls += 1
            self.first_call.set()
            if self.terminal:
                return OrderResult(status="canceled")
            return None

    async def go():
        eng = make_engine()
        pending = TerminalVenue()
        pending.last_traded_ts = time.monotonic()
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))

        await asyncio.wait_for(pending.first_call.wait(), timeout=0.1)
        pending.terminal = True
        eng._live_progress_update("order", pending.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert pending.resolve_calls == 2
        assert eng._pending_order_confirmations == []

    asyncio.run(go())


def test_pending_terminal_notification_wakes_live_reconcile_immediately():
    class TerminalVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_called = asyncio.Event()

        async def resolve_order(self, order_ref):
            self.resolve_called.set()
            return await super().resolve_order(order_ref)

    async def go():
        eng = make_engine()
        eng.cfg.reconcile_sec = 30.0
        pending = TerminalVenue()
        pending.terminal_results["42"] = OrderResult(status="canceled")
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")
        eng._reconcile_evt.clear()
        reconcile = asyncio.create_task(eng._reconcile_loop())
        try:
            await asyncio.sleep(0)
            eng._live_progress_update("order", pending.key)
            await asyncio.wait_for(pending.resolve_called.wait(), timeout=0.1)
        finally:
            eng.request_stop()
            done, _ = await asyncio.wait({reconcile}, timeout=0.1)
            if reconcile not in done:
                reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)

        assert eng._pending_order_confirmations == []

    asyncio.run(go())


def test_nonterminal_resolve_result_stops_for_manual_recovery():
    async def go():
        eng = make_engine()
        pending = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="42"))
        pending.terminal_results["42"] = OrderResult.unknown(
            "still-unknown", order_ref="42")
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert eng.stop.is_set() is True
        assert eng._auto_repair_disabled is True
        assert eng._pending_order_confirmations == []
        assert eng._shutdown_reconcile_required is False
        assert isinstance(eng._primary_error, RuntimeError)
        assert "terminal OrderResult" in str(eng._primary_error)

    asyncio.run(go())


def test_contract_failure_preserves_atomic_pending_order_progress():
    class TrackingVenue(ConfirmingVenue):
        def __init__(self, key, label, result):
            super().__init__(key, label, result)
            self.resolved_refs = []

        async def resolve_order(self, order_ref):
            self.resolved_refs.append(order_ref)
            return await super().resolve_order(order_ref)

    async def go():
        eng = make_engine()
        first = TrackingVenue(
            "entropy", "ENTROPY",
            OrderResult.unknown("timeout", order_ref="first"))
        remaining = TrackingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="second"))
        eng.entropy, eng.hedge = first, remaining
        eng.venues = {"entropy": first, "hedge": remaining}
        eng._register_unresolved_order(
            first, first.result, is_buy=True, applied_fill=0.0)
        eng._register_unresolved_order(
            remaining, remaining.result, is_buy=False, applied_fill=0.1)
        eng._register_unresolved_order(
            remaining,
            OrderResult.unknown("timeout", order_ref="third"),
            is_buy=True, applied_fill=0.0, is_residual_hedge=True)
        first.terminal_results["first"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0,
            order_ref="first")
        remaining.terminal_results["second"] = OrderResult.unknown(
            "still-open", order_ref="second")
        remaining.terminal_results["third"] = OrderResult(
            status="filled", filled_base=0.2, avg_px=100.0,
            order_ref="third")
        eng._enter_recovery("test ordered pending recovery")

        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert first.position == pytest.approx(0.5)
        assert first.cash == pytest.approx(-50.0)
        assert first.volume_usd == pytest.approx(50.0)
        assert first.resolved_refs == ["first"]
        assert remaining.resolved_refs == ["second"]
        assert eng._pending_order_confirmations == []
        assert [item.order_ref
                for item in eng._manual_order_confirmations] == [
                    "second", "third"]
        manual_second = eng._manual_order_confirmations[0]
        assert manual_second.venue is remaining
        assert manual_second.is_buy is False
        assert manual_second.applied_fill == pytest.approx(0.1)
        assert eng._manual_order_confirmations[1].is_residual_hedge is True
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True
        assert eng.stop.is_set() is True

    asyncio.run(go())


def test_resolve_order_schema_error_stops_for_manual_recovery():
    class BrokenSchemaVenue(ConfirmingVenue):
        async def resolve_order(self, _order_ref):
            raise KeyError("orders")

    async def go():
        eng = make_engine()
        pending = BrokenSchemaVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="42"))
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert eng.stop.is_set() is True
        assert eng._auto_repair_disabled is True
        assert eng._pending_order_confirmations == []
        assert isinstance(eng._primary_error, RuntimeError)
        assert "order 42" in str(eng._primary_error)
        assert isinstance(eng._primary_error.__cause__, KeyError)

    asyncio.run(go())


def test_resolve_order_index_error_stops_shutdown_drain():
    class BrokenSchemaVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_calls = 0

        async def resolve_order(self, _order_ref):
            self.resolve_calls += 1
            raise IndexError("empty orders")

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        pending = BrokenSchemaVenue()
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        await asyncio.wait_for(
            eng._drain_executions(poll_sec=0.001), timeout=0.1)

        assert pending.resolve_calls == 1
        assert eng._pending_order_confirmations == []
        assert [item.order_ref
                for item in eng._manual_order_confirmations] == ["42"]
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True
        assert eng.stop.is_set() is True
        assert isinstance(eng._primary_error.__cause__, IndexError)

    asyncio.run(go())


def test_only_target_book_wakes_live_post_order_residual_recovery():
    class HedgingVenue(PositionVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
            self.sent = asyncio.Event()

        async def send_taker(self, **kwargs):
            result = await super().send_taker(**kwargs)
            self.sent.set()
            return result

    async def go():
        eng = make_engine()
        eng.cfg.reconcile_sec = 30.0
        buy = HedgingVenue()
        buy.position = 0.5
        buy.set_book(99.9, 100.0)
        sell = PositionVenue(
            "entropy", "ENTROPY", OrderResult(status="canceled"))
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        cutoff = buy.book.last_update_mono + 1.0
        eng._residual_book_after[buy.key] = cutoff
        eng._residual_waiting_book_venues = {buy.key}
        eng._post_order_recovery_active = True
        eng._recovery_required = True
        reconcile = asyncio.create_task(eng._reconcile_loop())
        try:
            await asyncio.sleep(0)
            sell.set_book(100.2, 100.3)
            eng._live_progress_update("book", sell.key)
            await asyncio.sleep(0.02)
            assert buy.send_calls == 0

            buy.set_book(99.9, 100.0)
            buy.book.last_update_mono = cutoff + 1.0
            eng._live_progress_update("book", buy.key)
            await asyncio.wait_for(buy.sent.wait(), timeout=0.1)
        finally:
            eng.request_stop()
            done, _ = await asyncio.wait({reconcile}, timeout=0.1)
            if reconcile not in done:
                reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)

        assert buy.position == pytest.approx(0.0)

    asyncio.run(go())


def test_live_post_order_recovery_does_not_spin_without_new_book():
    async def go():
        eng = make_engine()
        eng.cfg.reconcile_sec = 30.0
        buy = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
        buy.position = 0.5
        buy.set_book(99.9, 100.0)
        eng.hedge = buy
        eng.venues = {"entropy": eng.entropy, "hedge": buy}
        eng._residual_book_after[buy.key] = buy.book.last_update_mono + 1.0
        eng._residual_waiting_book_venues = {buy.key}
        eng._post_order_recovery_active = True
        eng._recovery_required = True
        attempts = 0
        real_maybe_hedge = eng._maybe_hedge

        async def count_attempts():
            nonlocal attempts
            attempts += 1
            return await real_maybe_hedge()

        eng._maybe_hedge = count_attempts
        eng._reconcile_evt.set()
        reconcile = asyncio.create_task(eng._reconcile_loop())
        try:
            await asyncio.sleep(0.03)
        finally:
            eng.request_stop()
            done, _ = await asyncio.wait({reconcile}, timeout=0.1)
            if reconcile not in done:
                reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)

        assert attempts == 1

    asyncio.run(go())


def test_live_reconcile_immediately_checks_new_unknown_residual_order():
    class UnknownResidualVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="repair-1"))
            self.resolve_called = asyncio.Event()

        async def resolve_order(self, order_ref):
            self.resolve_called.set()
            return await super().resolve_order(order_ref)

    async def go():
        eng = make_engine()
        eng.cfg.reconcile_sec = 30.0
        venue = UnknownResidualVenue()
        venue.position = 0.5
        venue.set_book(99.9, 100.0)
        venue.terminal_results["repair-1"] = OrderResult(status="canceled")
        eng.hedge = venue
        eng.venues = {"entropy": eng.entropy, "hedge": venue}
        eng._post_order_recovery_active = True
        eng._recovery_required = True
        eng._reconcile_evt.set()
        reconcile = asyncio.create_task(eng._reconcile_loop())
        try:
            await asyncio.wait_for(venue.resolve_called.wait(), timeout=0.1)
        finally:
            eng.request_stop()
            done, _ = await asyncio.wait({reconcile}, timeout=0.1)
            if reconcile not in done:
                reconcile.cancel()
            await asyncio.gather(reconcile, return_exceptions=True)

        assert venue.send_calls == 1
        assert eng._pending_order_confirmations == []

    asyncio.run(go())


def test_post_order_residual_below_minimum_exits_shutdown_drain():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.5)
        buy.kind = "lighter"
        buy.min_base = 1.0
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        await asyncio.sleep(0.01)
        assert drain.done() is False

        buy.set_book(99.9, 100.0)
        buy.book.last_update_mono = eng._residual_book_after[buy.key] + 1.0
        eng._live_progress_update("book", buy.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert buy.send_calls == 1
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False
        assert eng._post_order_recovery_active is False

    asyncio.run(go())


def test_required_recovery_book_failure_aborts_shutdown_drain():
    async def fail_when_recovery_is_waiting(eng, release):
        while not eng._residual_waiting_book_venues:
            await asyncio.sleep(0)
        await release.wait()
        raise RuntimeError("book feed failed")

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng.entropy.position = 0.5
        eng._residual_book_after["entropy"] = (
            eng.entropy.book.last_update_mono + 1.0)
        eng._post_order_recovery_active = True
        eng._pause_for_recovery("test residual")
        eng._shutdown_reconcile_required = True

        release = asyncio.Event()
        feed = asyncio.create_task(
            fail_when_recovery_is_waiting(eng, release), name="book-entropy")
        tasks = []
        eng._track_task(tasks, feed)
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        while not eng._residual_waiting_book_venues:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(feed, return_exceptions=True)
        await asyncio.wait_for(drain, timeout=0.1)

        assert isinstance(eng._primary_error, RuntimeError)
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_required_order_feed_failure_aborts_shutdown_drain():
    class PendingVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_started = asyncio.Event()

        async def resolve_order(self, _order_ref):
            self.resolve_started.set()
            return None

    async def fail_after_first_lookup(pending, release):
        await pending.resolve_started.wait()
        await release.wait()
        raise RuntimeError("account feed failed")

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        pending = PendingVenue()
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        release = asyncio.Event()
        feed = asyncio.create_task(
            fail_after_first_lookup(pending, release), name="acct-hedge")
        tasks = []
        eng._track_task(tasks, feed)
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        await pending.resolve_started.wait()
        release.set()
        await asyncio.gather(feed, return_exceptions=True)
        await asyncio.wait_for(drain, timeout=0.1)

        assert isinstance(eng._primary_error, RuntimeError)
        assert len(eng._pending_order_confirmations) == 1
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_required_book_feed_normal_exit_during_cleanup_aborts_drain():
    async def exit_when_recovery_is_waiting(eng, release):
        while not eng._residual_waiting_book_venues:
            await asyncio.sleep(0)
        await release.wait()

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng.entropy.position = 0.5
        eng._residual_book_after["entropy"] = (
            eng.entropy.book.last_update_mono + 1.0)
        eng._post_order_recovery_active = True
        eng._pause_for_recovery("test residual")
        eng._shutdown_reconcile_required = True

        release = asyncio.Event()
        feed = asyncio.create_task(
            exit_when_recovery_is_waiting(eng, release), name="book-entropy")
        tasks = []
        eng._track_task(tasks, feed)
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        while not eng._residual_waiting_book_venues:
            await asyncio.sleep(0)
        eng.request_stop()
        release.set()
        await feed
        await asyncio.wait_for(drain, timeout=0.1)

        assert feed in eng._task_failures
        assert isinstance(eng._primary_error, RuntimeError)
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_order_feed_failure_waits_for_concurrent_recovery_lock():
    class BlockingTerminalVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_started = asyncio.Event()
            self.release_resolve = asyncio.Event()

        async def resolve_order(self, _order_ref):
            self.resolve_started.set()
            await self.release_resolve.wait()
            return OrderResult(
                status="filled", filled_base=0.5, avg_px=100.0)

    async def fail_after_lookup(pending):
        await pending.resolve_started.wait()
        raise RuntimeError("account feed failed")

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        pending = BlockingTerminalVenue()
        pending.set_book(99.9, 100.0)
        eng.entropy.set_book(100.2, 100.3)
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        recovery = asyncio.create_task(
            eng._recover_positions(strict=True), name="background-recovery")
        await pending.resolve_started.wait()
        feed = asyncio.create_task(
            fail_after_lookup(pending), name="acct-hedge")
        tasks = []
        eng._track_task(tasks, feed)
        await asyncio.gather(feed, return_exceptions=True)
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        await asyncio.sleep(0.02)
        assert drain.done() is False

        pending.release_resolve.set()
        await asyncio.wait_for(drain, timeout=0.1)
        await recovery

        assert pending.send_calls == 0
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True

    asyncio.run(go())


def test_pending_lookup_ignores_another_venues_terminal_notification():
    class TerminalVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"))
            self.resolve_calls = 0
            self.first_call = asyncio.Event()
            self.terminal = False

        async def resolve_order(self, _order_ref):
            self.resolve_calls += 1
            self.first_call.set()
            if self.terminal:
                return OrderResult(status="canceled")
            return None

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        pending = TerminalVenue()
        eng.hedge = pending
        eng.venues = {"entropy": eng.entropy, "hedge": pending}
        eng._register_unresolved_order(
            pending, pending.result, is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))

        await asyncio.wait_for(pending.first_call.wait(), timeout=0.1)
        eng._live_progress_update("order", "entropy")
        await asyncio.sleep(0.02)
        assert pending.resolve_calls == 1

        pending.terminal = True
        eng._live_progress_update("order", pending.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert pending.resolve_calls == 2

    asyncio.run(go())


def test_known_residual_shutdown_waits_for_target_post_submit_book():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.5)
        buy.kind = "lighter"
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())

        assert buy.position == pytest.approx(0.5)
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        await asyncio.sleep(0.01)
        assert drain.done() is False

        buy.set_book(99.9, 100.0)
        buy.book.last_update_mono = eng._residual_book_after[buy.key] + 1.0
        eng._live_progress_update("book", buy.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_resolved_pending_waits_only_for_target_book_without_rest_reconcile():
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"),
                chain_position=0.5)
            self.results = [
                self.result,
                OrderResult(status="filled", filled_base=0.5, avg_px=99.9),
            ]

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            return self.results.pop(0)

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = SequencedConfirmingVenue()
        buy.kind = "lighter"
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, execution_plan())
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)

        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.5))
        for _ in range(100):
            if not eng._pending_order_confirmations:
                break
            await asyncio.sleep(0.001)
        assert eng._pending_order_confirmations == []
        assert drain.done() is False

        sell.set_book(100.2, 100.3)
        eng._live_progress_update("book", sell.key)
        await asyncio.sleep(0.02)

        assert drain.done() is False
        assert buy.fetch_calls == 0
        assert buy.send_calls == 1

        buy.set_book(99.9, 100.0)
        eng._live_progress_update("book", buy.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert buy.fetch_calls == 0
        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)

    asyncio.run(go())


def test_shutdown_drain_waits_for_post_submission_book_before_hedging():
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"),
                chain_position=0.5)
            self.results = [
                self.result,
                OrderResult(status="filled", filled_base=0.5, avg_px=99.9),
            ]

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            return self.results.pop(0)

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = SequencedConfirmingVenue()
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, execution_plan())
        buy.set_book(99.9, 100.0)
        book_after = eng._residual_book_after[buy.key]
        buy.book.last_update_mono = book_after
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)

        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.01))
        for _ in range(100):
            if buy.position == pytest.approx(0.5):
                break
            await asyncio.sleep(0.001)
        assert buy.position == pytest.approx(0.5)
        assert drain.done() is False
        assert buy.send_calls == 1

        buy.set_book(99.9, 100.0)
        buy.book.last_update_mono = book_after + 1.0
        await asyncio.wait_for(drain, timeout=0.2)

        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_cleanup_keeps_feeds_alive_until_post_terminal_hedge_finishes():
    class TerminalVenue(ConfirmingVenue):
        def __init__(self, resolved):
            super().__init__(
                "hedge", "RH",
                OrderResult(status="filled", filled_base=0.5, avg_px=99.9),
                chain_position=0.5)
            self.resolved = resolved

        async def resolve_order(self, order_ref):
            self.resolved.set()
            return self.terminal_results.get(order_ref)

        async def close(self):
            return

    class ClosingVenue(PositionVenue):
        async def close(self):
            return

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.05
        eng.cfg.staleness_sec = 0.2
        resolved = asyncio.Event()
        buy = TerminalVenue(resolved)
        sell = ClosingVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0))
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        eng._register_unresolved_order(
            buy, OrderResult.unknown("timeout", order_ref="42"),
            is_buy=True, applied_fill=0.0)
        eng._enter_recovery("test pending order")

        feed_stop = getattr(eng, "_feed_stop", eng.stop)
        notify = getattr(eng, "_live_progress_update", eng._update_evt.set)
        first_hedge_check_done = asyncio.Event()
        real_maybe_hedge = eng._maybe_hedge

        async def observe_maybe_hedge():
            attempted = await real_maybe_hedge()
            if buy.position >= 0.5 and not attempted:
                first_hedge_check_done.set()
            return attempted

        eng._maybe_hedge = observe_maybe_hedge

        async def publish_post_terminal_book():
            await first_hedge_check_done.wait()
            if feed_stop.is_set():
                return
            buy.set_book(99.9, 100.0)
            buy.book.last_update_mono = buy.last_traded_ts + 1.0
            notify()
            await feed_stop.wait()

        feed = asyncio.create_task(
            publish_post_terminal_book(), name="book-hedge")
        real_drain = eng._drain_executions

        async def quick_drain():
            await real_drain(poll_sec=0.3)

        eng._drain_executions = quick_drain
        cleanup = asyncio.create_task(eng._cleanup([feed]))
        try:
            await asyncio.wait_for(resolved.wait(), timeout=0.1)
            assert feed_stop.is_set() is False
            done, _ = await asyncio.wait({cleanup}, timeout=0.5)
            assert cleanup in done
            await cleanup
        finally:
            if not cleanup.done():
                cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)

        assert buy.send_calls == 1
        assert buy.position == pytest.approx(0.0)
        assert feed_stop.is_set() is True

    asyncio.run(go())


def test_shutdown_drain_uses_book_published_after_submit_before_terminal():
    class BookBeforeTerminalVenue(ConfirmingVenue):
        def __init__(self):
            super().__init__(
                "hedge", "RH",
                OrderResult.unknown("timeout", order_ref="42"),
                chain_position=0.5)

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            if self.send_calls == 1:
                # This update is newer than submission, but the independent
                # account stream reaches terminal only after send_taker exits.
                await asyncio.sleep(0.03)
                self.set_book(99.9, 100.0)
                return OrderResult.unknown("timeout", order_ref="42")
            return OrderResult(
                status="filled", filled_base=0.5, avg_px=99.9)

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.staleness_sec = 1.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = BookBeforeTerminalVenue()
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}

        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, execution_plan())
        assert buy.send_calls == 1
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)

        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.3))
        try:
            done, _ = await asyncio.wait({drain}, timeout=0.2)
            assert drain in done
            await drain
        finally:
            if not drain.done():
                drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)

        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_shutdown_drain_waits_for_recovery_already_in_progress():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng._enter_recovery("test unknown outcome")
        reconcile_started = asyncio.Event()
        release_reconcile = asyncio.Event()

        async def reconcile(*, hedge, strict=False):
            reconcile_started.set()
            await release_reconcile.wait()
            return True

        eng._reconcile_positions = reconcile
        recovery = asyncio.create_task(eng._recover_positions(strict=True))
        await reconcile_started.wait()
        drain = asyncio.create_task(eng._drain_executions(poll_sec=0.005))
        await asyncio.sleep(0)

        assert drain.done() is False
        release_reconcile.set()
        await asyncio.gather(recovery, drain)
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_shutdown_drain_retries_unknown_outcome_reconciliation():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng._enter_recovery("test unknown outcome")
        calls = 0

        async def reconcile(*, hedge, strict=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary position fetch failure")
            return True

        eng._reconcile_positions = reconcile

        await asyncio.wait_for(
            eng._drain_executions(poll_sec=0.005), timeout=0.1)

        assert calls == 2
        assert eng._shutdown_reconcile_required is False
        assert eng._recovery_required is False

    asyncio.run(go())


def test_background_recovery_wakes_shutdown_retry_backoff():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng._enter_recovery("test unknown outcome")
        first_started = asyncio.Event()
        fail_first = asyncio.Event()
        calls = 0

        async def reconcile(*, hedge, strict=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await fail_first.wait()
                raise RuntimeError("temporary position fetch failure")
            return True

        eng._reconcile_positions = reconcile
        drain = asyncio.create_task(eng._drain_executions())
        await first_started.wait()
        recovery = asyncio.create_task(eng._recover_positions(strict=True))
        await asyncio.sleep(0)
        fail_first.set()

        await asyncio.wait_for(recovery, timeout=0.1)
        assert eng._shutdown_reconcile_required is False
        await asyncio.wait_for(drain, timeout=0.1)
        assert calls == 2

    asyncio.run(go())


def test_shutdown_rechecks_execution_tasks_after_recovery_wakeup():
    class BlockingHedgeVenue(ExecutingVenue):
        def __init__(self, key, label):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_taker(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().send_taker(**kwargs)

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = BlockingHedgeVenue("entropy", "ENTROPY")
        eng.hedge = ExecutingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0))
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._enter_recovery("test unknown outcome")
        first_started = asyncio.Event()
        fail_first = asyncio.Event()
        calls = 0

        async def reconcile(*, hedge, strict=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await fail_first.wait()
                raise RuntimeError("temporary position fetch failure")
            eng.entropy.position = 0.5
            return True

        eng._reconcile_positions = reconcile
        drain = asyncio.create_task(eng._drain_executions())
        await first_started.wait()
        recovery = asyncio.create_task(eng._recover_positions(strict=True))
        await asyncio.sleep(0)
        fail_first.set()

        await eng.entropy.started.wait()
        try:
            await asyncio.sleep(0)
            assert drain.done() is False
        finally:
            eng.entropy.release.set()
            await asyncio.gather(recovery, drain)

        assert calls == 2
        assert not eng._exec_tasks

    asyncio.run(go())


def test_unhedgeable_known_residual_pauses_new_entries():
    async def go():
        eng = make_engine()
        eng.entropy.position = 0.5
        eng.entropy.book.last_update_mono = 0.0

        await eng._maybe_hedge()

        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_hedge_reduces_same_direction_positions_across_all_venues():
    async def go():
        eng = make_engine()
        first = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.3, avg_px=99.9))
        second = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.2, avg_px=99.9))
        first.position = 0.3
        second.position = 0.2
        first.set_book(99.9, 100.0)
        second.set_book(99.9, 100.0)
        eng.entropy, eng.hedge = first, second
        eng.venues = {"entropy": first, "hedge": second}

        await eng._maybe_hedge()

        assert first.send_calls == 1
        assert second.send_calls == 1
        assert first.position == pytest.approx(0.0)
        assert second.position == pytest.approx(0.0)
        assert eng._auto_repair_disabled is False

    asyncio.run(go())


def test_hedge_transport_error_requires_manual_recovery():
    async def go():
        eng = make_engine()
        eng.entropy = ExecutingVenue(
            "entropy", "ENTROPY", OSError("connection reset after write"))
        eng.hedge = ExecutingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0))
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)

        with pytest.raises(OSError, match="connection reset after write"):
            await eng._maybe_hedge()
        await asyncio.sleep(0)

        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True
        assert eng._unreferenced_unknown is True
        assert eng.entropy.last_traded_ts > 0

    asyncio.run(go())


def test_hedge_adapter_exception_stops_without_snapshot_resubmission():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY", OSError("connection reset after write"),
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)

        with pytest.raises(OSError, match="connection reset after write"):
            await eng._maybe_hedge()
        await asyncio.sleep(0)

        assert eng.entropy.send_calls == 1
        assert eng.stop.is_set()
        assert eng._auto_repair_disabled is True
        assert eng._unreferenced_unknown is True
        assert eng._shutdown_reconcile_required is False

        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert eng.entropy.send_calls == 1

    asyncio.run(go())


def test_invalid_hedge_adapter_result_requires_unknown_outcome_reconciliation():
    async def go():
        eng = make_engine()
        eng.entropy = ExecutingVenue("entropy", "ENTROPY", {"status": "filled"})
        eng.hedge = ExecutingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0))
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)

        with pytest.raises(TypeError, match="expected OrderResult"):
            await eng._maybe_hedge()

        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True
        assert eng.entropy.last_traded_ts > 0

    asyncio.run(go())


def test_shutdown_drain_waits_for_reconcile_triggered_hedge():
    class BlockingHedgeVenue(ExecutingVenue):
        def __init__(self, key, label):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_taker(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().send_taker(**kwargs)

    async def go():
        eng = make_engine()
        eng.entropy = BlockingHedgeVenue("entropy", "ENTROPY")
        eng.hedge = ExecutingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0))
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)

        keep_caller_alive = asyncio.Event()

        async def reconcile_caller():
            await eng._maybe_hedge()
            await keep_caller_alive.wait()

        caller = asyncio.create_task(reconcile_caller())
        await eng.entropy.started.wait()
        assert caller not in eng._exec_tasks
        assert len(eng._exec_tasks) == 1
        drain = asyncio.create_task(eng._drain_executions(poll_sec=0.005))
        await asyncio.sleep(0)

        assert drain.done() is False
        eng.entropy.release.set()
        try:
            await asyncio.wait_for(drain, timeout=0.1)
            assert caller.done() is False
        finally:
            keep_caller_alive.set()
            await caller

    asyncio.run(go())


def test_known_hedge_rejection_does_not_loop_shutdown_reconciliation():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = ExecutingVenue(
            "entropy", "ENTROPY", OrderResult.send_failed("rejected"))
        eng.hedge = ExecutingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0))
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._enter_recovery("test unknown outcome")
        reconciles = 0

        async def reconcile_venue(v, strict):
            nonlocal reconciles
            reconciles += 1
            return True

        eng._reconcile_venue = reconcile_venue

        await asyncio.wait_for(
            eng._drain_executions(poll_sec=0.005), timeout=0.1)

        assert reconciles == 2
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


@pytest.mark.parametrize("result", [
    OrderResult.send_failed("rejected"),
    OrderResult(status="canceled", filled_base=0.0),
])
def test_terminal_hedge_failure_disables_runtime_resubmission(result):
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY", result, chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._pause_for_recovery("known residual")

        assert await eng._recover_positions_locked(strict=True) is False
        eng.entropy.last_traded_ts = 0.0
        eng.hedge.last_traded_ts = 0.0
        assert await eng._recover_positions_locked(strict=True) is False

        assert eng.entropy.send_calls == 1
        assert eng._auto_repair_disabled is True
        assert eng._recovery_required is True

    asyncio.run(go())


def test_unknown_hedge_terminal_cancel_disables_runtime_resubmission():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = ConfirmingVenue(
            "entropy", "ENTROPY",
            OrderResult.unknown("timeout", order_ref="repair-1"),
            chain_position=0.5)
        eng.hedge = ConfirmingVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)

        await eng._maybe_hedge()
        eng.entropy.terminal_results["repair-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        await eng._recover_positions_locked(strict=True)

        assert eng.entropy.send_calls == 1
        assert eng._pending_order_confirmations == []
        assert eng._auto_repair_disabled is True
        assert eng._recovery_required is True

    asyncio.run(go())


def test_reconcile_skipped_during_grace_is_rescheduled():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.03
        eng.entropy.last_traded_ts = time.monotonic()
        eng.hedge.last_traded_ts = time.monotonic()

        await eng._reconcile_positions(hedge=False)

        assert eng._reconcile_evt.is_set() is False
        await asyncio.sleep(0.05)
        assert eng._reconcile_evt.is_set() is True

    asyncio.run(go())


def test_incomplete_position_snapshot_never_starts_hedge():
    async def go():
        eng = make_engine()
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.position = 0.5
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng.entropy.last_traded_ts = time.monotonic()

        complete = await eng._reconcile_positions(hedge=True, strict=True)

        assert complete is False
        assert eng.entropy.send_calls == 0
        assert eng.hedge.send_calls == 0

    asyncio.run(go())


def test_post_trade_lighter_position_mismatch_is_not_adopted_or_hedged():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        entropy = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.5)
        lighter = PositionVenue(
            "hedge", "LIGHTER",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.0)
        entropy.kind = "hl"
        lighter.kind = "lighter"
        entropy.position = 0.5
        lighter.position = -0.5
        lighter.last_traded_ts = time.monotonic() - 10.0
        entropy.set_book(99.9, 100.0)
        lighter.set_book(99.9, 100.0)
        eng.entropy, eng.hedge = entropy, lighter
        eng.venues = {"entropy": entropy, "hedge": lighter}

        complete = await eng._reconcile_positions(hedge=True, strict=True)

        assert complete is False
        assert lighter.position == pytest.approx(-0.5)
        assert entropy.send_calls == 0
        assert lighter.send_calls == 0
        assert eng._recovery_required is True

        lighter.chain_position = -0.5
        complete = await eng._reconcile_positions(hedge=True, strict=True)

        assert complete is True
        assert entropy.send_calls == 0
        assert lighter.send_calls == 0

    asyncio.run(go())


def test_shutdown_exits_after_known_positions_when_repair_fails_before_send():
    class BadRoundVenue(PositionVenue):
        def px_round(self, px, round_up):
            raise ValueError("cannot round hedge price")

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = BadRoundVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._enter_recovery("test unknown outcome")

        await asyncio.wait_for(
            eng._drain_executions(), timeout=0.1)

        assert eng._shutdown_reconcile_required is False
        assert isinstance(eng._primary_error, ValueError)
        assert eng.entropy.send_calls == 0

    asyncio.run(go())


def test_lighter_coi_failure_with_other_leg_fill_is_repaired(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=Constants))

    class FilledVenue(PositionVenue):
        def __init__(self, key, label):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
            self.send_args = []

        async def send_taker(self, **kwargs):
            self.send_args.append(kwargs)
            result = await super().send_taker(**kwargs)
            self.set_book(99.9, 100.0)
            self.book.last_update_mono = time.monotonic() + 1.0
            return result

    async def go():
        eng = make_engine()
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = FilledVenue("entropy", "ENTROPY")
        sell = LighterVenue(eng.cfg.hedge, object(), 0.01)
        sell.signer = object()
        sell.market_id = 7
        buy.set_book(99.9, 100.0)
        sell.book.apply_hl(
            [[{"px": "100.2", "sz": "50"}],
             [{"px": "100.3", "sz": "50"}]])
        eng.entropy, eng.hedge = buy, sell
        eng.venues = {"entropy": buy, "hedge": sell}

        def fail_before_submit():
            raise OSError("COI database is locked")

        monkeypatch.setattr(sell, "_next_coi", fail_before_submit)
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())

        assert buy.send_calls == 2
        assert buy.send_args[0].get("reduce_only", False) is False
        assert buy.send_args[1]["reduce_only"] is True
        assert buy.position == pytest.approx(0.0)
        assert sell.position == pytest.approx(0.0)
        assert eng._unreferenced_unknown is False
        assert eng._auto_repair_disabled is False
        assert eng.stop.is_set() is False

    asyncio.run(go())


@pytest.mark.parametrize("invalid_position", [float("nan"), float("inf"),
                                               float("-inf")])
def test_strict_recovery_rejects_non_finite_positions(invalid_position):
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.0),
            chain_position=invalid_position)
        eng.hedge = PositionVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng._enter_recovery("test unknown outcome")

        with pytest.raises(RuntimeError, match="finite"):
            await eng._recover_positions(strict=True)

        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True

    asyncio.run(go())


def test_runtime_cancellation_of_background_task_stops_engine():
    async def go():
        eng = make_engine()
        task = asyncio.create_task(
            asyncio.Event().wait(), name="book-entropy")
        tasks = []
        eng._track_task(tasks, task)

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert eng.stop.is_set()
        assert isinstance(eng._primary_error, RuntimeError)
        assert "book-entropy" in str(eng._primary_error)
        assert task in eng._task_failures

    asyncio.run(go())


def test_background_cancellation_is_not_hidden_by_same_tick_stop_request():
    async def go():
        eng = make_engine()
        task = asyncio.create_task(
            asyncio.Event().wait(), name="book-entropy")
        tasks = []
        eng._track_task(tasks, task)

        task.cancel()
        eng.request_stop()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert isinstance(eng._primary_error, RuntimeError)
        assert "cancelled unexpectedly" in str(eng._primary_error)
        assert task in eng._task_failures

    asyncio.run(go())


def test_cleanup_reports_cancelled_task_even_if_done_callback_is_late():
    async def go():
        eng = Engine(make_cfg(), record_only=True)
        task = asyncio.create_task(
            asyncio.Event().wait(), name="book-entropy")
        tasks = []
        eng._track_task(tasks, task)

        task.cancel()
        eng.request_stop()
        await eng._cleanup(tasks)

        assert isinstance(eng._primary_error, RuntimeError)
        assert "cancelled unexpectedly" in str(eng._primary_error)
        assert task in eng._task_failures

    asyncio.run(go())


def test_unknown_lighter_order_waits_for_terminal_confirmation_not_rest_snapshot():
    async def go():
        eng = make_engine()
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="42"),
            chain_position=0.0)
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert buy.fetch_calls == 0
        assert eng._recovery_required is True

        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is True
        assert buy.fetch_calls == 0
        assert buy.position == pytest.approx(0.5)
        assert sell.position == pytest.approx(-0.5)

    asyncio.run(go())


def test_async_terminal_fill_requires_a_newer_book_before_trading_resumes(
        monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    async def go():
        eng = make_engine()
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="42"),
            chain_position=0.0)
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        await eng._execute_locked(buy, sell, execution_plan())
        clock[0] = 101.0
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        stale_book_ts = buy.book.last_update_mono
        assert stale_book_ts > buy.last_traded_ts

        clock[0] = 102.0
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is True
        assert buy.last_traded_ts >= stale_book_ts
        assert eng._scan(time.monotonic()) is None
        assert eng._scan(time.monotonic()) is None

    asyncio.run(go())


def test_residual_rejects_book_queued_before_send_taker_starts(monkeypatch):
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self, key, label, results, chain_position,
                     book_published):
            super().__init__(key, label, results[0], chain_position)
            self.results = list(results)
            self.book_published = book_published

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            if self.send_calls == 1:
                assert self.book_published.is_set()
            return self.results.pop(0)

    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.cfg.trades_csv = os.path.join(
            tempfile.mkdtemp(), "trades.csv")
        book_published = asyncio.Event()
        buy = SequencedConfirmingVenue(
            "hedge", "RH",
            [OrderResult.unknown("timeout", order_ref="42"),
             OrderResult(status="filled", filled_base=0.5, avg_px=99.9)],
            chain_position=0.5, book_published=book_published)
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        async def publish_queued_book():
            clock[0] = 101.0
            buy.set_book(99.9, 100.0)
            book_published.set()

        queued_book = asyncio.create_task(publish_queued_book())
        await eng._execute_locked(buy, sell, execution_plan())
        await queued_book
        clock[0] = 102.0
        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)

        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert buy.send_calls == 1
        assert buy.position == pytest.approx(0.5)

        clock[0] = 103.0
        buy.set_book(99.9, 100.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is True
        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)

    asyncio.run(go())


def test_pending_terminals_capture_each_venue_resolution_time(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    async def go():
        eng = make_engine()
        first = ConfirmingVenue(
            "entropy", "ENTROPY",
            OrderResult.unknown("timeout", order_ref="first"))
        second = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="second"))
        eng.entropy, eng.hedge = first, second
        eng.venues = {"entropy": first, "hedge": second}
        first.terminal_results["first"] = OrderResult(
            status="canceled", filled_base=0.0)
        second.terminal_results["second"] = OrderResult(
            status="canceled", filled_base=0.0)

        async def resolve_second(order_ref):
            clock[0] = 101.0
            first.set_book(100.2, 100.3)
            return second.terminal_results[order_ref]

        second.resolve_order = resolve_second
        eng._register_unresolved_order(
            first, first.result, is_buy=False, applied_fill=0.0)
        eng._register_unresolved_order(
            second, second.result, is_buy=True, applied_fill=0.0)

        assert await eng._resolve_pending_orders() is True
        assert first.last_traded_ts == 100.0
        assert first.book.last_update_mono == 101.0
        assert second.last_traded_ts == 101.0

    asyncio.run(go())


def test_trade_audit_failure_still_attempts_one_reduce_only_residual_hedge():
    async def go():
        eng = make_engine()
        buy = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            buy.set_book(99.9, 100.0)
            buy.book.last_update_mono = buy.last_traded_ts + 1.0
            raise OSError("audit disk full")

        eng._log_csv = fail_audit

        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert buy.send_calls == 2
        assert buy.position == pytest.approx(0.0)
        assert eng._recovery_required is True
        assert isinstance(eng._primary_error, OSError)
        assert "audit disk full" in str(eng._primary_error)

    asyncio.run(go())


def test_trade_audit_repair_grant_waits_until_an_order_is_submitted(
        monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine_module.time, "monotonic", lambda: clock[0])

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        buy = PositionVenue(
            "hedge", "RH",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            chain_position=0.5)
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0),
            chain_position=0.0)
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert buy.send_calls == 1
        assert eng._audit_repair_available is True
        assert eng._auto_repair_disabled is False
        assert eng._shutdown_reconcile_required is True

        clock[0] = 101.0
        buy.set_book(99.9, 100.0)
        buy.book.last_update_mono = buy.last_traded_ts + 1.0
        assert buy.book.is_fresh(eng.cfg.staleness_sec)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is True
        assert buy.send_calls == 2
        assert eng._audit_repair_available is False
        assert eng._auto_repair_disabled is True

    asyncio.run(go())


def test_audit_failure_with_unknown_order_waits_for_terminal_then_repairs():
    async def go():
        eng = make_engine()
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="42"))
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        task = asyncio.create_task(
            eng._execute_locked(buy, sell, execution_plan()),
            name="execute-audit-failure")
        eng._exec_tasks.add(task)
        task.add_done_callback(eng._execution_done)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert eng._shutdown_reconcile_required is True
        assert eng._auto_repair_disabled is False
        assert buy.send_calls == 1

        buy.terminal_results["42"] = OrderResult(
            status="filled", filled_base=0.5, avg_px=100.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is True
        assert buy.position == pytest.approx(0.5)
        assert sell.position == pytest.approx(-0.5)
        assert isinstance(eng._primary_error, OSError)

    asyncio.run(go())


def test_audit_failure_consumes_single_repair_before_unknown_resolves():
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self, key, label, results):
            super().__init__(key, label, results[0])
            self.results = list(results)

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            return self.results.pop(0)

    async def go():
        eng = make_engine()
        buy = SequencedConfirmingVenue("hedge", "RH", [
            OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
            OrderResult.unknown("timeout", order_ref="repair-1"),
            OrderResult(status="canceled", filled_base=0.0),
        ])
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="canceled", filled_base=0.0))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            buy.set_book(99.9, 100.0)
            buy.book.last_update_mono = buy.last_traded_ts + 1.0
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert buy.send_calls == 2
        buy.terminal_results["repair-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert buy.send_calls == 2
        assert eng._auto_repair_disabled is True
        assert eng._recovery_required is True

    asyncio.run(go())


def test_audit_failure_does_not_repair_unreferenced_unknown():
    async def go():
        eng = make_engine()
        buy = ConfirmingVenue(
            "hedge", "RH", OrderResult.unknown("accepted-unknown"))
        sell = PositionVenue(
            "entropy", "ENTROPY",
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())

        assert buy.send_calls == 1
        assert sell.send_calls == 1
        assert eng._shutdown_reconcile_required is False
        assert eng._auto_repair_disabled is True
        assert isinstance(eng._primary_error, RuntimeError)
        assert "no order reference" in str(eng._primary_error)

    asyncio.run(go())


def test_delayed_audit_repair_is_consumed_before_unknown_repair_settles():
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self, key, label, results):
            super().__init__(key, label, results[0])
            self.results = list(results)

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            return self.results.pop(0)

    async def go():
        eng = make_engine()
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="primary-1"))
        sell = SequencedConfirmingVenue("entropy", "ENTROPY", [
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2),
            OrderResult.unknown("timeout", order_ref="repair-1"),
            OrderResult(status="canceled", filled_base=0.0),
        ])
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            sell.set_book(100.2, 100.3)
            sell.book.last_update_mono = sell.last_traded_ts + 1.0
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())

        buy.terminal_results["primary-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        recovered = await eng._recover_positions_locked(strict=True)
        assert recovered is False
        assert sell.send_calls == 2

        sell.terminal_results["repair-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        recovered = await eng._recover_positions_locked(strict=True)

        assert recovered is False
        assert sell.send_calls == 2
        assert eng._auto_repair_disabled is True

    asyncio.run(go())


def test_shutdown_drain_confirms_unknown_audit_repair_after_auto_repair_disabled():
    class SequencedConfirmingVenue(ConfirmingVenue):
        def __init__(self, key, label, results):
            super().__init__(key, label, results[0])
            self.results = list(results)

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            return self.results.pop(0)

    async def go():
        eng = make_engine()
        buy = ConfirmingVenue(
            "hedge", "RH",
            OrderResult.unknown("timeout", order_ref="primary-1"))
        sell = SequencedConfirmingVenue("entropy", "ENTROPY", [
            OrderResult(status="filled", filled_base=0.5, avg_px=100.2),
            OrderResult.unknown("timeout", order_ref="repair-1"),
        ])
        buy.set_book(99.9, 100.0)
        sell.set_book(100.2, 100.3)
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()

        def fail_audit(*_args, **_kwargs):
            sell.set_book(100.2, 100.3)
            sell.book.last_update_mono = sell.last_traded_ts + 1.0
            raise OSError("audit disk full")

        eng._log_csv = fail_audit
        with pytest.raises(OSError, match="audit disk full"):
            await eng._execute_locked(buy, sell, execution_plan())
        buy.terminal_results["primary-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        assert await eng._recover_positions_locked(strict=True) is False
        assert [item.order_ref for item in eng._pending_order_confirmations] == [
            "repair-1"]
        assert eng._auto_repair_disabled is True
        assert eng._shutdown_reconcile_required is True

        async def yielding_feed_check():
            await asyncio.sleep(0)
            return False

        eng._abort_failed_recovery_feed = yielding_feed_check
        drain = asyncio.create_task(eng._drain_executions(poll_sec=0.5))
        sell.terminal_results["repair-1"] = OrderResult(
            status="canceled", filled_base=0.0)
        eng._live_progress_update("order", sell.key)
        await asyncio.wait_for(drain, timeout=0.1)

        assert eng._pending_order_confirmations == []
        assert eng._shutdown_reconcile_required is False

    asyncio.run(go())


def test_runtime_cancellation_of_execution_enters_unknown_recovery():
    class BlockingVenue(ExecutingVenue):
        def __init__(self, key, label):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_taker(self, **_kwargs):
            self.started.set()
            await self.release.wait()
            return OrderResult.unknown(
                "timeout", order_ref=f"{self.key}-cancelled")

    async def go():
        eng = make_engine()
        buy = BlockingVenue("hedge", "RH")
        sell = BlockingVenue("entropy", "ENTROPY")
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        task = asyncio.create_task(
            eng._execute_locked(buy, sell, execution_plan()),
            name="execute-test")
        eng._exec_tasks.add(task)
        task.add_done_callback(eng._execution_done)
        await asyncio.gather(buy.started.wait(), sell.started.wait())

        task.cancel()
        buy.release.set()
        sell.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert eng.stop.is_set()
        assert eng._recovery_required is True
        assert eng._shutdown_reconcile_required is True
        assert isinstance(eng._primary_error, RuntimeError)

    asyncio.run(go())


def test_execution_cancellation_waits_for_referenced_leg_outcomes():
    class BlockingReferencedVenue(ConfirmingVenue):
        def __init__(self, key, label, order_ref):
            super().__init__(
                key, label,
                OrderResult.unknown("timeout", order_ref=order_ref))
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send_taker(self, **kwargs):
            self.started.set()
            await self.release.wait()
            return await super().send_taker(**kwargs)

    async def go():
        eng = make_engine()
        buy = BlockingReferencedVenue("hedge", "RH", "buy-42")
        sell = BlockingReferencedVenue("entropy", "ENTROPY", "sell-43")
        eng.entropy, eng.hedge = sell, buy
        eng.venues = {"entropy": sell, "hedge": buy}
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        task = asyncio.create_task(
            eng._execute_locked(buy, sell, execution_plan()),
            name="execute-referenced-cancel")
        eng._exec_tasks.add(task)
        task.add_done_callback(eng._execution_done)
        await asyncio.gather(buy.started.wait(), sell.started.wait())

        task.cancel()
        buy.release.set()
        sell.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert task.cancelled() is True
        assert {item.order_ref for item in eng._pending_order_confirmations} == {
            "buy-42", "sell-43"}
        assert eng._pending_snapshot_venues == set()
        assert buy.fetch_calls == 0
        assert sell.fetch_calls == 0
        assert eng._shutdown_reconcile_required is True

    asyncio.run(go())


def test_matched_cancelled_partial_fills_are_not_successful():
    eng = make_engine()
    buy = ExecutingVenue(
        "hedge", "RH",
        OrderResult(status="canceled", filled_base=0.25, avg_px=100.0))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="canceled", filled_base=0.25, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["ok"] is False


def test_fatal_hedge_result_does_not_repeat_repair_during_shutdown():
    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = PositionVenue(
            "entropy", "ENTROPY", {"status": "filled"},
            chain_position=0.5)
        eng.hedge = PositionVenue(
            "hedge", "RH", OrderResult(status="filled", filled_base=0.0),
            chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._enter_recovery("test unknown outcome")

        await asyncio.wait_for(
            eng._drain_executions(poll_sec=0.001), timeout=0.1)

        assert eng.entropy.send_calls == 1
        assert eng._shutdown_reconcile_required is False
        assert eng._recovery_required is True
        assert isinstance(eng._primary_error, TypeError)

    asyncio.run(go())


def test_cancelled_recovery_hedge_is_reconciled_before_drain_returns():
    class CancellableHedgeVenue(PositionVenue):
        def __init__(self, key, label, chain_position):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.0),
                chain_position=chain_position)
            self.fetch_calls = 0
            self.send_started = asyncio.Event()

        async def fetch_position(self):
            self.fetch_calls += 1
            return self.chain_position

        async def send_taker(self, **_kwargs):
            self.send_calls += 1
            self.send_started.set()
            await asyncio.Event().wait()

    async def go():
        eng = make_engine()
        eng.RECONCILE_GRACE_SEC = 0.0
        eng.entropy = CancellableHedgeVenue(
            "entropy", "ENTROPY", chain_position=0.5)
        eng.hedge = CancellableHedgeVenue(
            "hedge", "RH", chain_position=0.0)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        eng._enter_recovery("test unknown outcome")
        drain = asyncio.create_task(
            eng._drain_executions(poll_sec=0.001), name="drain-test")

        await asyncio.wait_for(
            eng.entropy.send_started.wait(), timeout=0.1)
        hedge_task = next(
            task for task in eng._exec_tasks
            if task.get_name() == "hedge-residual")
        hedge_task.cancel()
        await asyncio.wait_for(drain, timeout=0.1)

        assert eng.entropy.send_calls == 1
        assert eng.entropy.fetch_calls >= 2
        assert eng._shutdown_reconcile_required is False
        assert eng._recovery_required is True
        assert isinstance(eng._primary_error, RuntimeError)

    asyncio.run(go())


def test_maybe_hedge_preserves_caller_cancellation():
    class ReleasableVenue(PositionVenue):
        def __init__(self, key, label, chain_position):
            super().__init__(
                key, label,
                OrderResult(status="filled", filled_base=0.5, avg_px=100.0),
                chain_position=chain_position)
            self.send_started = asyncio.Event()
            self.release_send = asyncio.Event()

        async def send_taker(self, **kwargs):
            self.send_calls += 1
            self.send_started.set()
            await self.release_send.wait()
            return await ExecutingVenue.send_taker(self, **kwargs)

    async def go():
        eng = make_engine()
        eng.entropy = ReleasableVenue(
            "entropy", "ENTROPY", chain_position=0.5)
        eng.hedge = ReleasableVenue(
            "hedge", "RH", chain_position=0.0)
        eng.entropy.position = 0.5
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        eng.entropy.set_book(99.9, 100.0)
        eng.hedge.set_book(99.9, 100.0)
        caller = asyncio.create_task(eng._maybe_hedge())

        await asyncio.wait_for(
            eng.entropy.send_started.wait(), timeout=0.1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        eng.entropy.release_send.set()
        await asyncio.gather(*tuple(eng._exec_tasks))

    asyncio.run(go())


def test_signal_recorder_starts_only_in_record_only_mode():
    async def go():
        record_engine = make_engine(record_only=True)
        record_dir = tempfile.mkdtemp()
        record_engine.cfg.recorder_csv = os.path.join(
            record_dir, "minutes.csv")
        record_engine.cfg.recorder_signal_csv = os.path.join(
            record_dir, "signals.csv")
        record_engine.cfg.entropy.symbol = "ANTH"
        record_engine.cfg.hedge.symbol = "ANTHROPIC"
        record_engine.cfg.recorder_signal_rotate_daily = False
        record_tasks = []

        record_engine._start_recorders(record_tasks)

        assert record_engine.signal_recorder is not None
        assert record_engine.recorder.entropy_symbol == "ANTH"
        assert record_engine.recorder.hedge_symbol == "ANTHROPIC"
        assert record_engine.recorder.entropy_dex == "io"
        assert record_engine.recorder.hedge_venue == "lighter-rh"
        assert (record_engine.recorder.entropy_reference
                is record_engine.entropy.reference)
        assert (record_engine.recorder.hedge_reference
                is record_engine.hedge.reference)
        assert record_engine.signal_recorder.entropy_symbol == "ANTH"
        assert record_engine.signal_recorder.hedge_symbol == "ANTHROPIC"
        assert record_engine.signal_recorder.signal_rotate_daily is False
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


@pytest.mark.parametrize(
    "outcome,match",
    [(RuntimeError("book failed"), "book failed"),
     (None, "book-entropy.*exited unexpectedly")],
)
def test_background_task_failure_or_early_exit_stops_engine(outcome, match):
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": BackgroundOutcomeVenue(
                "entropy", "ENTROPY", outcome),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match=match):
                await asyncio.wait_for(eng._run_inner(), timeout=0.2)
        finally:
            eng.request_stop()
            engine_module.create_venue = original
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_startup_residual_pauses_strategy_until_reduce_only_recovery():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_enabled = False
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.trades_csv = os.path.join(directory, "trades.csv")
        cfg.entropy.hl_creds = SimpleNamespace(complete=True)
        cfg.hedge.lighter_creds = SimpleNamespace(complete=True)
        venues = {
            "entropy": LiveLifecycleVenue(
                "entropy", "ENTROPY", chain_position=0.5),
            "hedge": LiveLifecycleVenue(
                "hedge", "RH", chain_position=0.0),
        }
        eng = Engine(cfg, record_only=False)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        task = asyncio.create_task(eng._run_inner())
        try:
            await asyncio.wait_for(
                _wait_until(lambda: eng._recovery_required), timeout=0.2)

            assert eng._scan(time.monotonic()) is None
            assert sum(v.position for v in venues.values()) == \
                pytest.approx(0.5)
        finally:
            eng.request_stop()
            await asyncio.gather(task, return_exceptions=True)
            engine_module.create_venue = original

    async def _wait_until(predicate):
        while not predicate():
            await asyncio.sleep(0)

    asyncio.run(go())


def test_market_load_failure_closes_every_created_venue():
    async def go():
        cfg = make_cfg()
        venues = {
            "entropy": LoadFailVenue("entropy", "ENTROPY"),
            "hedge": LoadFailVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match="market load failed"):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_partial_task_start_failure_cancels_started_tasks_and_closes_venues():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": StartFailVenue("entropy", "ENTROPY"),
            "hedge": StartFailVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match="task startup failed"):
                await eng._run_inner()
        finally:
            eng.request_stop()
            engine_module.create_venue = original
            leftovers = [task for task in (
                venues["entropy"].started_task,
                eng._recorder_task,
                eng._signal_task,
            ) if task is not None and not task.done()]
            for task in leftovers:
                task.cancel()
            if leftovers:
                await asyncio.gather(*leftovers, return_exceptions=True)

        assert venues["entropy"].started_task.done()
        assert venues["entropy"].started_task.cancelled()
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_external_cancellation_closes_venues_and_remains_cancelled():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": LifecycleVenue("entropy", "ENTROPY"),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        task = asyncio.create_task(eng._run_inner())
        try:
            while not eng.markets_ready:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            engine_module.create_venue = original
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_background_error_remains_primary_when_close_also_fails():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": BackgroundCloseFailVenue(
                "entropy", "ENTROPY", RuntimeError("book failed")),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match="book failed"):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_cancellation_during_cleanup_still_closes_venues():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": CleanupBlockingVenue("entropy", "ENTROPY"),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        task = asyncio.create_task(eng._run_inner())
        try:
            while not eng.markets_ready:
                await asyncio.sleep(0)
            eng.request_stop()
            await asyncio.wait_for(
                venues["entropy"].cancellation_started.wait(), timeout=0.2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            venues["entropy"].release_cancellation.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            venues["entropy"].release_cancellation.set()
            engine_module.create_venue = original
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_background_error_precedes_cancellation_during_cleanup():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": CleanupBlockingVenue(
                "entropy", "ENTROPY", fail=True),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        task = asyncio.create_task(eng._run_inner())
        try:
            await asyncio.wait_for(
                venues["entropy"].cancellation_started.wait(), timeout=0.2)
            task.cancel()
            venues["entropy"].release_cancellation.set()
            with pytest.raises(RuntimeError, match="background failed"):
                await task
        finally:
            venues["entropy"].release_cancellation.set()
            engine_module.create_venue = original
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert all(venue.closed for venue in venues.values())

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
            task = asyncio.create_task(eng._run_inner())
            await asyncio.wait_for(
                venues["entropy"].finished.wait(), timeout=0.2)
            eng.request_stop()
            await task
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


def test_signal_callback_error_remains_primary_when_close_also_fails():
    class CloseFailSignalRecorder(engine_module.SignalRecorder):
        def close(self, now=None):
            super().close(now)
            raise OSError("secondary close failure")

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
        original_signal_recorder = engine_module.SignalRecorder
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        engine_module.SignalRecorder = CloseFailSignalRecorder
        try:
            with pytest.raises(ZeroDivisionError):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original_create_venue
            engine_module.SignalRecorder = original_signal_recorder

        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_minute_recorder_error_propagates_from_record_only_engine():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        blocker = os.path.join(directory, "not-a-directory")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("block")
        cfg.recorder_csv = os.path.join(blocker, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": LifecycleVenue("entropy", "ENTROPY"),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original_create_venue = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(OSError):
                await asyncio.wait_for(eng._run_inner(), timeout=0.2)
        finally:
            engine_module.create_venue = original_create_venue

        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())


def test_signal_error_survives_venue_close_error_and_all_venues_close():
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": CloseFailVenue("entropy", "ENTROPY"),
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

    asyncio.run(go())


def test_trades_rotation_preserves_existing_archive():
    eng = make_engine()
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "trades.csv")
    eng.cfg.trades_csv = path
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("previous,header\n1,2\n")
    with open(path + ".old", "w", encoding="utf-8", newline="") as fh:
        fh.write("older archive\n")

    eng._log_csv(
        "sell_entropy", eng.hedge, eng.entropy, execution_plan(), True,
        0.5, 0.5, "filled", "filled", 0.1, 0.0)

    with open(path + ".old", encoding="utf-8") as fh:
        assert fh.read() == "older archive\n"
    with open(path + ".old.1", encoding="utf-8") as fh:
        assert fh.read() == "previous,header\n1,2\n"


def test_trades_csv_rotates_incomplete_tail_before_append():
    eng = make_engine()
    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "trades.csv")
    eng.cfg.trades_csv = path
    original = ",".join(engine_module.CSV_HEADER) + "\npartial,row"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(original)

    eng._log_csv(
        "sell_entropy", eng.hedge, eng.entropy, execution_plan(), True,
        0.5, 0.5, "filled", "filled", 0.1, 0.0)

    with open(path + ".old", encoding="utf-8") as fh:
        assert fh.read() == original
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 2
    assert all(len(row) == len(engine_module.CSV_HEADER) for row in rows)


def test_trades_csv_rotates_tail_with_unclosed_quote(tmp_path):
    eng = make_engine()
    path = tmp_path / "trades.csv"
    eng.cfg.trades_csv = str(path)
    row = ["0"] * len(engine_module.CSV_HEADER)
    original = (",".join(engine_module.CSV_HEADER) + "\n"
                + ",".join(row[:-1]) + ',"0\n')
    path.write_text(original, encoding="utf-8")

    eng._log_csv(
        "sell_entropy", eng.hedge, eng.entropy, execution_plan(), True,
        0.5, 0.5, "filled", "filled", 0.1, 0.0)

    assert (tmp_path / "trades.csv.old").read_text(
        encoding="utf-8") == original


def test_trades_csv_write_failure_propagates_without_rotating_directory(
        tmp_path):
    eng = make_engine()
    path = tmp_path / "trades.csv"
    path.mkdir()
    eng.cfg.trades_csv = str(path)

    with pytest.raises(OSError):
        eng._log_csv(
            "sell_entropy", eng.hedge, eng.entropy, execution_plan(), True,
            0.5, 0.5, "filled", "filled", 0.1, 0.0)

    assert path.is_dir()
    assert not (tmp_path / "trades.csv.old").exists()


def test_cleanup_closes_session_without_replacing_primary_error():
    class BrokenSession:
        def __init__(self):
            self.close_attempted = False

        async def close(self):
            self.close_attempted = True
            raise OSError("session close failed")

    async def go():
        eng = Engine(make_cfg(), record_only=True)
        primary = ValueError("primary failure")
        session = BrokenSession()
        eng._primary_error = primary
        eng.session = session

        await eng._cleanup([])

        assert session.close_attempted is True
        assert eng._primary_error is primary

    asyncio.run(go())


def test_cleanup_times_out_stuck_close_and_continues_other_resources():
    class StuckVenue(LifecycleVenue):
        def __init__(self, key, label):
            super().__init__(key, label)
            self.close_cancelled = False
            self.release_close = asyncio.Event()

        async def close(self):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                await self.release_close.wait()

    class Session:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def go():
        eng = Engine(make_cfg(), record_only=True)
        eng.RESOURCE_CLOSE_TIMEOUT_SEC = 0.01
        stuck = StuckVenue("entropy", "ENTROPY")
        healthy = LifecycleVenue("hedge", "RH")
        session = Session()
        eng.venues = {"entropy": stuck, "hedge": healthy}
        eng.session = session

        cleanup = asyncio.create_task(eng._cleanup([]))
        try:
            done, _ = await asyncio.wait({cleanup}, timeout=0.1)
            assert cleanup in done
            await cleanup

            assert stuck.close_cancelled is True
            assert healthy.closed is True
            assert session.closed is True
            assert isinstance(eng._primary_error, TimeoutError)
            assert "ENTROPY" in str(eng._primary_error)
        finally:
            stuck.release_close.set()
            if not cleanup.done():
                cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            await asyncio.sleep(0)

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
