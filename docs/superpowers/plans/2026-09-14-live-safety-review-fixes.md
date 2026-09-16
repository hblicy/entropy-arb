# Dynamic Residual Live-Safety Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the reviewed live-trading reversal, reconciliation, slippage, recovery, replay, and process-concurrency gaps while keeping dynamic live disabled by default.

**Architecture:** Tighten the existing engine rather than replace it. Dynamic decisions remain pure; the engine owns execution gates and durable recovery, while small focused stores provide pending-execution persistence and a cross-platform live account lock.

**Tech Stack:** Python 3.10+, asyncio, dataclasses, JSON/CSV standard library, `fcntl`/`msvcrt`, pytest

---

## File map

- Modify `entropy_arb/engine.py`: single-budget protection prices, reduce-only closes, two-leg campaign reconciliation, pending execution journal integration, live lock lifecycle.
- Modify `entropy_arb/campaign.py`: strict position/campaign reconciliation at the common size step.
- Modify `entropy_arb/strategy.py`: insert warm-start tail gaps.
- Create `entropy_arb/recovery_state.py`: validated atomic pending-execution journal.
- Create `entropy_arb/live_lock.py`: non-blocking cross-platform process lock.
- Modify `entropy_arb/venues/base.py`, `entropy_arb/venue_hl.py`, `entropy_arb/venue_lighter.py`: expose non-secret account lock identity.
- Modify `tools/replay_strategy.py`: persistence, cooldown, missing minutes, cumulative headroom, accumulated-notional reporting.
- Modify `main.py`, `README.md`, `README.zh-CN.md`: accurate shadow-mode and live-lock/recovery runbook.
- Modify `tests/test_engine.py`, `tests/test_campaign.py`, `tests/test_strategy.py`, `tests/test_replay_strategy.py`, `tests/test_main.py`.
- Create `tests/test_recovery_state.py`, `tests/test_live_lock.py`.

### Task 1: Enforce one total slippage budget and reduce-only closes

**Files:**
- Modify: `tests/test_book.py`
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/book.py:234-254,429-442`
- Modify: `entropy_arb/engine.py:1876-1920`

- [ ] **Step 1: Write failing order-parameter tests**

Add tests that create a live dynamic engine, execute an `OPEN`, a normal
`CLOSE`, and a `FORCED_CLOSE`, then inspect each venue stub's `send_args`:

```python
def test_dynamic_close_sends_both_legs_reduce_only(tmp_path):
    # Open once, move the books to the frozen exit target, then close.
    # The first call on each venue is OPEN; the second is CLOSE.
    assert eng.entropy.send_args[0]["reduce_only"] is False
    assert eng.hedge.send_args[0]["reduce_only"] is False
    assert eng.entropy.send_args[1]["reduce_only"] is True
    assert eng.hedge.send_args[1]["reduce_only"] is True


def test_dynamic_forced_close_sends_both_legs_reduce_only(tmp_path):
    eng.campaign = replace(
        eng.campaign,
        opened_at=time.time() - eng.cfg.strategy_hard_hold_minutes * 60 - 1,
    )
    await eng._evaluate()
    assert eng.entropy.send_args[-1]["reduce_only"] is True
    assert eng.hedge.send_args[-1]["reduce_only"] is True
```

Add a multi-level book test that asserts the submitted buy/sell protection
prices are no farther from the decision-time best ask/bid than the frozen
budget, including a 20 bps forced close.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/test_engine.py -k "reduce_only or single_total_slippage" -q
```

Expected: close calls omit/false `reduce_only`, and the final limit price
exceeds the single-budget assertion.

- [ ] **Step 3: Implement the minimal execution fix**

Add per-leg `buy_depth_slippage_bps` and `sell_depth_slippage_bps` fields to
`ConvergencePlan`; populate them alongside the existing combined
`open_depth_slippage_bps`. In `_execute`, read depth consumption from
`decision.plan`, derive protection from the unconsumed budget, and set the close
flag explicitly:

