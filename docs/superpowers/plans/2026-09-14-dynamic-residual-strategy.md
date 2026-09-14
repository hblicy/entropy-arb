# Dynamic Residual Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace structural raw-premium signals with a reference-adjusted dynamic residual model, one-campaign lifecycle, bounded dynamic slippage, durable recovery, and a deterministic record-only shadow path.

**Architecture:** Add focused strategy, campaign, slippage, and event-recorder modules above the existing venue and execution recovery layer. First make the complete lifecycle run in shadow mode against the existing order books; then connect the same decisions to live fills and durable campaign reconciliation without changing venue adapters or unknown-order recovery.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, csv/json standard library, PyYAML, pytest

---

## File map

- Create `entropy_arb/strategy.py`: rolling residual model, model snapshot, strategy decision, lifecycle rules, convergence math.
- Create `entropy_arb/campaign.py`: campaign value object, schema validation, atomic state store, restart reconciliation.
- Create `entropy_arb/slippage.py`: per-venue/side real-fill samples, percentile budget and anomaly state.
- Create `entropy_arb/strategy_recorder.py`: low-frequency strategy event journal with stable CSV schema.
- Modify `entropy_arb/config.py`: strict `strategy` and `slippage` configuration with legacy defaults and live safety gate.
- Modify `entropy_arb/book.py`: matched-depth planner for convergence trades and hard exits.
- Modify `entropy_arb/engine.py`: model warm-up, shadow decisions, campaign priority, live fill application and restart reconciliation.
- Modify `tools/analyze.py`: optional strategy-event summary without changing the existing minute analysis.
- Create `tools/replay_strategy.py`: deterministic top-of-book historical shadow replay for supplied minute and signal archives.
- Modify `config.example.yaml`, `README.md`, `README.zh-CN.md`: explicit residual shadow configuration and runbook.
- Create `tests/test_strategy.py`, `tests/test_campaign.py`, `tests/test_slippage.py`, `tests/test_strategy_recorder.py`.
- Create `tests/test_replay_strategy.py`.
- Modify `tests/test_config.py`, `tests/test_book.py`, `tests/test_engine.py`, `tests/test_analyze.py`, `tests/test_main.py`.

## Phase A — deterministic shadow lifecycle

### Task 1: Add strict, backward-compatible configuration

**Files:**
- Modify: `entropy_arb/config.py`
- Modify: `config.example.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_main.py`

- [ ] **Step 1: Write failing legacy/default and residual config tests**

Add tests proving that an absent section selects legacy behavior, the example selects residual shadow behavior, cross-field constraints fail, and residual live cannot use zero persistence:

```python
def test_missing_strategy_keeps_fixed_premium_mode():
    cfg = load(MINIMAL)
    assert cfg.strategy_mode == "fixed_premium"
    assert cfg.strategy_live_enabled is False


def test_example_enables_residual_shadow_defaults():
    cfg = load_config(EXAMPLE, NO_ENV, symbol="ANTH",
                      hedge_symbol="ANTHROPIC", hedge_venue="lighter-rh",
                      record_only=True)
    assert cfg.strategy_mode == "residual_dynamic"
    assert cfg.strategy_window_minutes == 180
    assert cfg.strategy_min_samples == 120
    assert cfg.strategy_soft_hold_minutes == 60
    assert cfg.strategy_hard_hold_minutes == 360
    assert cfg.strategy_live_enabled is False
    assert cfg.slippage_bootstrap_bps == 5.0
    assert cfg.slippage_hard_max_bps == 20.0


@pytest.mark.parametrize("extra, message", [
    ("strategy:\n  mode: other\n", "strategy.mode"),
    ("strategy:\n  mode: residual_dynamic\n  min_samples: 181\n",
     "min_samples"),
    ("strategy:\n  mode: residual_dynamic\n  lower_quantile: .9\n"
     "  upper_quantile: .1\n", "quantile"),
    ("strategy:\n  mode: residual_dynamic\n  soft_hold_minutes: 360\n"
     "  hard_hold_minutes: 60\n", "soft_hold_minutes"),
    ("slippage:\n  min_bps: 21\n  hard_max_bps: 20\n",
     "slippage.min_bps"),
])
def test_dynamic_config_rejects_invalid_cross_field_values(extra, message):
    expect_error(MINIMAL + extra, message)


def test_dynamic_live_requires_positive_persistence():
    with pytest.raises(ConfigError, match="premium_persist_sec"):
        load(MINIMAL + "\nstrategy:\n  mode: residual_dynamic\n"
             "  live_enabled: true\nexecution:\n"
             "  premium_persist_sec: 0\n")


def test_residual_live_requires_explicit_live_enabled():
    with pytest.raises(ConfigError, match="live_enabled"):
        load(MINIMAL + "\nstrategy:\n  mode: residual_dynamic\n"
             "  live_enabled: false\n", record_only=False)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m pytest tests/test_config.py tests/test_main.py -q
```

Expected: failures because `Config` and `_SCHEMA` do not expose `strategy_*` or `slippage_*` fields and the example has no sections.

- [ ] **Step 3: Implement the minimal config contract**

Add the following scalar fields to `Config` and matching strict nested schema entries:

```python
strategy_mode: str
strategy_live_enabled: bool
strategy_window_minutes: int
strategy_min_samples: int
strategy_lower_quantile: float
strategy_upper_quantile: float
strategy_regime_window_minutes: int
strategy_regime_recovery_minutes: int
strategy_exit_band_fraction: float
strategy_min_exit_band_bps: float
strategy_min_expected_profit_bps: float
strategy_soft_hold_minutes: int
strategy_hard_hold_minutes: int
strategy_entry_reference_max_age_sec: float
strategy_entry_reference_max_skew_sec: float
strategy_state_file: str
strategy_event_csv: str
slippage_bootstrap_bps: float
slippage_min_bps: float
slippage_safety_bps: float
slippage_hard_max_bps: float
slippage_max_edge_fraction: float
slippage_min_live_samples: int
```

Parse absent `strategy` as `fixed_premium`; use the confirmed defaults only when residual mode is selected. Validate:

