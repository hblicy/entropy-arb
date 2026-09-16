# Final Live Safety Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the six confirmed live-safety and validation defects without changing strategy configuration or pending schema version.

**Architecture:** Keep strategy decisions pure and fix invariants at their owning boundaries: pending validation, book planning, engine sizing/rounding, account locking, and campaign decoding. Each fix receives a focused regression before production code changes.

**Tech Stack:** Python 3, asyncio, pytest, OS file locks, dataclasses.

---

### Task 1: Permit safe close journals with non-ready models

**Files:**
- Modify: `tests/test_recovery_state.py`
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/recovery_state.py`

- [ ] Add tests constructing `CLOSE` and `FORCED_CLOSE` pending states with `REGIME_UNSTABLE` and empty `MODEL_NOT_READY` snapshots; assert `OPEN` and `ADD` still reject them.
- [ ] Add an engine regression proving a hard close with a non-ready model reaches both reduce-only submit calls.
- [ ] Run the focused tests and confirm they fail at the READY-only invariant.
- [ ] Make pending model validation intent-aware while preserving all integer, status, nullable-quantile, ordering, identity and campaign checks.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Recompute exact marginal convergence

**Files:**
- Modify: `tests/test_book.py`
- Modify: `entropy_arb/book.py`

- [ ] Add both-direction tests where the linear slippage approximation accepts a marginal level below `min_expected_profit_bps`.
- [ ] Run the tests and confirm the extra level is incorrectly included.
- [ ] Compute residual/convergence from each marginal ask/bid pair and from the final returned limits.
- [ ] Re-run book tests and confirm both directions stop before the failing level.

### Task 3: Enforce each fixed-strategy venue cap at its own price

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [ ] Add tests for different buy/sell prices with existing long and short positions.
- [ ] Run them and confirm `_headroom` overstates one venue's remaining capacity.
- [ ] Convert both venue caps to base headroom using their own execution prices, take the minimum, and convert to planner buy notional.
- [ ] Re-run the focused scan/headroom tests.

### Task 4: Keep residual hedge bounds inside the configured slippage

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [ ] Add coarse-tick BUY and SELL repair tests capturing submitted limit prices.
- [ ] Run them and confirm current rounding crosses the configured boundary.
- [ ] Round SELL minima upward and BUY maxima downward.
- [ ] Re-run all hedge/recovery tests.

### Task 5: Lock every actual signer across markets and subaccounts

**Files:**
- Modify: `tests/test_live_lock.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_venue_hl.py`
- Modify: `entropy_arb/live_lock.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] Add tests proving the same account id conflicts across markets, overlapping account sets conflict, partial acquisition rolls back, and Hyperliquid lock identity is the signer rather than query address.
- [ ] Run them and confirm the market-composite lock and query address behavior fail.
- [ ] Implement a sorted, deduplicated group of per-account locks with rollback and reverse release; wire it into Engine cleanup.
- [ ] Return the Hyperliquid signer wallet address from `account_lock_id()` and document the single-host boundary plus separate-wallet rule across hosts.
- [ ] Re-run live-lock, engine lifecycle and venue tests.

### Task 6: Normalize campaign overflow errors

**Files:**
- Modify: `tests/test_campaign.py`
- Modify: `entropy_arb/campaign.py`

- [ ] Add constructor and persisted-state tests using an integer too large for `float()`.
- [ ] Run them and confirm raw `OverflowError` escapes.
- [ ] Translate conversion overflow to `CampaignInvariantError`, which the loader wraps as `CampaignStateError`.
- [ ] Re-run campaign and recovery-state tests.

### Task 7: Final verification

**Files:**
- Verify only.

- [ ] Run all tests except no exclusions; distinguish any pre-existing uptime-sensitive failure from changed-code regressions.
- [ ] Run `python -m compileall -q entropy_arb main.py tools tests`.
- [ ] Run `git diff --check` and inspect `git diff --stat` plus `git status --short`.
- [ ] Re-read this plan and the design, confirming every requirement has a regression and implementation.