```python
reduce_only = (
    decision is not None
    and decision.intent in {"CLOSE", "FORCED_CLOSE"}
)
buy_depth = decision.plan.buy_depth_slippage_bps / 1e4
sell_depth = decision.plan.sell_depth_slippage_bps / 1e4
decision_best_ask = decision.plan.buy_limit / (1 + buy_depth)
decision_best_bid = decision.plan.sell_limit * (1 + sell_depth)
buy_bound = buy.px_round(
    decision_best_ask * (1 + buy_slippage_bps / 1e4), round_up=False)
sell_bound = sell.px_round(
    decision_best_bid / (1 + sell_slippage_bps / 1e4), round_up=True)

settlement = asyncio.gather(
    submit(buy, is_buy=True, qty=plan.qty, limit_px=buy_bound,
           reduce_only=reduce_only),
    submit(sell, is_buy=False, qty=plan.qty, limit_px=sell_bound,
           reduce_only=reduce_only),
    return_exceptions=True,
)
```

When `decision is None`, use zero consumed depth and preserve the existing
fixed-premium protection.

- [ ] **Step 4: Run focused and legacy execution tests**

Run:

```powershell
python -m pytest tests/test_engine.py tests/test_book.py -q
```

Expected: all pass; legacy first-order `reduce_only` assertions remain false.

- [ ] **Step 5: Commit**

```powershell
git add entropy_arb/book.py entropy_arb/engine.py tests/test_book.py tests/test_engine.py
git commit -m "修复：限制动态订单滑点并强制减仓"
```

### Task 2: Reconcile campaign state against both refreshed legs

**Files:**
- Modify: `tests/test_campaign.py`
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/campaign.py:168-205`
- Modify: `entropy_arb/engine.py:2273-2308,2491-2562`

- [ ] **Step 1: Write failing campaign reconciliation tests**

Add tests for the two reviewed failures:

```python
def test_reconcile_rejects_one_whole_step_as_flat():
    with pytest.raises(CampaignRecoveryError):
        reconcile_campaign(
            None,
            entropy_position=0.01,
            hedge_position=-0.01,
            step=0.01,
            net_tolerance=0.001,
        )


def test_periodic_reconcile_blocks_zero_net_campaign_mismatch(tmp_path):
    # Saved sell campaign qty=1, exchange positions were manually reduced
    # to entropy=-0.5 and hedge=+0.5. Net is still zero.
    complete = await eng._reconcile_positions(hedge=True, strict=True)
    assert complete is False
    assert eng._recovery_required
    assert eng._auto_repair_disabled
```

Add the exact partial-close regression: buy close fills `1.0`, sell close fills
`0.7`, residual repair fills `0.3`; after refreshed positions are both zero,
the campaign must be cleared from memory and disk, or the engine must remain
fail-closed. It must never resume with campaign quantity `0.3`.

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/test_campaign.py tests/test_engine.py -k "whole_step or campaign_mismatch or partial_close_residual" -q
```

Expected: the one-step flat mismatch is accepted and the engine resumes from
the stale campaign.

- [ ] **Step 3: Tighten zero tolerance and add an engine invariant helper**

Use a strict sub-step leg tolerance:

```python
leg_tolerance = min(common_step / 2.0, max(tolerance, 1e-12))
```

Add an engine helper that is called only after both venue snapshots complete:

```python
def _reconcile_live_campaign(self) -> bool:
    if self.record_only or self.cfg.strategy_mode != "residual_dynamic":
        return True
    try:
        result = reconcile_campaign(
            self.campaign,
            entropy_position=self.entropy.position,
            hedge_position=self.hedge.position,
            step=self._step,
            net_tolerance=self.cfg.net_tolerance_base,
        )
    except CampaignRecoveryError as exc:
        self._campaign_recovery_blocked = True
        self._auto_repair_disabled = True
        self._pause_for_recovery(str(exc))
        return False
    return True
```

Call it after a complete two-venue refresh and immediately before recovery can
clear `_recovery_required`. A mismatch must return `False`; never infer, resize,
or clear a campaign from positions alone. The partial-close/residual case
therefore remains safely paused unless the durable pending-execution evidence
implemented in Task 5 proves the remaining close.