```python
if cfg.strategy_mode not in {"fixed_premium", "residual_dynamic"}:
    raise ConfigError("'strategy.mode' must be fixed_premium or residual_dynamic")
if not 0 <= cfg.strategy_lower_quantile < .5 < cfg.strategy_upper_quantile <= 1:
    raise ConfigError("strategy quantiles must satisfy 0 <= lower < .5 < upper <= 1")
if cfg.strategy_min_samples > cfg.strategy_window_minutes:
    raise ConfigError("'strategy.min_samples' must be <= window_minutes")
if cfg.strategy_soft_hold_minutes >= cfg.strategy_hard_hold_minutes:
    raise ConfigError("'strategy.soft_hold_minutes' must be < hard_hold_minutes")
if cfg.slippage_min_bps > cfg.slippage_hard_max_bps:
    raise ConfigError("'slippage.min_bps' must be <= hard_max_bps")
if (cfg.strategy_mode == "residual_dynamic"
        and cfg.strategy_live_enabled
        and cfg.premium_persist_sec <= 0):
    raise ConfigError("residual live requires execution.premium_persist_sec > 0")
if (cfg.strategy_mode == "residual_dynamic"
        and not record_only and not cfg.strategy_live_enabled):
    raise ConfigError("residual live requires strategy.live_enabled=true")
```

Add strategy state/event paths to output collision validation only when residual mode is active. In `config.example.yaml`, add the confirmed sections, set persistence to `3.0`, cooldown to `60.0`, and leave `live_enabled: false`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_config.py tests/test_main.py -q
```

Expected: all focused tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/config.py config.example.yaml tests/test_config.py tests/test_main.py
git commit -m "功能：增加动态残差策略配置"
```

### Task 2: Implement the rolling residual model

**Files:**
- Create: `entropy_arb/strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: Write failing percentile, window and regime tests**

Define the wished-for API and cover deterministic interpolation, minute replacement, time-window expiry, readiness and recovery:

```python
def model(**overrides):
    opts = dict(window_minutes=180, min_samples=120,
                lower_quantile=.10, upper_quantile=.90,
                regime_window_minutes=60, recovery_minutes=15)
    opts.update(overrides)
    return ResidualModel(**opts)


def test_model_replaces_same_minute_and_uses_linear_quantiles():
    m = model(window_minutes=10, min_samples=4,
              regime_window_minutes=2, recovery_minutes=1)
    for minute, value in enumerate([0.0, 10.0, 20.0, 30.0]):
        m.observe(minute=minute, residual_bps=value, valid=True)
    m.observe(minute=3, residual_bps=40.0, valid=True)
    snap = m.snapshot(now_minute=3)
    assert snap.samples == 4
    assert snap.median_bps == pytest.approx(15.0)
    assert snap.lower_bps == pytest.approx(3.0)
    assert snap.upper_bps == pytest.approx(34.0)


def test_model_uses_elapsed_minutes_not_last_n_rows():
    m = model(window_minutes=3, min_samples=2,
              regime_window_minutes=2, recovery_minutes=1)
    m.observe(minute=1, residual_bps=1.0, valid=True)
    m.observe(minute=2, residual_bps=2.0, valid=True)
    assert m.snapshot(now_minute=2).ready
    assert not m.snapshot(now_minute=5).ready


def test_regime_requires_fifteen_new_stable_minutes_to_recover():
    m = model(window_minutes=180, min_samples=120,
              regime_window_minutes=60, recovery_minutes=15)
    seed_stable_minutes(m, end_minute=179, value=0.0)
    m.observe(minute=180, residual_bps=100.0, valid=True)
    assert m.snapshot(now_minute=180).status == "REGIME_UNSTABLE"
    for minute in range(181, 195):
        m.observe(minute=minute, residual_bps=0.0, valid=True)
        assert m.snapshot(now_minute=minute).status == "REGIME_UNSTABLE"
    m.observe(minute=195, residual_bps=0.0, valid=True)
    assert m.snapshot(now_minute=195).status == "READY"


def test_five_missing_real_minutes_marks_regime_unstable():
    m = ready_model()
    for minute in range(180, 185):
        m.observe(minute=minute, residual_bps=None, valid=False)
    assert m.snapshot(now_minute=184).status == "REGIME_UNSTABLE"
```

- [ ] **Step 2: Run the model tests and verify RED**

Run:

```bash
python -m pytest tests/test_strategy.py -q
```

Expected: collection fails because `entropy_arb.strategy` does not exist.

- [ ] **Step 3: Implement immutable snapshots and minute-indexed state**

Implement these public values and keep quantile calculation local to the module:

```python
@dataclass(frozen=True)
class ModelSnapshot:
    version: int
    minute: int
    samples: int
    status: str
    median_bps: Optional[float]
    lower_bps: Optional[float]
    q25_bps: Optional[float]
    q75_bps: Optional[float]
    upper_bps: Optional[float]

    @property
    def ready(self) -> bool:
        return self.status == "READY"

    @property
    def iqr_bps(self) -> Optional[float]:
        if self.q25_bps is None or self.q75_bps is None:
            return None
        return self.q75_bps - self.q25_bps


class ResidualModel:
    def observe(self, *, minute: int,
                residual_bps: Optional[float], valid: bool) -> None:
        """Store one close per real UTC minute and update recovery state."""

    def snapshot(self, *, now_minute: int) -> ModelSnapshot:
        """Compute the current robust window without retaining expired rows."""
```

Reject booleans, non-integer/negative minutes and non-finite valid residuals with `ValueError`. A missing minute is represented by `valid=False`; it participates in consecutive-missing detection but not quantiles. Do not synthesize intermediate observations.

- [ ] **Step 4: Run model tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_strategy.py -q
```

