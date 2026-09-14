"""Two-venue arbitrage engine: Entropy vs one hedge venue.

The signal is a fixed band around a configured midline (config.yaml):

    SELL entropy / BUY hedge  when executable premium >= midline + upper (+fees)
    BUY entropy / SELL hedge  when executable premium <= midline - lower (+fees)

Around the signal: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. ``--record-only`` also runs
the dynamic residual strategy as an isolated shadow campaign when selected;
it never sends orders. Both books are recorded to 1-minute CSV bars.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

import aiohttp

from .book import ArbPlan, floor_step, plan_arb
from .campaign import (
    CampaignRecoveryError,
    CampaignStateError,
    CampaignStore,
    PositionCampaign,
    reconcile_campaign,
)
from .config import Config
from .models import OrderResult
from .reference import (
    InvalidReference,
    ReferenceAlertState,
    calculate_reference_metrics,
)
from .recorder import (
    MinuteRecorder,
    SignalRecorder,
    csv_header_matches,
    csv_tail_complete,
    next_archive_path,
)
from .slippage import SlippageModel
from .strategy import (
    DynamicResidualStrategy,
    MarketIdentity,
    MarketView,
    ResidualModel,
    StrategyDecision,
    warm_start_residual_model,
)
from .strategy_recorder import StrategyEvent, StrategyEventRecorder
from .venues.base import VenueAdapter
from .venues.registry import VenueRuntime, create_venue

log = logging.getLogger("engine")

CSV_HEADER = ["ts", "direction", "buy_venue", "sell_venue", "qty",
              "buy_limit", "sell_limit", "buy_notional", "sell_notional",
              "exp_edge_usd", "gross_edge_usd", "marginal_premium_bps",
              "midline_bps", "inv_add_bps", "ok", "buy_fill", "sell_fill",
              "buy_status", "sell_status", "fill_edge_usd"]
BALANCE_POLL_SEC = 30.0


class _TradeAuditFailure(RuntimeError):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


class _OrderRecoveryInvariantError(RuntimeError):
    pass


@dataclass
class _PendingOrderConfirmation:
    venue: VenueAdapter
    order_ref: str
    is_buy: bool
    applied_fill: float
    is_residual_hedge: bool = False


class Engine:
    RESOURCE_CLOSE_TIMEOUT_SEC = 5.0

    def __init__(self, cfg: Config, record_only: bool = False) -> None:
        self.cfg = cfg
        self.record_only = record_only
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy: Optional[VenueAdapter] = None
        self.hedge: Optional[VenueAdapter] = None
        self.venues: Dict[str, VenueAdapter] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.signal_recorder: Optional[SignalRecorder] = None
        self.strategy_events: Optional[StrategyEventRecorder] = None
        self.residual_model: Optional[ResidualModel] = None
        self.dynamic_strategy: Optional[DynamicResidualStrategy] = None
        self.campaign_store: Optional[CampaignStore] = None
        self.campaign: Optional[PositionCampaign] = None
        self.model_warm_start = None
        self._campaign_recovery_blocked = False
        self._recorder_task: Optional[asyncio.Task] = None
        self._signal_task: Optional[asyncio.Task] = None
        self._primary_error: Optional[BaseException] = None
        self._task_failures: Dict[asyncio.Task, BaseException] = {}
        self._intentional_task_cancellations: set[asyncio.Task] = set()
        self._audit_repair_errors: set[BaseException] = set()
        self._audit_repair_available = False
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._feed_stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._strategy_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self._recovery_required = False
        self._shutdown_reconcile_required = False
        self._auto_repair_disabled = False
        self._recovery_generation = 0
        self._recovery_lock = asyncio.Lock()
        self._unknown_resolved_evt = asyncio.Event()
        self._background_failure_evt = asyncio.Event()
        self._order_progress_evts: Dict[str, asyncio.Event] = {}
        self._book_progress_evts: Dict[str, asyncio.Event] = {}
        self._residual_book_after: Dict[str, float] = {}
        self._residual_waiting_book_venues: set[str] = set()
        self._post_order_recovery_active = False
        self._pending_order_confirmations: List[
            _PendingOrderConfirmation] = []
        self._manual_order_confirmations: List[
            _PendingOrderConfirmation] = []
        self._pending_snapshot_venues: set[str] = set()
        self._unreferenced_unknown = False
        self.halted = False
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.last_trade_mono = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        self._reconcile_due: Optional[float] = None
        self._dynamic_last_action_mono = 0.0
        self._dynamic_pending_minute: Optional[int] = None
        self._dynamic_pending_residual: Optional[float] = None
        self._dynamic_pending_valid = False
        self._last_model_event_minute: Optional[int] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        self._reference_alerts = ReferenceAlertState(
            alert_bps=cfg.reference_residual_alert_bps,
            persist_sec=cfg.reference_residual_persist_sec)

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.monotonic()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.monotonic() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = (
            time.monotonic() + self.cfg.rate_limit_pause_sec)
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.monotonic())

    def _register_unresolved_order(
            self, venue: VenueAdapter, result: OrderResult, *,
            is_buy: bool, applied_fill: float,
            is_residual_hedge: bool = False) -> None:
        if result.order_ref is None:
            self._unreferenced_unknown = True
            self._auto_repair_disabled = True
            error = RuntimeError(
                f"[{venue.name}] unresolved order has no order reference; "
                "manual position recovery is required")
            self._remember_error("unreferenced order outcome", error)
            self.request_stop()
            return
        self._pending_order_confirmations.append(
            _PendingOrderConfirmation(
                venue=venue,
                order_ref=result.order_ref,
                is_buy=is_buy,
                applied_fill=applied_fill,
                is_residual_hedge=is_residual_hedge,
            ))
        self._post_order_recovery_active = True

    def _clear_armed(self) -> None:
        for direction in self._armed:
            self._armed[direction] = None

    def _pause_for_recovery(self, reason: str) -> None:
        self._clear_armed()
        if not self._recovery_required:
            log.critical(
                "trading PAUSED for position recovery: %s", reason)
            self._reconcile_evt.set()
        self._recovery_required = True

    def _enter_recovery(self, reason: str) -> None:
        self._pause_for_recovery(reason)
        self._recovery_generation += 1
        self._unknown_resolved_evt.clear()
        self._shutdown_reconcile_required = (
            not self._unreferenced_unknown
            or bool(self._pending_order_confirmations)
            or bool(self._pending_snapshot_venues))
        self._reconcile_evt.set()

    def request_stop(self) -> None:
        self.stop.set()
        self._update_evt.set()
        self._strategy_evt.set()
        self._reconcile_evt.set()

    def _remember_error(self, label: str, error: BaseException) -> None:
        if error is self._primary_error:
            return
        if self._primary_error is None:
            self._primary_error = error
            log.error("%s failed", label,
                      exc_info=(type(error), error, error.__traceback__))
        else:
            log.error("%s also failed; preserving the primary error", label,
                      exc_info=(type(error), error, error.__traceback__))

    def _task_done(self, task: asyncio.Task) -> None:
        if task in self._task_failures:
            return
        if task.cancelled():
            if task in self._intentional_task_cancellations:
                self._intentional_task_cancellations.discard(task)
                return
            error = RuntimeError(
                f"background task {task.get_name()} was cancelled unexpectedly")
        else:
            error = task.exception()
        if error is None:
            name = task.get_name()
            is_feed = any(
                name in (f"book-{venue_key}", f"acct-{venue_key}")
                for venue_key in self.venues)
            if self.stop.is_set() and (not is_feed or self._feed_stop.is_set()):
                return
            error = RuntimeError(
                f"background task {task.get_name()} exited unexpectedly")
        self._task_failures[task] = error
        self._remember_error(f"background task {task.get_name()}", error)
        self._background_failure_evt.set()
        self.request_stop()

    def _execution_done(self, task: asyncio.Task) -> None:
        self._exec_tasks.discard(task)
        if task.cancelled():
            self._auto_repair_disabled = True
            if not self._shutdown_reconcile_required:
                self._enter_recovery(
                    f"execution task {task.get_name()} was cancelled")
            error = RuntimeError(
                f"execution task {task.get_name()} was cancelled unexpectedly")
        else:
            error = task.exception()
        if error is not None:
            repair_allowed = error in self._audit_repair_errors
            self._audit_repair_errors.discard(error)
            if not repair_allowed:
                self._auto_repair_disabled = True
            self._remember_error(f"execution task {task.get_name()}", error)
            self.request_stop()

    def _track_task(self, tasks: List[asyncio.Task],
                    task: asyncio.Task) -> None:
        tasks.append(task)
        task.add_done_callback(self._task_done)

    def _record_only_book_update(self, *_args) -> None:
        self._update_evt.set()
        self._strategy_evt.set()
        if self.signal_recorder is not None:
            try:
                self.signal_recorder.observe(flush=False)
            except Exception as exc:
                self._remember_error(
                    "signal recorder book callback", exc)
                self.request_stop()

    @staticmethod
    def _venue_progress_evt(
            events: Dict[str, asyncio.Event], venue_key: str) -> asyncio.Event:
        event = events.get(venue_key)
        if event is None:
            event = events[venue_key] = asyncio.Event()
        return event

    def _live_progress_update(
            self, source: str = "book",
            venue_key: Optional[str] = None) -> None:
        self._update_evt.set()
        self._strategy_evt.set()
        if source == "order":
            keys = (venue_key,) if venue_key is not None else tuple(self.venues)
            for key in keys:
                self._venue_progress_evt(self._order_progress_evts, key).set()
            if (self._recovery_required
                    and any(confirmation.venue.key in keys
                            for confirmation
                            in self._pending_order_confirmations)):
                self._reconcile_evt.set()
            return
        if source != "book":
            raise ValueError(f"unknown recovery progress source {source!r}")
        keys = (venue_key,) if venue_key is not None else tuple(self.venues)
        for key in keys:
            self._venue_progress_evt(self._book_progress_evts, key).set()
        if (self._recovery_required
                and not self._pending_order_confirmations
                and any(key in self._residual_waiting_book_venues
                        for key in keys)):
            self._reconcile_evt.set()

    def _start_recorders(self, tasks: List[asyncio.Task]) -> None:
        cfg = self.cfg
        if cfg.recorder_enabled or self.record_only:
            self.recorder = MinuteRecorder(
                cfg.recorder_csv, self.entropy.book, self.hedge.book,
                cfg.staleness_sec,
                entropy_reference=self.entropy.reference,
                hedge_reference=self.hedge.reference,
                entropy_symbol=cfg.entropy.symbol,
                entropy_dex=cfg.entropy.hl_dex,
                hedge_symbol=cfg.hedge.symbol,
                hedge_venue=cfg.hedge_venue)
            self._recorder_task = asyncio.create_task(
                self.recorder.run(
                    self.stop, fail_fast=self.record_only),
                name="recorder")
            self._track_task(tasks, self._recorder_task)
        if self.record_only:
            self.signal_recorder = SignalRecorder(
                cfg.recorder_signal_csv,
                self.entropy,
                self.hedge,
                midline_bps=cfg.midline_bps,
                upper_bps=cfg.upper_bps,
                lower_bps=cfg.lower_bps,
                take_fraction=cfg.take_fraction,
                max_order_notional=cfg.max_order_notional,
                min_base=self._min_base,
                min_notional=self._min_notional,
                size_step=self._step,
                leg_slippage_bps=cfg.leg_slippage_bps,
                staleness_sec=cfg.staleness_sec,
                entropy_symbol=cfg.entropy.symbol,
                entropy_dex=cfg.entropy.hl_dex,
                hedge_symbol=cfg.hedge.symbol,
                hedge_venue=cfg.hedge_venue,
                signal_rotate_daily=cfg.recorder_signal_rotate_daily,
            )
            self._signal_task = asyncio.create_task(
                self.signal_recorder.run(self.stop, self._update_evt),
                name="signal-recorder")
            self._track_task(tasks, self._signal_task)

    def _market_identity(self) -> MarketIdentity:
        return MarketIdentity(
            entropy_symbol=self.cfg.entropy.symbol,
            entropy_dex=self.cfg.entropy.hl_dex,
            hedge_symbol=self.cfg.hedge.symbol,
            hedge_venue=self.cfg.hedge_venue,
        )

    def _initialize_dynamic_strategy(
            self, *, now_wall: Optional[float] = None) -> None:
        """Create the residual model, shadow/live state and event journal."""
        if self.cfg.strategy_mode != "residual_dynamic":
            return
        if self.dynamic_strategy is not None:
            raise RuntimeError("dynamic strategy is already initialized")
        cfg = self.cfg
        wall = time.time() if now_wall is None else now_wall
        self.residual_model = ResidualModel(
            window_minutes=cfg.strategy_window_minutes,
            min_samples=cfg.strategy_min_samples,
            lower_quantile=cfg.strategy_lower_quantile,
            upper_quantile=cfg.strategy_upper_quantile,
            regime_window_minutes=cfg.strategy_regime_window_minutes,
            recovery_minutes=cfg.strategy_regime_recovery_minutes,
        )
        self.model_warm_start = warm_start_residual_model(
            self.residual_model,
            path=cfg.recorder_csv,
            identity=self._market_identity(),
            now_minute=int(wall // 60),
            max_age_sec=cfg.strategy_entry_reference_max_age_sec,
            max_skew_sec=cfg.strategy_entry_reference_max_skew_sec,
        )
        slippage = SlippageModel(
            bootstrap_bps=cfg.slippage_bootstrap_bps,
            min_bps=cfg.slippage_min_bps,
            safety_bps=cfg.slippage_safety_bps,
            hard_max_bps=cfg.slippage_hard_max_bps,
            min_live_samples=cfg.slippage_min_live_samples,
        )
        self.dynamic_strategy = DynamicResidualStrategy(
            slippage=slippage,
            reference_max_age_sec=(
                cfg.strategy_entry_reference_max_age_sec),
            reference_max_skew_sec=(
                cfg.strategy_entry_reference_max_skew_sec),
            exit_band_fraction=cfg.strategy_exit_band_fraction,
            min_exit_band_bps=cfg.strategy_min_exit_band_bps,
            min_expected_profit_bps=cfg.strategy_min_expected_profit_bps,
            soft_hold_sec=cfg.strategy_soft_hold_minutes * 60.0,
            hard_hold_sec=cfg.strategy_hard_hold_minutes * 60.0,
            hard_slippage_bps=cfg.slippage_hard_max_bps,
            max_edge_fraction=cfg.slippage_max_edge_fraction,
        )
        self.campaign_store = CampaignStore(
            cfg.strategy_state_file, shadow=self.record_only)
        if self.record_only:
            self._load_dynamic_campaign()
        self.strategy_events = StrategyEventRecorder(
            cfg.strategy_event_csv)

        log.info(
            "dynamic residual model warm start: accepted=%d "
            "identity_rejected=%d reference_rejected=%d value_rejected=%d "
            "time_rejected=%d",
            self.model_warm_start.accepted,
            self.model_warm_start.rejected_identity,
            self.model_warm_start.rejected_reference,
            self.model_warm_start.rejected_value,
            self.model_warm_start.rejected_time,
        )

    def _load_dynamic_campaign(self) -> None:
        self.campaign = self.campaign_store.load()
        if (self.campaign is not None
                and self.campaign.identity != self._market_identity()):
            raise CampaignStateError(
                "saved campaign market identity does not match config")
        expected_mode = "shadow" if self.record_only else "live"
        if (self.campaign is not None
                and self.campaign.mode != expected_mode):
            raise CampaignStateError(
                "saved campaign mode does not match engine mode")

    def _close_dynamic_strategy(self) -> None:
        if self.strategy_events is not None:
            self.strategy_events.close()

    def _dynamic_market_view(self, now_mono: float) -> MarketView:
        entropy_ref = self.entropy.reference.snapshot
        hedge_ref = self.hedge.reference.snapshot
        entropy_age_ms = self.entropy.reference.age_ms(now_mono=now_mono)
        hedge_age_ms = self.hedge.reference.age_ms(now_mono=now_mono)
        skew = None
        if entropy_ref.source and hedge_ref.source:
            skew = abs(
                entropy_ref.received_mono
                - hedge_ref.received_mono)
        account_ready = (
            self.record_only
            or (self.entropy.ready_to_trade()
                and self.hedge.ready_to_trade()))
        books_ready = (
            self.entropy.book.is_fresh(self.cfg.staleness_sec)
            and self.hedge.book.is_fresh(self.cfg.staleness_sec)
            and account_ready
            and not self._venue_down
        )
        return MarketView(
            entropy_book=self.entropy.book,
            hedge_book=self.hedge.book,
            entropy_oracle_px=entropy_ref.oracle_px,
            hedge_index_px=hedge_ref.index_px,
            entropy_reference_age_sec=(
                None if entropy_age_ms is None else entropy_age_ms / 1000.0),
            hedge_reference_age_sec=(
                None if hedge_age_ms is None else hedge_age_ms / 1000.0),
            reference_skew_sec=skew,
            books_ready=books_ready,
            entropy_fee_bps=self.entropy.fee_bps,
            hedge_fee_bps=self.hedge.fee_bps,
            take_fraction=self.cfg.take_fraction,
            entry_cap_notional=self.cfg.max_order_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
        )

    def _current_dynamic_residual(
            self, now_mono: float) -> tuple[Optional[float], bool]:
        market = self._dynamic_market_view(now_mono)
        gate = self.dynamic_strategy._book_gate(market)
        if gate is None:
            gate = self.dynamic_strategy._reference_gate(market)
        if gate is not None:
            return None, False
        entropy_mid = market.entropy_book.mid()
        hedge_mid = market.hedge_book.mid()
        basis = (
            market.entropy_oracle_px / market.hedge_index_px - 1.0) * 1e4
        residual = (entropy_mid / hedge_mid - 1.0) * 1e4 - basis
        return residual, True

    def _advance_dynamic_model(
            self, *, now_wall: float, now_mono: float) -> None:
        """Retain the latest sample and commit one close per elapsed minute."""
        minute = int(now_wall // 60)
        residual, valid = self._current_dynamic_residual(now_mono)
        pending = self._dynamic_pending_minute
        if pending is None:
            self._dynamic_pending_minute = minute
        elif minute > pending:
            self.residual_model.observe(
                minute=pending,
                residual_bps=self._dynamic_pending_residual,
                valid=self._dynamic_pending_valid,
            )
            for missing in range(pending + 1, minute):
                self.residual_model.observe(
                    minute=missing, residual_bps=None, valid=False)
            self._dynamic_pending_minute = minute
        elif minute < pending:
            return
        self._dynamic_pending_residual = residual
        self._dynamic_pending_valid = valid

    def _record_model_snapshot(self, *, now_wall: float) -> None:
        minute = int(now_wall // 60)
        if self._last_model_event_minute == minute:
            return
        self._last_model_event_minute = minute
        model = self.residual_model.snapshot(now_minute=minute)
        self.strategy_events.record(StrategyEvent(
            ts=now_wall,
            mode="shadow" if self.record_only else "live",
            event="model_snapshot",
            entropy_symbol=self.cfg.entropy.symbol,
            entropy_dex=self.cfg.entropy.hl_dex,
            hedge_symbol=self.cfg.hedge.symbol,
            hedge_venue=self.cfg.hedge_venue,
            campaign_id=("" if self.campaign is None
                         else self.campaign.campaign_id),
            direction=("" if self.campaign is None
                       else self.campaign.direction),
            model_version=str(model.version),
            model_samples=model.samples,
            model_status=model.status,
            model_median_bps=model.median_bps,
            model_lower_bps=model.lower_bps,
            model_upper_bps=model.upper_bps,
            model_iqr_bps=model.iqr_bps,
        ))

    def _record_dynamic_decision(
            self, decision: StrategyDecision, *, now_wall: float,
            campaign_id: str = "", event: str = "decision",
            entropy_fill_px: Optional[float] = None,
            hedge_fill_px: Optional[float] = None,
            realized_pnl_usd: Optional[float] = None,
            hold_seconds: Optional[float] = None) -> None:
        model = decision.model
        plan = decision.plan
        planned_notional = None
        projected_net_bps = None
        projected_net_usd = None
        qty = None
        if plan is not None:
            qty = plan.qty
            planned_notional = max(plan.buy_notional, plan.sell_notional)
            projected_net_bps = getattr(plan, "projected_net_bps", None)
            if projected_net_bps is not None:
                projected_net_usd = (
                    planned_notional * projected_net_bps / 1e4)
        entropy_age = self.entropy.reference.age_ms()
        hedge_age = self.hedge.reference.age_ms()
        entropy_ref = self.entropy.reference.snapshot
        hedge_ref = self.hedge.reference.snapshot
        reference_skew_ms = None
        if entropy_ref.source and hedge_ref.source:
            reference_skew_ms = abs(
                entropy_ref.received_mono
                - hedge_ref.received_mono) * 1000.0
        funding = None
        if (entropy_ref.funding_current_bps_per_hour is not None
                and hedge_ref.funding_current_bps_per_hour is not None):
            funding = (entropy_ref.funding_current_bps_per_hour
                       - hedge_ref.funding_current_bps_per_hour)
            if decision.direction == "buy_entropy":
                funding = -funding
        active = self.campaign
        self.strategy_events.record(StrategyEvent(
            ts=now_wall,
            mode="shadow" if self.record_only else "live",
            event=event,
            intent=decision.intent,
            reason=decision.reason,
            decision_id=uuid.uuid4().hex,
            campaign_id=(campaign_id or (
                "" if active is None else active.campaign_id)),
            entropy_symbol=self.cfg.entropy.symbol,
            entropy_dex=self.cfg.entropy.hl_dex,
            hedge_symbol=self.cfg.hedge.symbol,
            hedge_venue=self.cfg.hedge_venue,
            direction=decision.direction,
            campaign_status=("" if active is None else active.status_at(
                now_wall,
                soft_sec=self.cfg.strategy_soft_hold_minutes * 60.0,
                hard_sec=self.cfg.strategy_hard_hold_minutes * 60.0)),
            model_version=("" if model is None else str(model.version)),
            model_samples=(None if model is None else model.samples),
            model_status=("" if model is None else model.status),
            model_median_bps=(None if model is None else model.median_bps),
            model_lower_bps=(None if model is None else model.lower_bps),
            model_upper_bps=(None if model is None else model.upper_bps),
            model_iqr_bps=(None if model is None else model.iqr_bps),
            signed_residual_bps=decision.signed_residual_bps,
            reference_basis_bps=decision.reference_basis_bps,
            entry_boundary_bps=decision.entry_boundary_bps,
            exit_target_bps=decision.exit_target_bps,
            convergence_bps=decision.convergence_bps,
            round_trip_fee_bps=decision.round_trip_fee_bps,
            buy_slippage_budget_bps=decision.buy_slippage_budget_bps,
            sell_slippage_budget_bps=decision.sell_slippage_budget_bps,
            projected_net_bps=projected_net_bps,
            projected_net_usd=projected_net_usd,
            estimated_campaign_pnl_usd=(
                decision.estimated_campaign_pnl_usd),
            qty=qty,
            planned_notional_usd=planned_notional,
            entropy_reference_age_ms=entropy_age,
            hedge_reference_age_ms=hedge_age,
            reference_update_skew_ms=reference_skew_ms,
            net_funding_bps_per_hour=funding,
            entropy_fill_px=entropy_fill_px,
            hedge_fill_px=hedge_fill_px,
            hold_seconds=hold_seconds,
            realized_pnl_usd=realized_pnl_usd,
        ))

    @staticmethod
    def _shadow_fill_prices(
            decision: StrategyDecision,
            campaign_direction: Optional[str] = None) -> tuple[float, float]:
        direction = campaign_direction or decision.direction
        if decision.intent in {"OPEN", "ADD"}:
            if direction == "sell_entropy":
                return decision.plan.sell_limit, decision.plan.buy_limit
            return decision.plan.buy_limit, decision.plan.sell_limit
        if direction == "sell_entropy":
            return decision.plan.buy_limit, decision.plan.sell_limit
        return decision.plan.sell_limit, decision.plan.buy_limit

    @staticmethod
    def _close_fill_pnl(
            campaign: PositionCampaign, *, qty: float,
            entropy_px: float, hedge_px: float,
            fees_usd: float) -> float:
        if campaign.direction == "buy_entropy":
            gross_per_base = (
                entropy_px - campaign.entropy_avg_px
                + campaign.hedge_avg_px - hedge_px)
        else:
            gross_per_base = (
                campaign.entropy_avg_px - entropy_px
                + hedge_px - campaign.hedge_avg_px)
        return campaign.realized_pnl_usd + gross_per_base * qty - fees_usd

    def _apply_shadow_decision(
            self, decision: StrategyDecision, *, now_wall: float) -> None:
        if decision.intent not in {"OPEN", "ADD", "CLOSE", "FORCED_CLOSE"}:
            raise ValueError("only executable decisions can be applied")
        if not self.record_only:
            raise RuntimeError("shadow decisions require record-only mode")
        prior = self.campaign
        campaign_direction = (
            decision.direction if prior is None else prior.direction)
        entropy_px, hedge_px = self._shadow_fill_prices(
            decision, campaign_direction)
        qty = decision.plan.qty
        fees = qty * (
            entropy_px * self.entropy.fee_bps
            + hedge_px * self.hedge.fee_bps) / 1e4
        final_pnl = None
        hold_seconds = None
        if decision.intent == "OPEN":
            if prior is not None:
                raise RuntimeError("cannot open a second campaign")
            self.campaign = PositionCampaign(
                campaign_id=uuid.uuid4().hex,
                mode="shadow",
                identity=self._market_identity(),
                direction=decision.direction,
                opened_at=now_wall,
                qty=qty,
                entropy_avg_px=entropy_px,
                hedge_avg_px=hedge_px,
                frozen_model=decision.model,
                entry_boundary_bps=decision.entry_boundary_bps,
                exit_target_bps=decision.exit_target_bps,
                fees_usd=fees,
                realized_pnl_usd=-fees,
            )
        else:
            if prior is None:
                raise RuntimeError("campaign decision has no active campaign")
            if decision.intent in {"CLOSE", "FORCED_CLOSE"}:
                final_pnl = self._close_fill_pnl(
                    prior, qty=qty, entropy_px=entropy_px,
                    hedge_px=hedge_px, fees_usd=fees)
                hold_seconds = max(now_wall - prior.opened_at, 0.0)
            self.campaign = prior.apply_matched_fill(
                intent=decision.intent,
                direction=prior.direction,
                qty=qty,
                entropy_px=entropy_px,
                hedge_px=hedge_px,
                fees_usd=fees,
            )
        self.campaign_store.save(self.campaign)
        campaign_id = (
            prior.campaign_id if prior is not None
            else self.campaign.campaign_id)
        event = (
            "campaign_closed"
            if prior is not None and self.campaign is None
            else "campaign_changed")
        self._record_dynamic_decision(
            decision, now_wall=now_wall,
            campaign_id=campaign_id, event=event,
            entropy_fill_px=entropy_px,
            hedge_fill_px=hedge_px,
            realized_pnl_usd=final_pnl,
            hold_seconds=hold_seconds,
        )

    def _dynamic_entry_persisted(
            self, decision: StrategyDecision, now_mono: float) -> bool:
        if decision.intent not in {"OPEN", "ADD"}:
            return True
        delay = self.cfg.premium_persist_sec
        if delay <= 0:
            return True
        direction = decision.direction
        other = "buy_entropy" if direction == "sell_entropy" else "sell_entropy"
        self._armed[other] = None
        armed = self._armed.get(direction)
        if armed is None:
            self._armed[direction] = now_mono
            self._schedule_poke(delay)
            return False
        elapsed = now_mono - armed
        if elapsed < delay:
            self._schedule_poke(delay - elapsed)
            return False
        return True

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        await self._run_inner()

    async def _load_markets(self) -> None:
        tasks = [
            asyncio.create_task(
                self.entropy.load_market(), name="load-market-entropy"),
            asyncio.create_task(
                self.hedge.load_market(), name="load-market-hedge"),
        ]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _reference_recovery_loop(self, venue: VenueAdapter) -> None:
        recovery_active = False
        while not self.stop.is_set():
            now = time.monotonic()
            if venue.reference.ws_is_fresh(
                    self.cfg.reference_stale_sec, now_mono=now):
                recovery_active = False
                delay = min(self.cfg.reference_rest_recovery_sec,
                            self.cfg.reference_stale_sec)
            else:
                age_ms = venue.reference.age_ms(now_mono=now)
                if not recovery_active and age_ms is not None:
                    remaining = self.cfg.reference_stale_sec - age_ms / 1000.0
                    if remaining > 0.0:
                        delay = min(self.cfg.reference_rest_recovery_sec,
                                    remaining)
                    else:
                        recovery_active = True
                        delay = 0.0
                else:
                    recovery_active = True
                    try:
                        await venue.refresh_reference_rest()
                    except (aiohttp.ClientError, asyncio.TimeoutError,
                            InvalidReference) as exc:
                        log.warning("[%s] reference REST recovery failed: %s",
                                    venue.name, exc)
                    delay = self.cfg.reference_rest_recovery_sec
            if delay <= 0.0:
                continue
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _observe_reference(self, now_mono: Optional[float] = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        entropy_ref = self.entropy.reference.snapshot
        hedge_ref = self.hedge.reference.snapshot
        entropy_age = self.entropy.reference.age_ms(now_mono=now)
        hedge_age = self.hedge.reference.age_ms(now_mono=now)
        stale_limit_ms = self.cfg.reference_stale_sec * 1000.0
        stale = (
            entropy_ref.oracle_px is None
            or hedge_ref.index_px is None
            or entropy_age is None
            or hedge_age is None
            or entropy_age > stale_limit_ms
            or hedge_age > stale_limit_ms
        )
        e_bid, e_ask = self.entropy.book.best_bid(), self.entropy.book.best_ask()
        h_bid, h_ask = self.hedge.book.best_bid(), self.hedge.book.best_ask()
        sell_residual = buy_residual = None
        if not stale and None not in (e_bid, e_ask, h_bid, h_ask):
            sell_residual = calculate_reference_metrics(
                direction="sell_entropy",
                entropy_bid=e_bid, entropy_ask=e_ask,
                hedge_bid=h_bid, hedge_ask=h_ask,
                entropy=entropy_ref, hedge=hedge_ref,
            ).signed_residual_bps
            buy_residual = calculate_reference_metrics(
                direction="buy_entropy",
                entropy_bid=e_bid, entropy_ask=e_ask,
                hedge_bid=h_bid, hedge_ask=h_ask,
                entropy=entropy_ref, hedge=hedge_ref,
            ).signed_residual_bps
        for event in self._reference_alerts.observe(
                now_mono=now,
                sell_residual_bps=sell_residual,
                buy_residual_bps=buy_residual,
                stale=stale):
            if event.kind == "stale":
                if event.active:
                    log.warning(
                        "reference data stale or incomplete — observation "
                        "continues without blocking trading")
                else:
                    log.info("reference data recovered")
            elif event.active:
                log.warning(
                    "%s reference residual alert: %+.2f bps",
                    event.direction, event.value_bps)
            else:
                log.info(
                    "%s reference residual recovered: %+.2f bps",
                    event.direction, event.value_bps)

    async def _reference_monitor_loop(self) -> None:
        while not self.stop.is_set():
            self._observe_reference()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    async def _cleanup(self, tasks: List[asyncio.Task]) -> None:
        self.request_stop()
        if self._primary_error is None:
            for task in tasks:
                if task.done() and not task.cancelled():
                    error = task.exception()
                    if error is not None:
                        self._remember_error(
                            f"background task {task.get_name()}", error)
                        break
        try:
            await self._drain_executions()
        except BaseException as exc:
            self._remember_error("execution drain", exc)
        self._feed_stop.set()
        # Give cancellations already requested by code outside cleanup one
        # loop turn to finish, so they are not mistaken for our own cancels.
        await asyncio.sleep(0)
        cancelled_by_cleanup = set()
        for task in tasks:
            if not task.done():
                self._intentional_task_cancellations.add(task)
                cancelled_by_cleanup.add(task)
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if (isinstance(result, asyncio.CancelledError)
                    and task not in cancelled_by_cleanup
                    and task not in self._task_failures):
                error = RuntimeError(
                    f"background task {task.get_name()} was cancelled "
                    "unexpectedly")
                self._task_failures[task] = error
                self._remember_error(
                    f"background task {task.get_name()}", error)
                continue
            if (isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)
                    and task not in self._task_failures):
                self._task_failures[task] = result
                self._remember_error(
                    f"background task {task.get_name()}", result)
        for venue in self.venues.values():
            await self._close_resource(
                f"[{venue.name}] close", venue.close)
        try:
            self._close_dynamic_strategy()
        except BaseException as exc:
            self._remember_error("strategy event recorder close", exc)
        if self.session is not None:
            await self._close_resource(
                "HTTP session close", self.session.close)

    async def _close_resource(self, label: str, close) -> None:
        task = asyncio.create_task(close(), name=f"close-{label}")
        done, _ = await asyncio.wait(
            {task}, timeout=self.RESOURCE_CLOSE_TIMEOUT_SEC)
        if task not in done:
            error = TimeoutError(
                f"{label} timed out after "
                f"{self.RESOURCE_CLOSE_TIMEOUT_SEC:.1f}s")
            self._remember_error(label, error)
            task.cancel()
            await asyncio.sleep(0)
            return
        try:
            task.result()
        except BaseException as exc:
            self._remember_error(label, exc)

    async def _finish_cleanup(self, tasks: List[asyncio.Task]) -> None:
        cleanup_task = asyncio.create_task(
            self._cleanup(tasks), name="engine-cleanup")
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                self._remember_error("engine cleanup cancellation", exc)
        try:
            cleanup_task.result()
        except BaseException as exc:
            self._remember_error("engine cleanup", exc)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        tasks: List[asyncio.Task] = []
        try:
            runtime = VenueRuntime(
                session=self.session,
                hl_api_url=cfg.hl_api_url,
                hl_ws_url=cfg.hl_ws_url,
                settle_timeout_sec=cfg.settle_timeout_sec,
            )
            self.entropy = create_venue(cfg.entropy, runtime)
            self.venues["entropy"] = self.entropy
            self.hedge = create_venue(cfg.hedge, runtime)
            self.venues["hedge"] = self.hedge
            await self._load_markets()
            self.markets_ready = True

            live = not self.record_only
            if live:
                if not cfg.creds_complete:
                    raise RuntimeError(
                        "live trading needs credentials for both venues in "
                        ".env (see .env.example); use --record-only to run "
                        "without them / 实盘需要在 .env 中配置两个交易所的"
                        "密钥，仅采集数据请用 --record-only")
                self.entropy.init_signer()
                self.hedge.init_signer()
            self.entropy.configure_peer(self.hedge)

            self._step = 10 ** -min(
                self.entropy.size_decimals, self.hedge.size_decimals)
            self._min_base = max(
                self.entropy.min_base, self.hedge.min_base, self._step)
            self._min_notional = max(
                cfg.min_order_notional,
                self.entropy.min_quote, self.hedge.min_quote)
            log.info(
                "pair ENTROPY(%s)-%s(%s): midline=%+.2fbps "
                "band=[-%.2f, +%.2f] fees=%.2f+%.2f step=%g min_ntl=$%g",
                self.entropy.conf.symbol, self.hedge.name,
                self.hedge.conf.symbol, cfg.midline_bps, cfg.lower_bps,
                cfg.upper_bps, self.entropy.fee_bps, self.hedge.fee_bps,
                self._step, self._min_notional)

            if cfg.strategy_mode == "residual_dynamic":
                self._initialize_dynamic_strategy()

            if self.record_only:
                if cfg.strategy_mode == "residual_dynamic":
                    log.warning(
                        "RECORD-ONLY SHADOW — evaluating dynamic residual "
                        "campaigns, no orders")
                else:
                    log.warning(
                        "RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
            else:
                log.warning(
                    "LIVE — real orders will be sent (use --record-only "
                    "for credential-less data collection)")
                await self._reconcile_positions(hedge=False, strict=True)
                log.info(
                    "starting positions: %s (net %+.6g)",
                    " ".join(f"{v.name}={v.position:+.6g}"
                             for v in self.venues.values()),
                    sum(v.position for v in self.venues.values()))
                if cfg.strategy_mode == "residual_dynamic":
                    self._load_dynamic_campaign()
                    try:
                        reconcile_campaign(
                            self.campaign,
                            entropy_position=self.entropy.position,
                            hedge_position=self.hedge.position,
                            step=self._step,
                            net_tolerance=cfg.net_tolerance_base,
                        )
                    except CampaignRecoveryError as exc:
                        self._campaign_recovery_blocked = True
                        self._auto_repair_disabled = True
                        self._pause_for_recovery(str(exc))
                startup_net = sum(
                    v.position for v in self.venues.values())
                if abs(startup_net) > cfg.net_tolerance_base:
                    self._pause_for_recovery(
                        f"startup net residual {startup_net:+.6g}")

            if self.record_only:
                self._start_recorders(tasks)
            notify = (self._record_only_book_update if self.record_only
                      else self._live_progress_update)
            for venue in self.venues.values():
                for task in venue.start_tasks(self._feed_stop, notify, live):
                    self._track_task(tasks, task)
                self._track_task(
                    tasks, asyncio.create_task(
                        self._reference_recovery_loop(venue),
                        name=f"reference-rest-{venue.key}"))
            if ((not self.record_only
                 or cfg.strategy_mode == "residual_dynamic")
                    and not self._campaign_recovery_blocked):
                if not self.record_only:
                    self._start_recorders(tasks)
                self._track_task(
                    tasks, asyncio.create_task(
                        self._strategy_loop(), name="strategy"))
            if not self.record_only:
                self._track_task(
                    tasks, asyncio.create_task(
                        self._balance_loop(), name="balances"))
                if cfg.http_keepalive_sec > 0:
                    self._track_task(
                        tasks, asyncio.create_task(
                            self._http_keepalive_loop(), name="keepalive"))
            self._track_task(
                tasks,
                asyncio.create_task(self._status_loop(), name="status"))
            self._track_task(
                tasks, asyncio.create_task(
                    self._reference_monitor_loop(), name="reference-monitor"))
            if live:
                self._track_task(
                    tasks, asyncio.create_task(
                        self._reconcile_loop(), name="reconcile"))

            await self.stop.wait()
        except BaseException as exc:
            self._remember_error("engine lifecycle", exc)
        finally:
            await self._finish_cleanup(tasks)

        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)
        if self._primary_error is not None:
            raise self._primary_error

    async def _wait_for_recovery_progress(
            self, timeout: float, *, order_venues=(), book_venues=()) -> None:
        waiters = [
            asyncio.create_task(self._unknown_resolved_evt.wait()),
            asyncio.create_task(self._background_failure_evt.wait()),
        ]
        waiters.extend(
            asyncio.create_task(self._venue_progress_evt(
                self._order_progress_evts, key).wait())
            for key in order_venues)
        waiters.extend(
            asyncio.create_task(self._venue_progress_evt(
                self._book_progress_evts, key).wait())
            for key in book_venues)
        try:
            await asyncio.wait(
                waiters, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            self._background_failure_evt.clear()

    def _failed_required_recovery_feed(self) -> Optional[asyncio.Task]:
        pending_order_venues = {
            item.venue.key for item in self._pending_order_confirmations}
        for task in self._task_failures:
            name = task.get_name()
            for venue_key in pending_order_venues:
                if name == f"acct-{venue_key}":
                    return task
            for venue_key in self._residual_waiting_book_venues:
                if name == f"book-{venue_key}":
                    return task
        return None

    async def _abort_failed_recovery_feed(self) -> bool:
        failed = self._failed_required_recovery_feed()
        if failed is None:
            return False
        self._auto_repair_disabled = True
        async with self._recovery_lock:
            pass
        while self._exec_tasks:
            pending = tuple(self._exec_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
        log.critical(
            "required recovery feed %s failed; leaving positions paused for "
            "manual recovery", failed.get_name())
        self._shutdown_reconcile_required = False
        self._residual_waiting_book_venues.clear()
        self._post_order_recovery_active = False
        return True

    async def _drain_executions(self, poll_sec: Optional[float] = None) -> None:
        """Wait for every submitted execution; shutdown never abandons a leg."""
        interval = poll_sec or max(self.cfg.settle_timeout_sec + 2.0, 5.0)
        while (self._exec_tasks or self._shutdown_reconcile_required
               or self._pending_order_confirmations
               or self._pending_snapshot_venues):
            while self._exec_tasks:
                pending = tuple(self._exec_tasks)
                log.info("waiting for %d in-flight execution(s) to settle",
                         len(pending))
                _, still_pending = await asyncio.wait(
                    pending, timeout=interval)
                if still_pending:
                    log.critical(
                        "shutdown still waiting for %d in-flight execution(s); "
                        "orders are not being cancelled", len(still_pending))
            if await self._abort_failed_recovery_feed():
                return
            if not self._shutdown_reconcile_required:
                if (self._pending_order_confirmations
                        or self._pending_snapshot_venues):
                    self._shutdown_reconcile_required = True
                else:
                    continue
            delay = 0.0
            if not self._post_order_recovery_active:
                last_trade = max(
                    (v.last_traded_ts for v in self.venues.values()),
                    default=0.0)
                delay = max(
                    self.RECONCILE_GRACE_SEC - (
                        time.monotonic() - last_trade), 0.0)
            if delay:
                log.warning(
                    "shutdown waiting %.2fs for position state to become "
                    "fresh before reconciling an unknown order", delay)
                await asyncio.sleep(delay)
                # Recompute against the monotonic clock.  A timer may wake a
                # fraction early; attempting immediately would hit the grace
                # guard, then unnecessarily wait a full recovery interval.
                continue
            try:
                wait_for_progress = False
                for event in self._order_progress_evts.values():
                    event.clear()
                for event in self._book_progress_evts.values():
                    event.clear()
                async with self._recovery_lock:
                    if self._shutdown_reconcile_required:
                        recovered = await self._recover_positions_locked(
                            strict=True)
                        wait_for_progress = (
                            not recovered
                            and self._shutdown_reconcile_required)
                        if wait_for_progress:
                            # Clear while holding the recovery lock so a
                            # background recovery cannot signal between the
                            # failed attempt and the wait below.
                            self._unknown_resolved_evt.clear()
                if wait_for_progress:
                    if await self._abort_failed_recovery_feed():
                        return
                    await self._wait_for_recovery_progress(
                        interval,
                        order_venues={
                            item.venue.key
                            for item in self._pending_order_confirmations},
                        book_venues=(
                            () if self._pending_order_confirmations
                            else self._residual_waiting_book_venues))
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._shutdown_reconcile_required:
                    log.exception(
                        "shutdown position reconciliation failed; retrying in "
                        "%.2fs because an order outcome is still unknown",
                        interval)
                    await self._wait_for_recovery_progress(
                        interval,
                        order_venues={
                            item.venue.key
                            for item in self._pending_order_confirmations})

    # --------------------------------------------------------------- signals

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(abs(v.position) * ref / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the reverse premium must clear lower - midline."""
        if sell.key == "entropy":
            base = self.cfg.midline_bps + self.cfg.upper_bps
        else:
            base = self.cfg.lower_bps - self.cfg.midline_bps
        return base + self._inv_add_bps(buy, sell)

    def _headroom(self, buy, sell, ref_px: float) -> float:
        hb = buy.cap_usd - buy.position * ref_px
        hs = sell.cap_usd + sell.position * ref_px
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float):
        return plan_arb(
            buy.book, sell.book,
            threshold_bps=self._eff_threshold(buy, sell),
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.cfg.take_fraction,
            cap_notional=cap_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
        )

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        wakeup = (self._strategy_evt
                  if self.cfg.strategy_mode == "residual_dynamic"
                  else self._update_evt)
        while not self.stop.is_set():
            await wakeup.wait()
            wakeup.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")
                raise

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            if self.cfg.strategy_mode == "residual_dynamic":
                self._strategy_evt.set()
            else:
                self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.monotonic()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted:
            return
        if cfg.strategy_mode == "residual_dynamic":
            await self._evaluate_dynamic()
            return
        now = time.monotonic()
        if now - self.last_trade_mono < cfg.cooldown_sec:
            self._schedule_poke(
                cfg.cooldown_sec - (now - self.last_trade_mono))
            return
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(self._execute_locked(buy, sell, plan))
        self._exec_tasks.add(t)
        t.add_done_callback(self._execution_done)
        await asyncio.shield(t)

    async def _evaluate_dynamic(self) -> None:
        if self.dynamic_strategy is None or self.residual_model is None:
            raise RuntimeError("dynamic strategy is not initialized")
        now_wall = time.time()
        now_mono = time.monotonic()
        self._advance_dynamic_model(
            now_wall=now_wall, now_mono=now_mono)
        self._record_model_snapshot(now_wall=now_wall)
        model = self.residual_model.snapshot(
            now_minute=int(now_wall // 60))
        decision = self.dynamic_strategy.decide(
            market=self._dynamic_market_view(now_mono),
            model=model,
            campaign=self.campaign,
            now_wall=now_wall,
            now_mono=now_mono,
        )
        if decision.intent == "SKIP":
            if self.campaign is None:
                self._clear_armed()
            self._record_dynamic_decision(
                decision, now_wall=now_wall)
            return
        if (decision.intent in {"OPEN", "ADD"}
                and now_mono - self._dynamic_last_action_mono
                < self.cfg.cooldown_sec):
            self._schedule_poke(
                self.cfg.cooldown_sec
                - (now_mono - self._dynamic_last_action_mono))
            deferred = StrategyDecision(
                intent="SKIP", direction=decision.direction,
                reason="COOLDOWN", model=decision.model)
            self._record_dynamic_decision(
                deferred, now_wall=now_wall)
            return
        if not self._dynamic_entry_persisted(decision, now_mono):
            deferred = StrategyDecision(
                intent="SKIP", direction=decision.direction,
                reason="ENTRY_PERSISTING", model=decision.model)
            self._record_dynamic_decision(
                deferred, now_wall=now_wall)
            return
        self._clear_armed()
        if not self.record_only:
            raise RuntimeError(
                "dynamic residual live execution is not initialized")
        self._apply_shadow_decision(decision, now_wall=now_wall)
        self._dynamic_last_action_mono = now_mono

    async def _execute_locked(self, buy, sell, plan: ArbPlan) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        unresolved = False
        audit_error = None
        try:
            unresolved = await self._execute(buy, sell, plan)
        except _TradeAuditFailure as exc:
            audit_error = exc.error
            self._audit_repair_errors.add(audit_error)
            self._audit_repair_available = True
            self._remember_error("trade audit write", audit_error)
            if (self._pending_order_confirmations
                    or self._pending_snapshot_venues):
                self._enter_recovery(
                    f"trade audit failed while an order is unresolved: "
                    f"{audit_error!r}")
            else:
                self._pause_for_recovery(
                    f"trade audit write failed: {audit_error!r}")
        except asyncio.CancelledError:
            self._auto_repair_disabled = True
            self._enter_recovery("execution was cancelled before settlement")
            raise
        except Exception as exc:
            self._auto_repair_disabled = True
            self._enter_recovery(f"execution processing failed: {exc!r}")
            raise
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if audit_error is not None:
            if (not self._shutdown_reconcile_required
                    and not self._unreferenced_unknown):
                try:
                    attempted = await self._maybe_hedge()
                    if (not attempted
                            and self._audit_repair_available
                            and abs(sum(v.position for v in self.venues.values()))
                            > self.cfg.net_tolerance_base):
                        # The post-trade book has not arrived yet.  Preserve
                        # the one-shot grant and make shutdown keep retrying.
                        self._shutdown_reconcile_required = True
                except BaseException:
                    log.exception(
                        "reduce-only repair also failed after trade audit "
                        "error")
            raise audit_error
        try:
            if unresolved:
                self._enter_recovery("order outcome is unresolved")
            else:
                await self._maybe_hedge()
        except asyncio.CancelledError:
            self._auto_repair_disabled = True
            self._enter_recovery("execution aftermath was cancelled")
            raise
        except Exception as exc:
            self._auto_repair_disabled = True
            self._enter_recovery(f"execution aftermath failed: {exc!r}")
            raise
        finally:
            self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        if self._recovery_required:
            self._skiplog("trading paused: waiting for position recovery")
            return None
        best = None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            if not (buy.book.is_fresh(cfg.staleness_sec)
                    and sell.book.is_fresh(cfg.staleness_sec)):
                self._armed[dkey] = None
                continue
            if not (buy.ready_to_trade() and sell.ready_to_trade()):
                self._armed[dkey] = None
                continue
            if self._venue_down:
                self._armed[dkey] = None
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if self._venue_limited(buy) or self._venue_limited(sell):
                continue  # reactive 429 exclusion
            if not (self._venue_rate_ok(buy) and self._venue_rate_ok(sell)):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_mono <= buy.last_traded_ts
                    or sell.book.last_update_mono <= sell.last_traded_ts):
                continue
            plan, reason = self._plan(buy, sell, cfg.max_order_notional)
            edge_present = reason not in ("no_edge", "empty_book")
            if not edge_present:
                self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                continue
            headroom = self._headroom(buy, sell, plan.buy_limit)
            if headroom < plan.buy_notional:
                plan, _ = self._plan(buy, sell,
                                     min(cfg.max_order_notional, headroom))
                if plan is None:
                    self._skiplog("%s blocked by position caps (headroom $%.0f)",
                                  dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    # ------------------------------------------------------------- execution

    async def _execute(self, buy, sell, plan: ArbPlan) -> bool:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns True when an outcome is unresolved and the caller
        must escalate to reconcile."""
        if self.halted:
            return False
        cfg = self.cfg
        inv_bps = self._inv_add_bps(buy, sell)
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        dispatch_at = time.monotonic()
        self.last_trade_ts = time.time()
        self.last_trade_mono = dispatch_at
        log.info("[ARB] %s: BUY %s %.6g @<=%.6g | SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | prem %.2fbps | exp $%.4f",
                 direction, buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.marginal_premium_bps, plan.exp_edge_usd)
        slip = cfg.leg_slippage_bps / 1e4
        buy_bound = buy.px_round(plan.buy_limit * (1 + slip), round_up=False)
        sell_bound = sell.px_round(plan.sell_limit * (1 - slip), round_up=True)
        self._record_send(buy)
        self._record_send(sell)
        order_submitted_at = {}

        async def submit(venue, **kwargs):
            order_submitted_at[venue.key] = time.monotonic()
            return await venue.send_taker(**kwargs)

        settlement = asyncio.gather(
            submit(buy, is_buy=True, qty=plan.qty, limit_px=buy_bound),
            submit(sell, is_buy=False, qty=plan.qty, limit_px=sell_bound),
            return_exceptions=True)
        cancellation = None
        while True:
            try:
                raw_results = await asyncio.shield(settlement)
                break
            except asyncio.CancelledError as exc:
                # Once order submission has started, cancellation must not
                # discard adapter results (and their client order references).
                # Preserve the request and re-raise it after both legs settle.
                if cancellation is None:
                    cancellation = exc
        settled_at = time.monotonic()
        buy.last_traded_ts = sell.last_traded_ts = settled_at
        results = []
        for result in raw_results:
            if isinstance(result, BaseException):
                results.append(OrderResult.unknown(
                    "adapter-exception", repr(result)))
            elif isinstance(result, OrderResult):
                results.append(result)
            else:
                raise TypeError(
                    f"venue returned {type(result).__name__}, expected OrderResult")
        binfo, sinfo = results
        for venue, info, side in ((buy, binfo, "buy"),
                                  (sell, sinfo, "sell")):
            if info.err:
                log.error("[%s] %s leg: %s", venue.name, side, info.err)
        for venue, info, is_buy in (
                (buy, binfo, True), (sell, sinfo, False)):
            if not info.unresolved:
                continue
            self._register_unresolved_order(
                venue, info, is_buy=is_buy,
                applied_fill=info.filled_base)
        bfill = binfo.filled_base
        sfill = sinfo.filled_base
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.avg_px or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
        if sfill:
            spx = sinfo.avg_px or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx

        matched = min(bfill, sfill)
        fill_edge = 0.0
        if matched > 0 and binfo.avg_px and sinfo.avg_px:
            fill_edge = matched * (
                sinfo.avg_px * (1 - plan.sell_fee)
                - binfo.avg_px * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo.status, bfill, plan.qty,
                 sell.name, sinfo.status, sfill, plan.qty, matched, fill_edge)
        unresolved = binfo.unresolved or sinfo.unresolved
        net = sum(v.position for v in self.venues.values())
        if unresolved or abs(net) > cfg.net_tolerance_base:
            # Book/account updates use independent streams.  Residual repair
            # may use a fresh book received after submission even when its
            # update arrived before the terminal account message.
            self._residual_book_after[buy.key] = order_submitted_at[buy.key]
            self._residual_book_after[sell.key] = order_submitted_at[sell.key]
            self._post_order_recovery_active = True
        else:
            self._residual_book_after.pop(buy.key, None)
            self._residual_book_after.pop(sell.key, None)
        hard_err = binfo.err is not None or sinfo.err is not None
        rate_limited = False
        for venue, info in ((buy, binfo), (sell, sinfo)):
            if info.rate_limited:
                rate_limited = True
                self._mark_limited(venue)
            elif "margin" in info.status.lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", venue.name)
                self._mark_limited(venue)
        fills_match = (matched > 0
                       and abs(bfill - sfill) <= cfg.net_tolerance_base)
        terminal_success = (binfo.status == "filled"
                            and sinfo.status == "filled")
        sent_ok = (terminal_success and not hard_err
                   and not unresolved and fills_match)
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                self.halted = True
                log.critical("HALTED after %d consecutive execution problems "
                             "— flatten manually and restart / 连续执行异常，"
                             "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(
            direction, plan, None if unresolved else fill_edge,
            f"{binfo.status}/{sinfo.status}", sent_ok)
        audit_failure = None
        try:
            self._log_csv(
                direction, buy, sell, plan, sent_ok, bfill, sfill,
                binfo.status, sinfo.status, fill_edge, inv_bps)
        except Exception as exc:
            audit_failure = _TradeAuditFailure(exc)
        self.last_trade_ts = time.time()
        self.last_trade_mono = time.monotonic()
        if cancellation is not None:
            if audit_failure is not None:
                self._remember_error("trade audit write", audit_failure.error)
            raise cancellation
        if audit_failure is not None:
            raise audit_failure from audit_failure.error
        return bool(unresolved)

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool) -> None:
        self.recent_trades.append({
            "ts": time.time(), "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.marginal_premium_bps,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok})

    async def _maybe_hedge(self) -> bool:
        net = sum(v.position for v in self.venues.values())
        if abs(net) <= self.cfg.net_tolerance_base:
            return False
        task = asyncio.create_task(
            self._hedge_and_check(net), name="hedge-residual")
        self._exec_tasks.add(task)
        task.add_done_callback(self._execution_done)
        return await asyncio.shield(task)

    async def _hedge_and_check(self, net: float) -> bool:
        try:
            attempted = await self._hedge(net)
            remaining = sum(v.position for v in self.venues.values())
            if abs(remaining) <= self.cfg.net_tolerance_base:
                self._residual_book_after.clear()
                self._residual_waiting_book_venues.clear()
                self._post_order_recovery_active = False
            else:
                waiting_books = self._residual_book_wait_keys(remaining)
                if attempted and not self._pending_order_confirmations:
                    self._auto_repair_disabled = True
                    self._residual_book_after.clear()
                    self._residual_waiting_book_venues.clear()
                    self._post_order_recovery_active = False
                elif (not attempted and self._post_order_recovery_active
                      and waiting_books):
                    self._residual_waiting_book_venues = waiting_books
                    self._shutdown_reconcile_required = True
                else:
                    self._residual_waiting_book_venues.clear()
                    if (not attempted and self._post_order_recovery_active
                            and not self._pending_order_confirmations
                            and not self._pending_snapshot_venues):
                        self._residual_book_after.clear()
                        self._post_order_recovery_active = False
                        self._shutdown_reconcile_required = False
                self._pause_for_recovery(
                    f"net residual {remaining:+.6g} remains after hedge attempt")
            return attempted
        except asyncio.CancelledError as exc:
            self._auto_repair_disabled = True
            raise RuntimeError(
                "hedge execution was cancelled unexpectedly") from exc
        except Exception:
            self._auto_repair_disabled = True
            raise

    async def _hedge(self, net: float) -> bool:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        attempted = False
        audit_repair_attempted = False
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down \
                    or not v.book.is_fresh(cfg.staleness_sec):
                continue  # unreachable or blind: cannot hedge here
            book_after = self._residual_book_after.get(
                v.key, v.last_traded_ts)
            if v.book.last_update_mono <= book_after:
                self._schedule_reconcile(1.0)
                continue  # wait for a book newer than order submission
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            remaining = sum(venue.position for venue in self.venues.values())
            if remaining * sgn <= self.cfg.net_tolerance_base:
                return attempted
            qty = floor_step(
                min(abs(remaining), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            if qty * limit < max(cfg.min_order_notional, v.min_quote):
                continue
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            remaining, "SELL" if is_sell else "BUY", qty,
                            v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                if self._audit_repair_available:
                    # An audit failure permits exactly one supervised repair
                    # order.  Consume it only when submission really starts.
                    self._audit_repair_available = False
                    self._auto_repair_disabled = True
                    audit_repair_attempted = True
                attempted = True
                try:
                    info = await v.send_taker(
                        is_buy=not is_sell, qty=qty,
                        limit_px=limit, reduce_only=True)
                except asyncio.CancelledError:
                    v.last_traded_ts = time.monotonic()
                    self._enter_recovery(
                        f"hedge submission on {v.name} was cancelled")
                    raise
                except Exception as exc:
                    v.last_traded_ts = time.monotonic()
                    log.exception("[HEDGE] %s submission failed", v.name)
                    self._unreferenced_unknown = True
                    self._auto_repair_disabled = True
                    self._enter_recovery(
                        f"hedge submission on {v.name} is unresolved: {exc!r}")
                    raise
                if not isinstance(info, OrderResult):
                    v.last_traded_ts = time.monotonic()
                    self._enter_recovery(
                        f"hedge adapter {v.name} returned an invalid result")
                    raise TypeError(
                        f"venue returned {type(info).__name__}, "
                        "expected OrderResult")
                if info.err or info.unresolved:
                    log.error("[HEDGE] %s: %s", v.name,
                              info.err or "unresolved")
                    if info.rate_limited:
                        self._mark_limited(v)
                    if info.unresolved:
                        self._register_unresolved_order(
                            v, info, is_buy=not is_sell, applied_fill=0.0,
                            is_residual_hedge=True)
                        self._enter_recovery(
                            f"hedge outcome on {v.name} is not confirmed")
                    else:
                        self._pause_for_recovery(
                            f"hedge on {v.name} was rejected")
                else:
                    fill = info.filled_base
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.avg_px or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info.status, fill, qty)
                v.last_traded_ts = time.monotonic()
            finally:
                lk.release()
            if audit_repair_attempted or info.err or info.unresolved \
                    or info.filled_base <= 0:
                return attempted
        log.warning("[HEDGE] net %+.6g has no eligible venue/book — carrying "
                    "(next reconcile retries)", net)
        return attempted

    def _residual_book_wait_keys(self, net: Optional[float] = None) -> set[str]:
        net = (sum(v.position for v in self.venues.values())
               if net is None else net)
        if abs(net) <= self.cfg.net_tolerance_base:
            return set()
        sign = 1.0 if net > 0 else -1.0
        waiting = set()
        for venue in self.venues.values():
            if venue.position * sign <= 0 or venue.key in self._venue_down:
                continue
            cutoff = self._residual_book_after.get(
                venue.key, venue.last_traded_ts)
            if (not venue.book.is_fresh(self.cfg.staleness_sec)
                    or venue.book.last_update_mono <= cutoff):
                waiting.add(venue.key)
        return waiting

    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    def _schedule_reconcile(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if (self._reconcile_due is not None
                and self._reconcile_due <= due + 0.02):
            return

        def _fire() -> None:
            self._reconcile_due = None
            if not self.stop.is_set():
                self._reconcile_evt.set()

        self._reconcile_due = due
        loop.call_at(due, _fire)

    async def _reconcile_positions(
            self, hedge: bool, strict: bool = False,
            venue_keys: Optional[set[str]] = None) -> bool:
        now = time.monotonic()
        vs = []
        retry_after = None
        candidates = [
            v for v in self.venues.values()
            if venue_keys is None or v.key in venue_keys
        ]
        for v in candidates:
            age = now - v.last_traded_ts
            if age < self.RECONCILE_GRACE_SEC:
                remaining = self.RECONCILE_GRACE_SEC - age
                retry_after = remaining if retry_after is None \
                    else min(retry_after, remaining)
                continue  # just traded: chain read would be stale
            if not strict and v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if retry_after is not None:
            self._schedule_reconcile(retry_after)
        if not vs:
            return False
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        complete = (len(vs) == len(candidates)
                    and all(result is True for result in got))
        if hedge and complete:
            await self._maybe_hedge()
        return complete

    async def _reconcile_venue(self, v, strict: bool) -> bool:
        async with self._vlock(v.key):
            now = time.monotonic()
            age = now - v.last_traded_ts
            if age < self.RECONCILE_GRACE_SEC:
                self._schedule_reconcile(self.RECONCILE_GRACE_SEC - age)
                return False  # traded while waiting for the lock
            try:
                r = float(await v.fetch_position())
                if not math.isfinite(r):
                    raise ValueError(f"non-finite position {r!r}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    self._clear_armed()
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                 "it recovers", v.name, n,
                                 self.cfg.venue_probe_sec)
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                return False
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                            "trading RESUMED", v.name,
                            now - self._venue_down.pop(v.key))
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            if (abs(delta) > 1e-12 and getattr(v, "kind", None) == "lighter"
                    and v.last_traded_ts > 0):
                self._pause_for_recovery(
                    f"[{v.name}] unversioned post-trade position snapshot "
                    f"{r:+.6g} differs from local {v.position:+.6g}")
                self._schedule_reconcile(1.0)
                return False
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r
            return True

    async def _recover_positions(self, strict: bool) -> bool:
        async with self._recovery_lock:
            return await self._recover_positions_locked(strict)

    async def _resolve_pending_orders(self) -> bool:
        confirmations = list(self._pending_order_confirmations)
        pending = []
        resolved_residual = False
        for index, confirmation in enumerate(confirmations):
            # Keep the current and unprocessed references recoverable if this
            # iteration is cancelled or violates the adapter contract.
            self._pending_order_confirmations = (
                pending + confirmations[index:])
            try:
                result = await confirmation.venue.resolve_order(
                    confirmation.order_ref)
            except asyncio.CancelledError:
                raise
            except (AttributeError, IndexError, KeyError,
                    TypeError, ValueError) as exc:
                raise _OrderRecoveryInvariantError(
                    f"[{confirmation.venue.name}] order "
                    f"{confirmation.order_ref}: resolve_order response does "
                    "not match the adapter contract") from exc
            if result is None:
                pending.append(confirmation)
                self._pending_order_confirmations = (
                    pending + confirmations[index + 1:])
                continue
            resolved_at = time.monotonic()
            if not isinstance(result, OrderResult) or result.unresolved:
                raise _OrderRecoveryInvariantError(
                    f"[{confirmation.venue.name}] order "
                    f"{confirmation.order_ref}: resolve_order must return a "
                    "terminal OrderResult or None")
            additional_fill = result.filled_base - confirmation.applied_fill
            if additional_fill < -1e-12:
                raise _OrderRecoveryInvariantError(
                    f"[{confirmation.venue.name}] order "
                    f"{confirmation.order_ref}: terminal fill "
                    f"{result.filled_base} is below already observed fill "
                    f"{confirmation.applied_fill}")
            if additional_fill > 0 and result.avg_px is None:
                raise _OrderRecoveryInvariantError(
                    f"[{confirmation.venue.name}] order "
                    f"{confirmation.order_ref}: terminal fill has no average "
                    "price")
            venue = confirmation.venue
            venue.last_traded_ts = max(venue.last_traded_ts, resolved_at)
            if additional_fill > 0:
                px = result.avg_px
                venue.position += additional_fill if confirmation.is_buy \
                    else -additional_fill
                fee = venue.fee_bps / 1e4
                venue.cash += (-additional_fill * px * (1 + fee)
                               if confirmation.is_buy
                               else additional_fill * px * (1 - fee))
                venue.volume_usd += additional_fill * px
            resolved_residual = (
                resolved_residual or confirmation.is_residual_hedge)
            log.warning(
                "[%s] unresolved order %s reached terminal status %s "
                "with fill %.6g",
                confirmation.venue.name, confirmation.order_ref,
                result.status, result.filled_base)
            self._pending_order_confirmations = (
                pending + confirmations[index + 1:])
        if (resolved_residual
                and abs(sum(v.position for v in self.venues.values()))
                > self.cfg.net_tolerance_base):
            self._auto_repair_disabled = True
            log.critical(
                "residual hedge reached terminal status with net exposure "
                "remaining — automatic repair disabled; manual recovery "
                "required")
        self._pending_order_confirmations = pending
        if pending:
            self._schedule_reconcile(1.0)
            return False
        return True

    async def _recover_positions_locked(self, strict: bool) -> bool:
        generation = self._recovery_generation
        order_recovery = bool(
            self._post_order_recovery_active
            or self._pending_order_confirmations
            or self._pending_snapshot_venues)
        try:
            if not await self._resolve_pending_orders():
                return False
        except _OrderRecoveryInvariantError as exc:
            self._auto_repair_disabled = True
            manual = list(self._pending_order_confirmations)
            self._manual_order_confirmations.extend(manual)
            self._pending_order_confirmations.clear()
            if manual:
                log.critical(
                    "order recovery requires manual confirmation: %s",
                    ", ".join(
                        f"{item.venue.name}:{item.order_ref}"
                        for item in manual))
            self._shutdown_reconcile_required = bool(
                self._pending_snapshot_venues)
            self._residual_book_after.clear()
            self._residual_waiting_book_venues.clear()
            self._post_order_recovery_active = False
            self._remember_error("order recovery contract", exc)
            self.request_stop()
            return False
        if self._pending_snapshot_venues:
            complete = await self._reconcile_positions(
                hedge=False, strict=strict,
                venue_keys=set(self._pending_snapshot_venues))
            if complete:
                self._pending_snapshot_venues.clear()
        elif order_recovery:
            complete = True
        else:
            complete = await self._reconcile_positions(
                hedge=False, strict=strict)
        unknown_resolved = (
            complete and generation == self._recovery_generation)
        if not unknown_resolved:
            return False
        self._unknown_resolved_evt.set()
        if self._auto_repair_disabled:
            self._shutdown_reconcile_required = False
            self._residual_book_after.clear()
            self._residual_waiting_book_venues.clear()
            self._post_order_recovery_active = False
            return False
        await self._maybe_hedge()
        net = sum(v.position for v in self.venues.values())
        recovered = (generation == self._recovery_generation
                     and abs(net) <= self.cfg.net_tolerance_base)
        if recovered:
            self._shutdown_reconcile_required = False
            self._recovery_required = False
            self._residual_book_after.clear()
            self._residual_waiting_book_venues.clear()
            self._post_order_recovery_active = False
            log.warning("position recovery complete — trading RESUMED")
            self._update_evt.set()
        elif self._auto_repair_disabled:
            self._shutdown_reconcile_required = bool(
                self._pending_order_confirmations
                or self._pending_snapshot_venues)
        else:
            self._shutdown_reconcile_required = bool(
                self._pending_order_confirmations
                or self._pending_snapshot_venues
                or self._residual_waiting_book_venues)
        return recovered

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                if not self._recovery_required:
                    await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                if self._recovery_required:
                    await self._recover_positions(strict=True)
                else:
                    await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            total += v.equity - v.start_equity
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m is None:
                return None
            total += v.cash + v.position * m
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        return (em / hm - 1.0) * 1e4

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                + ("" if v.book.is_fresh(cfg.staleness_sec) else " STALE")
                + (" RATE-LTD" if self._venue_limited(v) else "")
                + (" DOWN" if v.key in self._venue_down else "")
                for v in self.venues.values())
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            log.info("[status] %s | prem %s bps (band %+.2f..%+.2f) | pos %s "
                     "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                     "fillEdge $%.4f%s%s",
                     books, prem_s, cfg.midline_bps - cfg.lower_bps,
                     cfg.midline_bps + cfg.upper_bps, pos, net, self.trades,
                     self.hedges,
                     f"${pnl:+.4f}" if pnl is not None else "—",
                     self.total_exp_edge, self.total_fill_edge, rec,
                     " *** HALTED ***" if self.halted else "")

    def _log_csv(self, direction, buy, sell, plan: ArbPlan, ok: bool, bfill,
                 sfill, bstatus, sstatus, fill_edge, inv_bps) -> None:
        path = self.cfg.trades_csv
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(path):
            if (not csv_header_matches(path, CSV_HEADER)
                    or not csv_tail_complete(path, CSV_HEADER)):
                os.replace(path, next_archive_path(path))
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(CSV_HEADER)
            w.writerow([f"{time.time():.3f}",
                        direction, buy.name, sell.name, f"{plan.qty:.8g}",
                        plan.buy_limit, plan.sell_limit,
                        f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
                        f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
                        f"{plan.marginal_premium_bps:.3f}",
                        f"{self.cfg.midline_bps:.3f}",
                        f"{inv_bps:.3f}", int(ok), f"{bfill:.8g}",
                        f"{sfill:.8g}", bstatus, sstatus, f"{fill_edge:.4f}"])