- [ ] **Step 4: Run focused reconciliation tests**

Run:

```powershell
python -m pytest tests/test_campaign.py tests/test_engine.py -k "campaign or reconcile or residual" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add entropy_arb/campaign.py entropy_arb/engine.py tests/test_campaign.py tests/test_engine.py
git commit -m "修复：按两腿真实仓位校验套利批次"
```

### Task 3: Make warm-start missing minutes fail closed

**Files:**
- Modify: `tests/test_strategy.py`
- Modify: `entropy_arb/strategy.py:259-305`

- [ ] **Step 1: Write failing tail-gap tests**

```python
def test_warm_start_marks_five_minute_tail_gap_unstable(tmp_path):
    model = make_model()
    write_valid_history_ending_at(tmp_path, minute=194, rows=180)
    warm_start_residual_model(
        model,
        path=str(tmp_path / "minutes.csv"),
        identity=identity(),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )
    assert model.snapshot(now_minute=200).status == "REGIME_UNSTABLE"
```

Also test a one-minute gap does not immediately mark an otherwise stable model
unstable.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m pytest tests/test_strategy.py -k "warm_start and tail_gap" -q
```

Expected: the five-minute case incorrectly returns `READY`.

- [ ] **Step 3: Insert historical gaps deterministically**

After sorting accepted rows, insert `valid=False` observations between distinct
minutes and through `now_minute - 1`:

```python
previous = None
for minute, residual in sorted(accepted_rows):
    if previous is not None:
        for missing in range(previous + 1, minute):
            model.observe(minute=missing, residual_bps=None, valid=False)
    model.observe(minute=minute, residual_bps=residual, valid=True)
    previous = minute
if previous is not None:
    for missing in range(previous + 1, now_minute):
        model.observe(minute=missing, residual_bps=None, valid=False)
```

Keep accepted/rejected CSV counters tied to real rows, not synthesized gaps.

- [ ] **Step 4: Run strategy tests and commit**

```powershell
python -m pytest tests/test_strategy.py -q
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "修复：预热模型时补齐缺失分钟"
```

### Task 4: Match replay to engine persistence, cooldown, gaps, and caps

**Files:**
- Modify: `tests/test_replay_strategy.py`
- Modify: `tools/replay_strategy.py:231-470`

- [ ] **Step 1: Write failing replay parity tests**

Add four deterministic fixtures:

```python
def test_replay_requires_entry_persistence(tmp_path):
    result = replay_with_edge_shorter_than(tmp_path, persist_sec=3)
    assert result.campaigns_opened == 0


def test_replay_applies_action_cooldown(tmp_path):
    result = replay_with_repeated_adds(tmp_path, cooldown_sec=60)
    assert result.actions == 1


def test_replay_never_exceeds_accumulated_position_cap(tmp_path):
    result = replay_with_continuous_adds(tmp_path, venue_cap=1000)
    assert result.max_accumulated_leg_notional <= 1000.0 + 1e-6


def test_replay_inserts_missing_minutes(tmp_path):
    result = replay_across_five_minute_gap(tmp_path)
    assert result.entries_during_unstable_gap == 0
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m pytest tests/test_replay_strategy.py -k "persistence or cooldown or accumulated or missing" -q
```

Expected: repeated signals open/add immediately, accumulated notional can exceed
the venue cap, and the gap does not lock the model unstable.

- [ ] **Step 3: Add replay execution-gate state**

Track `armed_since`, `last_action_ts`, and previous minute. Before applying an
`OPEN`/`ADD`, require continuous direction and elapsed persistence; after any
applied action, enforce cooldown for new risk. Insert invalid model observations
for missing minutes using the same loop as the engine.

Calculate simulated positions from the campaign and price the remaining room
with the current mids:

```python
def _entry_headroom(config, campaign, direction, entropy_mid, hedge_mid):
    sign = 0.0
    qty = 0.0
    if campaign is not None:
        sign = -1.0 if campaign.direction == "sell_entropy" else 1.0
        qty = campaign.qty
    entropy_position = sign * qty
    hedge_position = -sign * qty
    if direction == "sell_entropy":
        entropy_room = config.entropy.cap_usd + entropy_position * entropy_mid
        hedge_room = config.hedge.cap_usd - hedge_position * hedge_mid
    else:
        entropy_room = config.entropy.cap_usd - entropy_position * entropy_mid
        hedge_room = config.hedge.cap_usd + hedge_position * hedge_mid
    return max(0.0, min(config.max_order_notional,
                        entropy_room, hedge_room))