Expected: all model tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "功能：实现滚动残差模型"
```

### Task 3: Load validated minute history into the model

**Files:**
- Modify: `entropy_arb/strategy.py`
- Modify: `tests/test_strategy.py`

- [ ] **Step 1: Write failing warm-start tests**

```python
def test_warm_start_filters_identity_age_skew_future_and_old_rows(tmp_path):
    path = tmp_path / "minutes.csv"
    write_minute_fixture(path, [
        minute_row(100, residual="1", e_age="100", h_age="100", skew="0"),
        minute_row(101, residual="2", entropy_symbol="OTHER"),
        minute_row(102, residual="3", e_age="16000"),
        minute_row(103, residual="4", skew="16000"),
        minute_row(104, residual="nan"),
        minute_row(201, residual="5"),
    ])
    loaded = warm_start_residual_model(
        model(window_minutes=180, min_samples=1), path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200, max_age_sec=15, max_skew_sec=15)
    assert loaded.accepted == 1
    assert loaded.rejected_identity == 1
    assert loaded.rejected_reference == 2
    assert loaded.rejected_value == 1
    assert loaded.rejected_time == 1


def test_warm_start_rejects_incompatible_header(tmp_path):
    path = tmp_path / "minutes.csv"
    path.write_text("minute_ts,residual_close_bps\n", encoding="utf-8")
    with pytest.raises(ValueError, match="minute history header"):
        warm_start_residual_model(
            model(window_minutes=180, min_samples=1), path=str(path),
            identity=MarketIdentity(
                "ANTH", "io", "ANTHROPIC", "lighter-rh"),
            now_minute=200, max_age_sec=15, max_skew_sec=15)
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_strategy.py -q
```

Expected: failures because `MarketIdentity`, `WarmStartResult`, and `warm_start_residual_model` are absent.

- [ ] **Step 3: Implement strict history loading**

Add:

```python
@dataclass(frozen=True)
class MarketIdentity:
    entropy_symbol: str
    entropy_dex: str
    hedge_symbol: str
    hedge_venue: str


@dataclass(frozen=True)
class WarmStartResult:
    accepted: int = 0
    rejected_identity: int = 0
    rejected_reference: int = 0
    rejected_value: int = 0
    rejected_time: int = 0
```

Require the existing 44-column schema fields used by the loader, parse with `csv.DictReader`, validate every accepted value as finite, and feed accepted rows in sorted minute order. A missing file returns an empty result and leaves the model not ready; unreadable files and incompatible headers propagate a contextual error.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_strategy.py -q
```

Expected: all strategy model and warm-start tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "功能：从分钟数据预热残差模型"
```

### Task 4: Implement dynamic slippage budgets

**Files:**
- Create: `entropy_arb/slippage.py`
- Create: `tests/test_slippage.py`

- [ ] **Step 1: Write failing bootstrap, p95 and anomaly tests**

```python
def test_bootstrap_and_edge_share_bound_the_budget():
    model = SlippageModel(bootstrap_bps=5, min_bps=1, safety_bps=1,
                          hard_max_bps=20, min_live_samples=10)
    quote = model.quote(venue="entropy", side="buy", now=100,
                        convergence_bps=18, round_trip_fee_bps=2,
                        min_profit_bps=2, max_edge_fraction=.25)
    assert quote.statistical_bps == 5
    assert quote.edge_cap_bps == pytest.approx(3.5)
    assert quote.budget_bps == pytest.approx(3.5)


def test_quote_is_unavailable_when_edge_cap_is_below_minimum():
    model = make_model()
    quote = model.quote(venue="entropy", side="buy", now=100,
                        convergence_bps=5, round_trip_fee_bps=2,
                        min_profit_bps=2, max_edge_fraction=.25)
    assert quote.budget_bps is None
    assert quote.reason == "SLIPPAGE_BUDGET_TOO_SMALL"


def test_live_samples_use_recent_hour_or_last_fifty():
    model = make_model(min_live_samples=10)
    for i in range(60):
        model.record(venue="entropy", side="buy", now=i * 100,
                     adverse_bps=float(i % 10), decision_budget_bps=20)
    quote = model.quote(venue="entropy", side="buy", now=6000,
                        convergence_bps=100, round_trip_fee_bps=2,
                        min_profit_bps=2, max_edge_fraction=.25)
    assert quote.sample_count == 50
    assert quote.statistical_bps <= 20


def test_three_breaches_halves_size_and_five_pause_entries():
    model = make_model()
    for i in range(3):
        model.record(venue="entropy", side="buy", now=i,
                     adverse_bps=6, decision_budget_bps=5)
    assert model.entry_size_factor("entropy", now=3) == .5
    for i in range(3, 5):
        model.record(venue="entropy", side="buy", now=i,
                     adverse_bps=6, decision_budget_bps=5)
    assert model.entry_paused("entropy", now=5)
    assert not model.entry_paused("entropy", now=905)
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_slippage.py -q
```

Expected: collection fails because `entropy_arb.slippage` does not exist.

- [ ] **Step 3: Implement the bounded model**

Expose:

```python
@dataclass(frozen=True)
class SlippageQuote:
    budget_bps: Optional[float]
    statistical_bps: float
    edge_cap_bps: float
    sample_count: int
    source: str
    reason: str = ""


class SlippageModel:
    def quote(self, *, venue: str, side: str, now: float,
              convergence_bps: float, round_trip_fee_bps: float,
              min_profit_bps: float,
              max_edge_fraction: float) -> SlippageQuote:
        samples = self._selected_samples(venue, side, now)
        statistical = (self.bootstrap_bps if len(samples) < self.min_live_samples
                       else min(max(percentile(samples, .95) + self.safety_bps,
                                    self.min_bps), self.hard_max_bps))
        edge_cap = ((convergence_bps - round_trip_fee_bps
                     - min_profit_bps) * max_edge_fraction)
        budget = min(statistical, edge_cap, self.hard_max_bps)
        if budget < self.min_bps:
            return SlippageQuote(None, statistical, edge_cap, len(samples),
                                 "bootstrap" if not samples else "live",
                                 "SLIPPAGE_BUDGET_TOO_SMALL")
        return SlippageQuote(budget, statistical, edge_cap, len(samples),
                             "bootstrap" if len(samples) < self.min_live_samples
                             else "live")

    def record(self, *, venue: str, side: str, now: float,
               adverse_bps: float, decision_budget_bps: float) -> None:
        self._append_bounded_sample(venue, side, now, adverse_bps)
        self._append_bounded_breach(
            venue, adverse_bps > decision_budget_bps)
        if adverse_bps > self.hard_max_bps or self._breach_count(venue) >= 5:
            self._paused_until[venue] = now + 900.0

    def entry_size_factor(self, venue: str, now: float) -> float:
        return .5 if self._breach_count(venue) >= 3 else 1.0

    def entry_paused(self, venue: str, now: float) -> bool:
        return now < self._paused_until.get(venue, 0.0)