```

Pass the reduced cap into `_signal_market`. Extend `ReplayResult` and CLI output
with `actions`, `max_accumulated_leg_notional`, and
`entries_during_unstable_gap`.

- [ ] **Step 4: Run replay tests and the supplied replay command**

```powershell
python -m pytest tests/test_replay_strategy.py -q
python tools/replay_strategy.py --minutes "C:/Users/Administrator/Desktop/entropy-arb/minutes.csv" --signals "C:/Users/Administrator/Desktop/entropy-arb/signals-20260912.csv.gz" "C:/Users/Administrator/Desktop/entropy-arb/signals-20260913.csv.gz" "C:/Users/Administrator/Desktop/entropy-arb/signals-current.csv.gz" --config "C:/Users/Administrator/Desktop/entropy-arb/config.yaml" --now-ts 1789372800
```

Expected: tests pass; replay remains labeled top-of-book approximation; maximum
single order is at most `$500`, accumulated leg notional at most `$1000`, no
invalid-reference entry, and no reverse campaign.

- [ ] **Step 5: Commit**

```powershell
git add tools/replay_strategy.py tests/test_replay_strategy.py
git commit -m "修复：让历史回放遵守执行层风控"
```

### Task 5: Persist pending dynamic execution evidence

**Files:**
- Create: `entropy_arb/recovery_state.py`
- Create: `tests/test_recovery_state.py`
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py:85-104,228-253,1911-1980,2371-2489`

- [ ] **Step 1: Write failing journal validation and atomicity tests**

Define the wished-for API in tests:

```python
def test_pending_store_round_trips_without_secrets(tmp_path):
    store = PendingExecutionStore(tmp_path / "campaign.pending.json")
    expected = pending_execution_fixture()
    store.save(expected)
    assert store.load() == expected
    payload = (tmp_path / "campaign.pending.json").read_text("utf-8")
    assert "private" not in payload.lower()
    assert "api_key" not in payload.lower()


def test_pending_store_rejects_incompatible_or_nonfinite_state(tmp_path):
    write_invalid_pending_state(tmp_path)
    with pytest.raises(PendingExecutionStateError):
        PendingExecutionStore(path).load()
```

Test that `save(None)` atomically records an empty journal and leaves no temp
file after replace failure.

- [ ] **Step 2: Run store tests and verify RED**

```powershell
python -m pytest tests/test_recovery_state.py -q
```

Expected: import/collection fails because the module does not exist.

- [ ] **Step 3: Implement the validated journal**

Create immutable `PendingLegState` and `PendingExecutionState` dataclasses.
Use a strict schema envelope and `NamedTemporaryFile(..., delete=False)` plus
`os.replace`, following `CampaignStore`. Fields are limited to:

```python
@dataclass(frozen=True)
class PendingLegState:
    venue_key: str
    is_buy: bool
    order_ref: Optional[str]
    status: str
    filled_base: float
    avg_px: Optional[float]
    applied_fill: float


@dataclass(frozen=True)
class PendingExecutionState:
    execution_id: str
    identity: MarketIdentity
    intent: str
    direction: str
    campaign_id: Optional[str]
    qty: float
    entropy_expected_px: float
    hedge_expected_px: float
    entropy_fee_bps: float
    hedge_fee_bps: float
    opened_at: Optional[float]
    frozen_model: Optional[ModelSnapshot]
    entry_boundary_bps: Optional[float]
    exit_target_bps: Optional[float]
    buy: PendingLegState
    sell: PendingLegState
    audit_ok: bool
    campaign_applied: bool
```

Reject unknown fields, wrong identity, invalid intent/direction combinations,
non-finite numbers, negative fills, or `applied_fill > filled_base`.

- [ ] **Step 4: Write failing engine crash-window tests**

```python
def test_dynamic_execution_journal_exists_before_send(tmp_path):
    await eng._evaluate()
    assert venue.saw_pending_journal_before_send


def test_startup_pending_without_refs_fails_closed(tmp_path):
    PendingExecutionStore(path).save(pre_send_state_without_refs())
    await start_engine(eng)
    assert eng._recovery_required
    assert eng._auto_repair_disabled
    assert no_orders_sent(eng)


def test_startup_resolves_persisted_unknown_once(tmp_path):
    PendingExecutionStore(path).save(persisted_unknown_with_refs())
    await start_and_recover(eng)
    assert eng.campaign.qty == pytest.approx(expected_qty)
    assert eng.pending_execution_store.load() is None
    await eng._recover_positions(strict=True)
    assert eng.campaign.qty == pytest.approx(expected_qty)
```

- [ ] **Step 5: Run engine journal tests and verify RED**

```powershell
python -m pytest tests/test_engine.py -k "journal or persisted_unknown" -q
```

Expected: no journal exists and startup does not know the persisted references.

- [ ] **Step 6: Integrate fail-closed journal transitions**

Derive the journal path from `strategy_state_file` by inserting `.pending`
before the suffix. Persist a pre-send state before `asyncio.gather`; update it
after each returned or unresolved result and after audit success. Clear it only
after campaign state is durably saved and complete position reconciliation
succeeds.

At startup:

- a journal with both resolvable refs rebuilds pending confirmations and uses
  the existing `resolve_order` path;
- a terminal journal applies only `filled_base - applied_fill` and respects
  `campaign_applied` for idempotency;
- a pre-send/ambiguous journal without refs sets manual recovery, keeps the
  journal, and sends no order.

- [ ] **Step 7: Run recovery tests and commit**

```powershell
python -m pytest tests/test_recovery_state.py tests/test_engine.py -k "pending or unknown or recovery or journal" -q
git add entropy_arb/recovery_state.py entropy_arb/engine.py tests/test_recovery_state.py tests/test_engine.py
git commit -m "修复：持久化动态订单恢复状态"
```

### Task 6: Reject concurrent live processes for the same accounts and market