```

Store at most 50 samples per `(venue, side)`, at most 10 breach booleans per venue, and a 900-second pause deadline. Any actual adverse value above `hard_max_bps` pauses immediately. Validate all public numeric inputs as finite.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_slippage.py -q
```

Expected: all slippage tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/slippage.py tests/test_slippage.py
git commit -m "功能：实现动态滑点预算"
```

### Task 5: Add convergence-aware matched-depth planning

**Files:**
- Modify: `entropy_arb/book.py`
- Modify: `tests/test_book.py`

- [ ] **Step 1: Write failing two-direction depth and hard-exit tests**

```python
def test_convergence_plan_stops_before_unprofitable_marginal_level():
    plan, reason = plan_convergence_trade(
        buy=book(asks=[(100, 1), (101, 1)]),
        sell=book(bids=[(99, 2)]), direction="sell_entropy",
        reference_basis_bps=-200, exit_residual_bps=0,
        round_trip_fee_bps=2, close_slippage_reserve_bps=10,
        min_expected_profit_bps=2, buy_slippage_budget_bps=5,
        sell_slippage_budget_bps=5, take_fraction=1,
        cap_notional=500, min_base=.01, min_notional=10,
        size_step=.01)
    assert reason == ""
    assert plan.qty == pytest.approx(1.0)


def test_convergence_plan_rejects_visible_depth_over_budget():
    plan, reason = plan_convergence_trade(
        buy=book(asks=[(100, 1), (101, 1)]),
        sell=book(bids=[(99, 2)]), direction="sell_entropy",
        reference_basis_bps=-200, exit_residual_bps=0,
        round_trip_fee_bps=2, close_slippage_reserve_bps=10,
        min_expected_profit_bps=2, buy_slippage_budget_bps=1,
        sell_slippage_budget_bps=1, take_fraction=1,
        cap_notional=500, min_base=.01, min_notional=10,
        size_step=.01)
    assert plan is None
    assert reason == "DEPTH_SLIPPAGE_EXCEEDED"


def test_forced_close_only_uses_common_depth_inside_hard_limit():
    plan, reason = plan_matched_close(
        buy=buy_book, sell=sell_book, max_qty=2,
        buy_slippage_bps=20, sell_slippage_bps=20,
        min_base=.01, min_notional=10, size_step=.01)
    assert reason == ""
    assert 0 < plan.qty < 2
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_book.py -q
```

Expected: failures because convergence and matched-close planners are absent.

- [ ] **Step 3: Implement marginal matched-depth walkers**

Add a plan type that does not claim the opening cash spread is locked profit:

```python
@dataclass(frozen=True)
class ConvergencePlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    open_depth_slippage_bps: float
    convergence_bps: float
    projected_net_bps: float
```

`plan_convergence_trade` walks matched ask/bid quantities and, for every marginal slice, recomputes direction-specific signed residual, convergence to the frozen exit target, full round-trip fees, visible opening depth slippage and reserved closing slippage. Stop before the first failing marginal slice. Reuse the existing hard-cap helpers so both planned legs remain under `cap_notional` after size-step rounding.

`plan_matched_close` walks both books up to `max_qty`; it limits each price to its per-leg slippage bound and never returns more than the campaign quantity. It does not use an opening-profit threshold.

- [ ] **Step 4: Run book tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_book.py -q
```

Expected: all legacy and new book tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/book.py tests/test_book.py
git commit -m "功能：增加收敛交易深度规划"
```

### Task 6: Implement campaign values and atomic shadow state

**Files:**
- Create: `entropy_arb/campaign.py`
- Create: `tests/test_campaign.py`

- [ ] **Step 1: Write failing lifecycle and store tests**

```python
def test_campaign_transitions_by_wall_clock_and_never_reverses():
    c = campaign_fixture(opened_at=1000, direction="buy_entropy", qty=1)
    assert c.status_at(4599, soft_sec=3600, hard_sec=21600) == "OPEN"
    assert c.status_at(4600, soft_sec=3600, hard_sec=21600) == "SOFT_EXIT"
    assert c.status_at(22600, soft_sec=3600, hard_sec=21600) == "HARD_EXIT"
    with pytest.raises(CampaignInvariantError, match="direction"):
        c.apply_matched_fill(
            intent="ADD", direction="sell_entropy", qty=.1,
            entropy_px=100, hedge_px=100, fees_usd=.01)


def test_atomic_store_round_trip_and_shadow_path(tmp_path):
    store = CampaignStore(str(tmp_path / "campaign-state.json"), shadow=True)
    store.save(campaign_fixture())
    assert store.path.name == "campaign-state.shadow.json"
    assert store.load() == campaign_fixture()
    assert not list(tmp_path.glob("*.tmp"))


def test_store_rejects_corrupt_unknown_or_wrong_identity(tmp_path):
    path = tmp_path / "campaign-state.json"
    path.write_text('{"schema_version":999}', encoding="utf-8")
    with pytest.raises(CampaignStateError, match="schema_version"):
        CampaignStore(str(path), shadow=False).load()
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_campaign.py -q
```

Expected: collection fails because `entropy_arb.campaign` does not exist.

- [ ] **Step 3: Implement immutable campaign updates and atomic JSON**

Expose:

```python
@dataclass(frozen=True)
class PositionCampaign:
    schema_version: int
    campaign_id: str
    mode: str
    identity: MarketIdentity
    direction: str
    opened_at: float
    qty: float
    entropy_avg_px: float
    hedge_avg_px: float
    frozen_model: ModelSnapshot
    entry_boundary_bps: float
    exit_target_bps: float
    fees_usd: float
    realized_pnl_usd: float

    def status_at(self, now: float, *, soft_sec: float,
                  hard_sec: float) -> str:
        age = max(now - self.opened_at, 0.0)
        if age >= hard_sec:
            return "HARD_EXIT"
        if age >= soft_sec:
            return "SOFT_EXIT"
        return "OPEN"

    def apply_matched_fill(self, *, intent: str, qty: float,
                           direction: str,
                           entropy_px: float, hedge_px: float,
                           fees_usd: float) -> Optional["PositionCampaign"]:
        if direction != self.direction:
            raise CampaignInvariantError("fill direction does not match campaign")
        if intent in {"CLOSE", "FORCED_CLOSE"}:
            remaining = self.qty - qty
            if remaining < 0:
                raise CampaignInvariantError("close exceeds campaign quantity")
            return None if remaining == 0 else replace(self, qty=remaining)
        if intent not in {"OPEN", "ADD"}:
            raise CampaignInvariantError("unknown campaign intent")
        total = self.qty + qty
        return replace(
            self, qty=total,
            entropy_avg_px=(self.entropy_avg_px * self.qty
                            + entropy_px * qty) / total,
            hedge_avg_px=(self.hedge_avg_px * self.qty
                          + hedge_px * qty) / total,
            fees_usd=self.fees_usd + fees_usd)


class CampaignStore:
    def load(self) -> Optional[PositionCampaign]:
        if not self.path.exists():
            return None
        return campaign_from_json(self.path.read_text(encoding="utf-8"))

    def save(self, campaign: Optional[PositionCampaign]) -> None:
        payload = campaign_to_json(campaign)
        temp = unique_temp_path_next_to(self.path)
        with temp.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)
```

Write UTF-8 JSON to a unique file in the target directory, flush and `os.fsync`, then `os.replace`. On supported platforms fsync the directory after replacement. Never silently delete or overwrite an invalid state with `None`.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_campaign.py -q
```

Expected: all campaign tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/campaign.py tests/test_campaign.py
git commit -m "功能：持久化套利批次状态"
```

### Task 7: Implement strategy decisions independently of the engine

**Files:**
- Modify: `entropy_arb/strategy.py`
- Modify: `tests/test_strategy.py`

- [ ] **Step 1: Write failing open/add/close/timeout/gate tests**

```python
def test_ready_model_opens_at_side_specific_quantile():
    decision = strategy_with_ready_model().decide(
        market=sell_entropy_market(residual=41), campaign=None, now=1000)
    assert decision.intent == "OPEN"
    assert decision.direction == "sell_entropy"


@pytest.mark.parametrize("reason, market", [
    ("MODEL_NOT_READY", market_with_model_not_ready()),
    ("REFERENCE_INCOMPLETE", market_without_oracle()),
    ("REFERENCE_STALE", market_with_reference_age(15.001)),
    ("REFERENCE_SKEW", market_with_reference_skew(15.001)),
    ("REGIME_UNSTABLE", market_with_unstable_model()),
])
def test_new_risk_fails_closed(reason, market):
    assert strategy().decide(market=market, campaign=None,
                             now=1000).reason == reason


def test_active_campaign_blocks_reverse_and_adds_only_at_frozen_boundary():
    c = buy_campaign(entry_boundary_bps=-20)
    assert strategy().decide(market=buy_market(residual=-19),
                             campaign=c, now=1000).reason == "ENTRY_NOT_REACHED"
    assert strategy().decide(market=buy_market(residual=-21),
                             campaign=c, now=1000).intent == "ADD"


def test_normal_soft_and_hard_exit_priority():
    c = sell_campaign(exit_target_bps=12, opened_at=0)
    assert strategy().decide(market=close_market(residual=11),
                             campaign=c, now=100).intent == "CLOSE"
    assert strategy().decide(market=profitable_close_market(),
                             campaign=c, now=3600).intent == "CLOSE"
    hard = strategy().decide(market=stale_reference_fresh_books(),
                             campaign=c, now=21600)
    assert hard.intent == "FORCED_CLOSE"
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_strategy.py -q
```

Expected: failures because decision types and lifecycle orchestration are absent.

- [ ] **Step 3: Implement pure decision logic**

Add immutable `MarketView`, `StrategyDecision` and `DynamicResidualStrategy`. The decision order must be:

```python
status = (None if campaign is None else campaign.status_at(
    now, soft_sec=self.soft_hold_sec, hard_sec=self.hard_hold_sec))
if campaign is not None and status == "HARD_EXIT":
    return self._forced_close(market=market, campaign=campaign, now=now)
if campaign is not None:
    close = self._normal_or_soft_close(
        market=market, campaign=campaign, now=now)
    if close is not None:
        return close
    if status != "OPEN":
        return skip("SOFT_EXIT_WAITING")
    return self._same_direction_add_or_skip(
        market=market, campaign=campaign, now=now)
return self._gated_open_or_skip(market=market, now=now)
```

The module may call the convergence and matched-close planners but must not access `Engine`, venue adapters, environment variables or files.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_strategy.py tests/test_book.py -q
```

Expected: all decision and planning tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "功能：实现残差批次决策状态机"
```

### Task 8: Add low-frequency strategy event recording and analysis

**Files:**
- Create: `entropy_arb/strategy_recorder.py`
- Create: `tests/test_strategy_recorder.py`
- Modify: `tools/analyze.py`
- Modify: `tests/test_analyze.py`

- [ ] **Step 1: Write failing event coalescing and summary tests**

```python
def test_repeated_skip_is_written_once_per_reason_per_minute(tmp_path):
    recorder = StrategyEventRecorder(tmp_path / "strategy-events.csv")
    recorder.record(skip_event(ts=60.1, reason="REFERENCE_STALE"))
    recorder.record(skip_event(ts=60.9, reason="REFERENCE_STALE"))
    recorder.record(skip_event(ts=120.0, reason="REFERENCE_STALE"))
    recorder.close()
    assert len(read_rows(tmp_path / "strategy-events.csv")) == 2