**Files:**
- Create: `entropy_arb/live_lock.py`
- Create: `tests/test_live_lock.py`
- Modify: `entropy_arb/venues/base.py:31-65`
- Modify: `entropy_arb/venue_hl.py:320-332`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/engine.py:110-198,1250-1360`

- [ ] **Step 1: Write failing lock tests**

```python
def test_second_live_lock_for_same_identity_is_rejected(tmp_path):
    first = LiveProcessLock(lock_identity(), directory=tmp_path)
    second = LiveProcessLock(lock_identity(), directory=tmp_path)
    first.acquire()
    with pytest.raises(LiveProcessLockError, match="already running"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_different_account_or_market_has_different_lock(tmp_path):
    first = LiveProcessLock(lock_identity(account="1"), directory=tmp_path)
    second = LiveProcessLock(lock_identity(account="2"), directory=tmp_path)
    first.acquire()
    second.acquire()
    second.release()
    first.release()
```

Add an engine lifecycle test proving record-only takes no lock and live lock
contention fails before either venue can send.

- [ ] **Step 2: Run and verify RED**

```powershell
python -m pytest tests/test_live_lock.py tests/test_engine.py -k "live_lock or process_lock" -q
```

Expected: module/API is absent.

- [ ] **Step 3: Implement the OS lock and account identity**

Add `account_lock_id()` to `VenueAdapter`. Hyperliquid returns the initialized
query address; Lighter returns `chain_id:account_index`. Hash this tuple with the
market identity and write only the digest/PID to the lock file.

`LiveProcessLock.acquire()` opens one file and takes a non-blocking exclusive
lock using `fcntl.flock(..., LOCK_EX | LOCK_NB)` on Linux or
`msvcrt.locking(..., LK_NBLCK, 1)` on Windows. Keep the file handle for the
engine lifetime and always release it during cleanup. Do not delete a lock file
to break contention.

- [ ] **Step 4: Integrate before live task startup**

After signers expose their non-secret account identities but before feed or
strategy tasks can submit orders:

```python
if not self.record_only:
    self._live_lock = LiveProcessLock.from_market(
        self._market_identity(),
        self.entropy.account_lock_id(),
        self.hedge.account_lock_id(),
    )
    self._live_lock.acquire()
```

Release in the same `finally` block that closes venue/session resources.

- [ ] **Step 5: Run tests and commit**

```powershell
python -m pytest tests/test_live_lock.py tests/test_engine.py tests/test_venue_hl.py tests/test_venue_lighter.py -q
git add entropy_arb/live_lock.py entropy_arb/venues/base.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py entropy_arb/engine.py tests/test_live_lock.py tests/test_engine.py tests/test_venue_hl.py tests/test_venue_lighter.py
git commit -m "修复：阻止相同账户交易对重复启动"
```

### Task 7: Correct CLI and recovery runbook text

**Files:**
- Modify: `tests/test_main.py`
- Modify: `main.py:207-209`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Write the failing CLI help assertion**

```python
def test_record_only_help_mentions_residual_shadow_strategy():
    completed = subprocess.run(
        [sys.executable, "main.py", "--help"],
        text=True, capture_output=True, check=True,
    )
    assert "shadow strategy" in completed.stdout
    assert "send no orders" in completed.stdout
```

- [ ] **Step 2: Run and verify RED**

```powershell
python -m pytest tests/test_main.py -k record_only_help -q
```

Expected: help still says no strategy runs.

- [ ] **Step 3: Update only affected operational documentation**

Set the help text to:

```python
help=("collect data and, in residual_dynamic mode, run the shadow "
      "strategy; send no orders (needs no credentials)")
```

Document that live startup is single-instance per account/market, ambiguous
pending execution fails closed, and `live_enabled` must remain false until a
fresh shadow dataset is reviewed.

- [ ] **Step 4: Run docs-adjacent tests and commit**

```powershell
python -m pytest tests/test_main.py tests/test_config.py -q
git add main.py README.md README.zh-CN.md tests/test_main.py
git commit -m "文档：说明动态影子与实盘恢复约束"
```

### Task 8: Complete verification and readiness handoff

**Files:**
- Verify all files modified in Tasks 1-7.

- [ ] **Step 1: Run the complete suite without persistent caches**

```powershell
python -B -m pytest -q -p no:cacheprovider
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Compile every Python source file**

```powershell
python -m compileall -q entropy_arb main.py tools tests
```

Expected: exit code 0.

- [ ] **Step 3: Re-run supplied historical replay**

```powershell
python tools/replay_strategy.py --minutes "C:/Users/Administrator/Desktop/entropy-arb/minutes.csv" --signals "C:/Users/Administrator/Desktop/entropy-arb/signals-20260912.csv.gz" "C:/Users/Administrator/Desktop/entropy-arb/signals-20260913.csv.gz" "C:/Users/Administrator/Desktop/entropy-arb/signals-current.csv.gz" --config "C:/Users/Administrator/Desktop/entropy-arb/config.yaml" --now-ts 1789372800
```

Expected: top-of-book warning remains present; no reverse campaign or invalid
reference entry; single-order and accumulated-position caps hold. Record the new
campaign counts and hold distribution rather than comparing them to the flawed
pre-fix replay counts.

- [ ] **Step 4: Inspect repository state**

```powershell
git diff --check 553a859..HEAD
git status --short
git log --oneline --decorate -12
```

Expected: no whitespace errors or uncommitted implementation artifacts.

- [ ] **Step 5: Keep live disabled and report exact evidence**

Do not change `strategy.live_enabled` to true. Report exact test count, compile
status, replay interval and invariants, commit range, and any remaining manual
recovery limitations. Require another multi-day `--record-only` run before a
separate live-readiness review.