def test_lifecycle_events_are_never_coalesced(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.record(decision_event("OPEN", ts=1))
    recorder.record(decision_event("CLOSE", ts=2))
    recorder.close()
    assert [row["intent"] for row in read_rows(path)] == ["OPEN", "CLOSE"]


def test_analyze_strategy_events_reports_closed_and_open_campaigns(tmp_path):
    path = write_strategy_events(tmp_path, completed=2, open_count=1,
                                 hold_seconds=[1800, 7200])
    result = run_analyze("--strategy-csv", str(path))
    assert "completed campaigns: 2" in result.stdout
    assert "within 1h: 1" in result.stdout
    assert "within 6h: 2" in result.stdout
    assert "still open: 1" in result.stdout
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_strategy_recorder.py tests/test_analyze.py -q
```

Expected: failures because the recorder and `--strategy-csv` option are absent.

- [ ] **Step 3: Implement stable event rows and optional analysis**

Use one exported `STRATEGY_EVENT_HEADER` containing the fields fixed by the design. Open with UTF-8/newline, verify existing header before append, flush lifecycle events immediately, and make close idempotent. Coalesce only identical `SKIP` reason/campaign/direction tuples within the same UTC minute.

Add to `tools/analyze.py`:

```python
p.add_argument("--strategy-csv",
               help="optional strategy-events.csv to summarize")
```

Parse event timestamps, campaign IDs, intents, modes, hold seconds, projected and realized/shadow PnL as finite values. Reject mixed pair identities. Preserve existing output byte-for-byte when the option is absent.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_strategy_recorder.py tests/test_analyze.py -q
```

Expected: all recorder and analyzer tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/strategy_recorder.py tests/test_strategy_recorder.py tools/analyze.py tests/test_analyze.py
git commit -m "功能：记录并分析策略批次事件"
```

### Task 9: Integrate the full shadow lifecycle

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing zero-order shadow integration tests**

```python
@pytest.mark.asyncio
async def test_residual_record_only_runs_shadow_open_and_close_without_orders(tmp_path):
    eng = make_dynamic_engine(record_only=True, tmp_path=tmp_path)
    seed_ready_residual_model(eng, median=10, lower=-20, upper=40)
    set_market_for_sell_residual(eng, 45)
    await eng._evaluate()
    assert eng.campaign is not None
    assert eng.campaign.mode == "shadow"
    assert eng.entropy.send_calls == []
    assert eng.hedge.send_calls == []
    set_market_for_sell_campaign_close(eng, residual=11)
    await eng._evaluate()
    assert eng.campaign is None
    assert eng.entropy.send_calls == []
    assert eng.hedge.send_calls == []


@pytest.mark.asyncio
async def test_shadow_reference_gate_and_model_not_ready_never_create_campaign():
    eng = make_dynamic_engine(record_only=True)
    assert await evaluate_with_missing_reference(eng) is None
    assert eng.campaign is None


def test_shadow_state_uses_separate_file_and_survives_restart(tmp_path):
    first = make_dynamic_engine(record_only=True, tmp_path=tmp_path)
    first.apply_shadow_decision(open_decision(
        direction="buy_entropy", qty=1,
        entropy_px=100, hedge_px=101))
    second = make_dynamic_engine(record_only=True, tmp_path=tmp_path)
    second.load_shadow_state()
    assert second.campaign.campaign_id == first.campaign.campaign_id
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_engine.py -q
```

Expected: new integration tests fail because record-only currently skips strategy and only runs the signal recorder.

- [ ] **Step 3: Wire model, store, recorder and shadow decisions**

During market setup in residual mode:

```python
self.residual_model = ResidualModel.from_config(cfg)
self.model_warm_start = warm_start_residual_model(
    self.residual_model, path=cfg.recorder_csv,
    identity=current_identity(cfg), now_minute=int(time.time() // 60),
    max_age_sec=cfg.strategy_entry_reference_max_age_sec,
    max_skew_sec=cfg.strategy_entry_reference_max_skew_sec)
self.campaign_store = CampaignStore(cfg.strategy_state_file,
                                    shadow=self.record_only)
self.campaign = self.campaign_store.load()
self.strategy_events = StrategyEventRecorder(cfg.strategy_event_csv)
```

Feed the model with one valid/invalid close at each UTC minute boundary independently of CSV write success ordering. In residual record-only mode, `_evaluate` must call the strategy path rather than returning after signal recording. Apply shadow plans only to `PositionCampaign`; do not change adapter positions, trade counters, cash or live slippage samples.

Track and close the strategy event recorder through the existing fail-fast task supervision. Keep the legacy `SignalRecorder` running for raw comparison.

- [ ] **Step 4: Run focused engine tests and verify GREEN**

Run:

```bash
python -m pytest tests/test_engine.py tests/test_strategy_recorder.py -q
```

Expected: all focused tests pass and shadow tests make zero adapter send calls.

- [ ] **Step 5: Commit Phase A**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "功能：接入动态残差影子策略"
```

## Phase B — live campaign execution and recovery

### Task 10: Reconcile durable campaigns against live positions

**Files:**
- Modify: `entropy_arb/campaign.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_campaign.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing startup reconciliation tests**

```python
def test_reconcile_allows_flat_without_state():
    assert reconcile_campaign(None, entropy_position=0, hedge_position=0,
                              step=.001, net_tolerance=.001).campaign is None


def test_reconcile_resumes_matching_sell_campaign():
    c = sell_campaign(qty=1)
    result = reconcile_campaign(c, entropy_position=-1,
                                hedge_position=1, step=.001,
                                net_tolerance=.001)
    assert result.campaign == c
    assert result.reason == ""


@pytest.mark.parametrize("campaign,e_pos,h_pos", [
    (None, -1, 1),
    (sell_campaign(qty=1), 0, 0),
    (sell_campaign(qty=1), -1, .5),
    (buy_campaign(qty=1), -1, 1),
])
def test_reconcile_fails_closed_on_ambiguous_or_mismatched_state(
        campaign, e_pos, h_pos):
    with pytest.raises(CampaignRecoveryError):
        reconcile_campaign(campaign, entropy_position=e_pos,
                           hedge_position=h_pos, step=.001,
                           net_tolerance=.001)
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_campaign.py tests/test_engine.py -q
```

Expected: failures because live campaign reconciliation is absent.

- [ ] **Step 3: Implement fail-closed startup ordering**

Add `reconcile_campaign` as a pure function. In live residual mode, engine startup order must be:

```python
await self._reconcile_positions(hedge=False, strict=True)
self.campaign = self.campaign_store.load()
recovered = reconcile_campaign(
    self.campaign,
    entropy_position=self.entropy.position,
    hedge_position=self.hedge.position,
    step=self._step,
    net_tolerance=cfg.net_tolerance_base)
self.campaign = recovered.campaign
```

On `CampaignRecoveryError`, enter recovery, log actual versus saved quantities without credentials, and do not start the strategy task. Do not infer a frozen model from current positions.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_campaign.py tests/test_engine.py -q
```

Expected: all campaign recovery tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/campaign.py entropy_arb/engine.py tests/test_campaign.py tests/test_engine.py
git commit -m "功能：校验并恢复实盘套利批次"
```

### Task 11: Apply confirmed live fills to campaign state

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing matched, partial and state-write-failure tests**

```python
@pytest.mark.asyncio
async def test_live_matched_open_fill_persists_campaign_after_trade_audit():
    eng = ready_live_dynamic_engine()
    await execute_open_with_fills(eng, buy_fill=1, sell_fill=1)
    assert eng.campaign.qty == pytest.approx(1)
    assert eng.campaign_store.load().campaign_id == eng.campaign.campaign_id


@pytest.mark.asyncio
async def test_only_matched_fill_changes_campaign_before_residual_hedge():
    eng = ready_live_dynamic_engine()
    await execute_open_with_fills(eng, buy_fill=1, sell_fill=.7)
    assert eng.campaign.qty == pytest.approx(.7)
    assert eng._recovery_required


@pytest.mark.asyncio
async def test_campaign_state_write_failure_pauses_new_entries(caplog):
    eng = ready_live_dynamic_engine()
    eng.campaign_store.save = Mock(side_effect=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        await execute_open_with_fills(eng, buy_fill=1, sell_fill=1)
    assert eng._recovery_required
    assert "campaign state write" in caplog.text
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_engine.py -q
```

Expected: failures because current execution updates venue positions but not campaign state.

- [ ] **Step 3: Integrate intents with the existing execution settlement**

Attach the immutable decision to `_execute_locked`. After both order results are normalized and trade audit succeeds, apply only `matched = min(bfill, sfill)` to the campaign. Preserve the existing unmatched residual recovery. Persist the campaign before another strategy evaluation can run.

For close fills, subtract matched quantity and never cross below zero. When the campaign reaches the common size-step zero, atomically save `None`, emit a completed event, and start cooldown. Unknown outcomes must not be guessed into campaign state; update only after existing confirmation/reconciliation establishes actual fills.

If audit or campaign persistence fails, enter the existing recovery path and propagate the original error with context.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_engine.py -q
```

Expected: all existing settlement/recovery tests and new campaign tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "功能：用实盘成交维护套利批次"
```

### Task 12: Learn real slippage and enforce degradation controls

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing actual-fill slippage tests**

```python
@pytest.mark.asyncio
async def test_actual_adverse_slippage_uses_decision_book_average():
    eng = ready_live_dynamic_engine()
    decision = open_decision(expected_buy_avg=100, buy_budget_bps=5,
                             expected_sell_avg=101, sell_budget_bps=5)
    await execute_decision(eng, decision,
                           buy_result=fill(qty=1, avg_px=100.04),
                           sell_result=fill(qty=1, avg_px=100.96))
    assert eng.slippage.latest("entropy", "buy").adverse_bps == pytest.approx(4)


@pytest.mark.asyncio
async def test_degraded_slippage_halves_only_new_risk_not_close_quantity():
    eng = engine_with_three_recent_breaches()
    assert eng.entry_cap_notional() == pytest.approx(
        eng.cfg.max_order_notional * .5)
    assert eng.close_cap_qty() == eng.campaign.qty


def test_shadow_fill_does_not_add_live_sample():
    eng = ready_shadow_dynamic_engine()
    eng.apply_shadow_decision(open_decision())
    assert eng.slippage.sample_count("entropy", "buy") == 0
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_engine.py tests/test_slippage.py -q
```

Expected: failures because actual fills are not connected to the slippage model.

- [ ] **Step 3: Record actual adverse slippage and apply controls**

For buys use `max((avg_px / expected_avg_px - 1) * 1e4, 0)`; for sells use `max((expected_avg_px / avg_px - 1) * 1e4, 0)`. Record each resolved real fill with the budget frozen in its decision. Apply size factor and pause only to `OPEN`/`ADD`; `CLOSE` and `FORCED_CLOSE` remain available.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_engine.py tests/test_slippage.py -q
```

Expected: all slippage integration tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "功能：按真实成交调整滑点保护"
```

### Task 13: Add deterministic historical shadow replay

**Files:**
- Create: `tools/replay_strategy.py`
- Create: `tests/test_replay_strategy.py`

- [ ] **Step 1: Write failing chronological replay and invariant tests**

```python
def test_replay_merges_gzip_signals_and_minutes_chronologically(tmp_path):
    minutes = write_replay_minutes(tmp_path, residuals=stable_residuals(180))
    first = write_replay_signals_gzip(
        tmp_path / "signals-1.csv.gz", start_ts=1000,
        residuals=[("buy_entropy", 30), ("buy_entropy", 31)])
    second = write_replay_signals_gzip(
        tmp_path / "signals-2.csv.gz", start_ts=2000,
        residuals=[("sell_entropy", 45), ("sell_entropy", 10)])
    result = replay_files(
        minutes_path=minutes, signal_paths=[second, first],
        config=replay_config(), now_ts=3000)
    assert result.signal_rows == 4
    assert result.timestamps_monotonic


def test_replay_reports_raw_coverage_and_residual_campaign_invariants(tmp_path):
    result = replay_fixture_with_raw_buy_always_on(tmp_path)
    assert result.raw_buy_coverage > .95
    assert result.residual_open_coverage < .20
    assert result.reverse_campaigns == 0
    assert result.max_planned_leg_notional <= 500
    assert result.max_slippage_budget_bps <= 20
    assert result.invalid_reference_entries == 0


def test_replay_labels_top_of_book_approximation(tmp_path):
    completed = subprocess.run(
        [sys.executable, "tools/replay_strategy.py", "--minutes",
         str(write_replay_minutes(tmp_path)), "--signals",
         str(write_replay_signals_gzip(tmp_path / "signals.csv.gz"))],
        text=True, capture_output=True, check=True)
    assert "top-of-book approximation" in completed.stdout
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_replay_strategy.py -q
```

Expected: collection fails because `tools/replay_strategy.py` does not exist.

- [ ] **Step 3: Implement a read-only replay tool**

Expose `ReplayResult` and `replay_files`. Read `minutes.csv` with the same strict identity and reference filters as warm start. Read one or more `signals.csv` / `.csv.gz` files, reject mixed identities, deduplicate exact `(ts_ms, direction, event, event_id)` rows, sort by numeric timestamp, and feed recorded top-of-book/reference values into the pure strategy.

Because historical signal files contain top-of-book rather than complete depth snapshots, size each replay plan at the smaller of recorded `planned_notional_usd`, configured single-order cap and available recorded `crossable_notional_usd`. Mark every result and CLI heading as `top-of-book approximation`; do not present replay PnL as an actual fill result.

The CLI accepts:

```text
--minutes PATH
--signals PATH1 PATH2 PATH3
--config PATH
--now-ts UNIX_SECONDS
```

Print raw direction coverage, residual candidate coverage, campaigns opened/completed/still open, normal/soft/hard closes, hold p50/p75/p95/max, maximum planned leg notional, maximum slippage budget, reference-gate rejects and reverse-campaign count.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_replay_strategy.py -q
```

Expected: all replay tests pass.

- [ ] **Step 5: Commit**

```bash
git add tools/replay_strategy.py tests/test_replay_strategy.py
git commit -m "工具：增加动态残差历史回放"
```

### Task 14: Document the shadow-to-live runbook

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Test: `tests/test_requirements.py`

- [ ] **Step 1: Write failing documentation assertions**

Add checks that both READMEs contain the residual mode, the double live gate, event analysis command, campaign mismatch behavior and exact ANTH command:

```python
def test_readmes_document_dynamic_residual_safety_gate():
    for path in ("README.md", "README.zh-CN.md"):
        text = Path(path).read_text(encoding="utf-8")
        assert "strategy.mode" in text
        assert "live_enabled" in text
        assert "--strategy-csv" in text
        assert "campaign-state" in text
        assert "--hedge-symbol ANTHROPIC" in text
```

- [ ] **Step 2: Run and verify RED**

Run:

```bash
python -m pytest tests/test_requirements.py -q
```

Expected: the new documentation test fails.

- [ ] **Step 3: Document exact commands and safety conditions**

Document shadow collection:

```bash
python -u main.py \
  --record-only \
  --symbol ANTH \
  --hedge lighter-rh \
  --hedge-symbol ANTHROPIC \
  --no-dashboard \
  2>&1 | tee -a logs/engine.log
```

Document analysis:

```bash
python tools/analyze.py \
  --csv logs/minutes.csv \
  --strategy-csv logs/strategy-events.csv \
  --entropy-fee-bps 0.9 \
  --hedge-fee-bps 0.0
```

Explain that real orders require all three conditions: `mode=residual_dynamic`, `live_enabled=true`, and absence of `--record-only`. State that implementation completion is not authorization to enable live.

- [ ] **Step 4: Run and verify GREEN**

Run:

```bash
python -m pytest tests/test_requirements.py -q
```

Expected: documentation assertions pass.

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md tests/test_requirements.py
git commit -m "文档：补充动态残差影子运行流程"
```

## Final verification

### Task 15: Run complete tests, compile checks and historical replay

**Files:**
- Verify: all files listed in Tasks 1–14; do not edit unrelated files during this task.

- [ ] **Step 1: Run the full suite**

Run:

```bash
python -m pytest -q
```

Expected: exit 0, zero failed tests.

- [ ] **Step 2: Compile every Python source and test file**

Run:

```bash
python -m compileall -q main.py entropy_arb tools tests
```

Expected: exit 0 and no output.

- [ ] **Step 3: Replay the supplied 43.1-hour dataset in shadow mode**

Run:

```powershell
python tools/replay_strategy.py `
  --minutes 'C:/Users/Administrator/Desktop/entropy-arb/minutes.csv' `
  --signals `
    'C:/Users/Administrator/Desktop/entropy-arb/signals-20260912.csv.gz' `
    'C:/Users/Administrator/Desktop/entropy-arb/signals-20260913.csv.gz' `
    'C:/Users/Administrator/Desktop/entropy-arb/signals-current.csv.gz' `
  --config 'C:/Users/Administrator/Desktop/entropy-arb/config.yaml' `
  --now-ts 1789372800
```

Expected: replay completes without malformed rows; output explicitly says `top-of-book approximation`; residual mode does not reproduce the raw strategy's approximately 98% continuous buy signal; reverse-campaign count and invalid-reference entries are zero; maximum planned leg notional is at most `$500`; maximum slippage budget is at most 20 bps. Record campaign counts and holding-time distribution as evidence, but do not add a profitability or live-ready assertion from only 43.1 hours.

- [ ] **Step 4: Check diff and working tree**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -15
```

Expected: no whitespace errors; only intentionally modified files remain, with no temporary replay output committed.

- [ ] **Step 5: Record verification evidence in the final handoff**

Report exact test count, compile exit status, replay interval, shadow campaign count, completed/soft/hard/open counts, holding-time quantiles, rejected-entry counts, and remaining limitations. Explicitly instruct the user to keep `live_enabled: false` and continue `--record-only` until a new multi-day shadow dataset is reviewed.
