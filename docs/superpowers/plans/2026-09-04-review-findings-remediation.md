# Review Findings Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every confirmed execution-safety and CSV-analysis defect from the final branch review.

**Architecture:** Keep the current two-venue engine and normalized adapter boundary. Add one explicit recovery gate around existing reconciliation, tighten adapter/result classification, and repair data at the recorder/analyzer boundaries.

**Tech Stack:** Python 3, asyncio, pytest, CSV, argparse, PyYAML

---

### Task 1: Freeze trading while an order outcome is unresolved

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Write failing tests** proving `_scan()` returns `None` during recovery, successful strict reconciliation clears recovery, and unexpected strategy/execution exceptions propagate.
- [x] **Step 2: Run:** `python -m pytest -q tests/test_engine.py -p no:cacheprovider` and confirm the new tests fail for the missing recovery gate and swallowed exceptions.
- [x] **Step 3: Implement minimal recovery state:**

```python
def _enter_recovery(self) -> None:
    self._recovery_required = True
    self._shutdown_reconcile_required = True
    self._reconcile_evt.set()
```

Block `_scan()` while recovery is active, return completion from reconciliation,
clear recovery only after strict position reads and a net position within
`net_tolerance_base`, and re-raise non-cancellation strategy/execution errors.
- [x] **Step 4: Re-run `tests/test_engine.py` and confirm green.**
- [ ] **Step 5: Commit with Chinese message.**

### Task 2: Preserve uncertainty at venue boundaries

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/test_models.py`
- Modify: venue adapter tests covering Hyperliquid and Lighter
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/models.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`

- [x] **Step 1: Write failing tests** for raised leg exceptions, unknown accepted
Hyperliquid statuses, unknown/empty Lighter statuses, non-finite normalized
values, and Lighter close error propagation.
- [x] **Step 2: Run the targeted tests and verify expected failures.**
- [x] **Step 3: Implement minimal classifications:**

```python
if isinstance(result, BaseException):
    results.append(OrderResult.unknown(f"adapter exception: {result!r}"))
```

Use `OrderResult.unknown()` for accepted-but-unrecognised states, require known
Lighter terminal statuses, use `math.isfinite()` in `OrderResult`, and let SDK
close errors propagate.
- [x] **Step 4: Re-run targeted tests and confirm green.**
- [ ] **Step 5: Commit with Chinese message.**

### Task 3: Validate active output paths

**Files:**
- Modify: `tests/test_config.py`
- Modify: `entropy_arb/config.py`

- [x] **Step 1: Write failing tests** for live minute/trade, live log/trade,
and normalized/hardlink collisions while retaining the inactive live signal
path behavior.
- [x] **Step 2: Run `tests/test_config.py` and verify failures.**
- [x] **Step 3: Build the active output tuple by run mode and apply the existing
`_same_output_file()` pairwise validation.**
- [x] **Step 4: Re-run config tests and confirm green.**
- [ ] **Step 5: Commit with Chinese message.**

### Task 4: Repair minute aggregation and fee CLI validation

**Files:**
- Modify: `tests/test_analyze.py`
- Modify: `tools/analyze.py`

- [x] **Step 1: Write failing tests** for duplicate minute fragments, weighted
means, post-merge minimum samples, sorted output, and one-sided exact fee flags.
- [x] **Step 2: Run `tests/test_analyze.py` and verify failures.**
- [x] **Step 3: Parse sample counts, merge by market identity and timestamp,
retain the final close, take both maxima, compute the weighted mean, then filter
and sort. Require exact fee arguments as a pair before reading input.**
- [x] **Step 4: Re-run analyzer tests and confirm green.**
- [ ] **Step 5: Commit with Chinese message.**

### Task 5: Prevent appending after a torn CSV tail

**Files:**
- Modify: `tests/test_recorder.py`
- Modify: `entropy_arb/recorder.py`

- [x] **Step 1: Write failing minute and signal tests** using a valid header
followed by an unterminated or wrong-column tail; assert the damaged file is
preserved and the new file starts with one valid header and row.
- [x] **Step 2: Run the targeted recorder tests and verify failures.**
- [x] **Step 3: Add a shared tail validator using binary newline detection and
`csv.reader`; rotate schema-compatible files whose final record is incomplete
or structurally invalid.**
- [x] **Step 4: Re-run recorder tests and confirm green.**
- [ ] **Step 5: Commit with Chinese message.**

### Task 6: Full verification

**Files:**
- Review all modified files

- [x] **Step 1: Run:** `python -m pytest -q -p no:cacheprovider -W error`.
- [x] **Step 2: Run:** `python -m compileall -q entropy_arb tools main.py`.
- [x] **Step 3: Run:** `git diff --check` and inspect `git status --short`.
- [x] **Step 4: Review the diff against every design requirement and report any remaining limitation.**

### Task 7: Final lifecycle and path-conflict review fixes

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/test_config.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/config.py`

- [x] **Step 1: Write failing tests** for malformed dual-leg results observing
the settlement grace period, incomplete position snapshots never starting a
hedge, shutdown completing after a known pre-submit repair failure, and active
output paths that are ancestors or descendants of one another.
- [x] **Step 2: Run the five focused tests and verify the expected failures.**
- [x] **Step 3: Timestamp both legs before normalized result validation, split
strict recovery into complete fetch and repair phases, only repair complete
snapshots, and reject ancestor/descendant output paths.**
- [x] **Step 4: Run engine/config regression tests and the complete suite.**
- [x] **Step 5: Obtain independent read-only execution, lifecycle, and data-path
reviews.**
- [x] **Step 6: Make shutdown retry backoff interruptible by a successful
background recovery and cover the lock-handoff race with a controlled test.**

### Task 8: Close remaining execution-safety gaps

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Write failing tests** proving strict recovery rejects `NaN` and
infinite positions, runtime cancellation of tracked tasks is supervised,
execution cancellation enters unknown recovery, matched cancelled partial fills
are not successful, and a fatal hedge adapter result cannot trigger repeated
repair submissions during shutdown.
- [x] **Step 2: Run the focused tests and verify each fails for its reviewed
production behavior.**
- [x] **Step 3: Implement the minimal state changes:** normalize and validate
positions inside the existing fetch error boundary; distinguish cleanup task
cancellation from runtime cancellation; require `filled` on both legs; and set
an internal fatal-execution gate that permits strict reconciliation but skips
subsequent automatic repair.
- [x] **Step 4: Re-run the focused Engine tests and the complete Engine suite.**

### Task 9: Preserve startup and audit resources

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/test_venue_contract.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/venue_lighter.py`

- [x] **Step 1: Write failing tests** proving a failed Lighter credential check
leaves its constructed signer reachable for cleanup, a torn trades row rotates
before append, and session-close failure cannot replace an earlier engine error.
- [x] **Step 2: Verify the focused tests fail on the reviewed paths.**
- [x] **Step 3: Register the Lighter signer before checking it, reuse the CSV
header/tail validator for trades, and move shared-session close into shielded
engine cleanup with first-error preservation.**
- [x] **Step 4: Re-run focused venue/lifecycle/audit tests.**

### Task 10: Enforce data-source invariants before filtering

**Files:**
- Modify: `tests/test_analyze.py`
- Modify: `tests/test_recorder.py`
- Modify: `tools/analyze.py`
- Modify: `entropy_arb/recorder.py`

- [x] **Step 1: Write failing tests** for a second market hidden by
`min_samples`, a second market hidden by `hours`, and valid headers followed by
invalid UTF-8 tails in both minute and signal files.
- [x] **Step 2: Verify all tests fail for the reviewed reasons.**
- [x] **Step 3: Collect market identity from every parseable row before any
filter and validate it before returning; read CSV headers as a single binary
physical line, rotate decode failures, and write all CSVs explicitly as UTF-8.**
- [x] **Step 4: Re-run analyzer and recorder suites.**

### Task 11: Documentation and final verification

**Files:**
- Modify: `README.zh-CN.md`
- Modify: this plan and its design document

- [x] **Step 1: Synchronize the Chinese documentation for safe tail rotation,
restart-fragment merging, and paired exact-fee flags.**
- [x] **Step 2: Run `python -m pytest -q -p no:cacheprovider -W error`.**
- [x] **Step 3: Run `python -m compileall -q entropy_arb tools main.py` and
`git diff --check`.**
- [x] **Step 4: Obtain independent read-only execution and data reviews, then
resolve every Critical or Important finding before merge.**

### Task 12: Resolve final-review regressions

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/test_recorder.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/recorder.py`

- [x] **Step 1: Add failing regressions** for filesystem errors being mistaken
for corrupt CSV content and for a cancelled recovery hedge aborting shutdown
drain before its new unknown outcome is reconciled.
- [x] **Step 2: Propagate filesystem errors without rotation and distinguish a
cancelled shield child from cancellation of its awaiting caller.**
- [x] **Step 3: Re-run the focused regressions.**
- [x] **Step 4: Re-run complete verification and obtain final independent
read-only review of both fixes.**

### Task 13: Preserve Lighter terminal identity across adapter exceptions

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `tests/test_venue_contract.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/venue_lighter.py`

- [x] **Step 1: Add failing regressions** where a Lighter WS terminal payload
causes result validation to fail after submission and where an adapter exception
carries a known order reference.
- [x] **Step 2: Verify the focused tests fail because Engine loses the
`client_order_index`.**
- [x] **Step 3: Normalize malformed Lighter terminal data to
`OrderResult.unknown(..., order_ref=...)`; keep no-reference post-submit
unknowns paused for manual recovery instead of accepting an unversioned
snapshot; shield paired bounded adapter calls so cancellation cannot discard
their order references.**
- [x] **Step 4: Re-run the focused venue and Engine tests.**

### Task 14: Consume the audit repair grant exactly once

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a failing regression** where audit writing fails, the first
reduce-only repair is unresolved, and its terminal status later becomes
cancelled; assert no second repair order is submitted.
- [x] **Step 2: Verify the regression reports one extra submission.**
- [x] **Step 3: Consume the audit-repair grant before the first `_maybe_hedge()`
call and keep later recovery limited to terminal confirmation and position
reads.**
- [x] **Step 4: Re-run Engine tests.**

### Task 15: Validate actual output targets and all CSV identities

**Files:**
- Modify: `tests/test_config.py`
- Modify: `entropy_arb/config.py`

- [x] **Step 1: Add failing tests** for illegal Windows characters in a leaf
and missing parent component, reserved intermediate components, and control
characters in `entropy.dex`.
- [x] **Step 2: Verify the new configuration tests fail.**
- [x] **Step 3: Share one identity validator, validate every Windows component,
create missing parents during startup preflight, and test the exact target by
exclusive creation/removal when it does not yet exist.**
- [x] **Step 4: Re-run configuration tests.**

### Task 16: Preserve Dashboard failure evidence and bounded shutdown semantics

**Files:**
- Modify: `tests/test_main.py`
- Modify: `main.py`
- Modify: `entropy_arb/dashboard.py`

- [x] **Step 1: Add failing tests** for simultaneous Engine/Dashboard failures
and for cancellation reaching a bounded Dashboard cleanup without suppression.
- [x] **Step 2: Verify the first loses the secondary traceback and the second
exposes any unbounded Dashboard cleanup.**
- [x] **Step 3: Log the secondary Dashboard exception with `exc_info`; use a
bounded application runner that closes the loop after reporting any
non-cooperative auxiliary task.**
- [x] **Step 4: Re-run main and Dashboard tests.**

### Task 17: Final verification

**Files:**
- Review all files changed by Tasks 13-16

- [x] **Step 1: Run `python -m pytest -q -p no:cacheprovider -W error`.**
- [x] **Step 2: Run `python -m compileall -q entropy_arb tools main.py tests`.**
- [x] **Step 3: Run `git diff --check HEAD` and inspect `git status --short`.**
- [x] **Step 4: Review every new test against the six accepted findings and
report any remaining operational limitation.**

### Task 18: Stop automatic repair after residual-hedge adapter exceptions

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a regression** that makes a residual hedge raise after a
possible write and asserts strict recovery cannot submit a second order.
- [x] **Step 2: Run the focused test and verify the extra submission or
recovery-loop behavior fails the assertion.**
- [x] **Step 3: Mark the outcome unreferenced, disable automatic repair,
preserve the original exception, and stop without snapshot-based resubmission.**
- [x] **Step 4: Re-run the focused Engine tests.**

### Task 19: Strengthen Lighter order identity and terminal convergence

**Files:**
- Modify: `tests/test_venue_contract.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/venues/base.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add regressions** for distinct API-key namespaces, retained
unknown terminal entries, and authenticated exact-order lookup on cache miss.
- [x] **Step 2: Run the focused tests and verify collisions/eviction/miss
handling fail.**
- [x] **Step 3: Allocate uint48 client indexes from API-key namespace plus a
monotonic counter; retain pending terminal entries and make order resolution
asynchronous with REST fallback.**
- [x] **Step 4: Re-run venue-contract and Engine recovery tests.**

### Task 20: Reject fills without execution prices

**Files:**
- Modify: `tests/test_models.py`
- Modify: `tests/test_venue_contract.py`
- Modify: `entropy_arb/models.py`
- Modify: `entropy_arb/venue_hl.py`

- [x] **Step 1: Add regressions** for positive fills without an average price
and malformed Hyperliquid filled responses.
- [x] **Step 2: Run the focused tests and verify both are currently accepted.**
- [x] **Step 3: Enforce the normalized fill invariant and convert malformed
Hyperliquid payloads to an unreferenced unknown result.**
- [x] **Step 4: Re-run model, venue, and Engine execution tests.**

### Task 21: Refuse unversioned stale Lighter position mismatches

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a regression** where only the Lighter REST leg returns a
post-trade stale position and assert no automatic hedge is sent.
- [x] **Step 2: Run it and verify the stale value currently creates a hedge.**
- [x] **Step 3: Treat a post-trade Lighter mismatch as incomplete recovery,
pause entries, retain local state, and retry without submitting an order.**
- [x] **Step 4: Re-run reconciliation tests.**

### Task 22: Bound non-order resource cleanup

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a regression** with one blocked venue close and one normal
close, asserting cleanup proceeds to the normal venue and HTTP session.
- [x] **Step 2: Run it and verify cleanup currently exceeds the deadline.**
- [x] **Step 3: Add a bounded per-resource close helper while leaving order
drain unbounded.**
- [x] **Step 4: Re-run lifecycle and main shutdown tests.**

### Task 23: Align analyzer and operator documentation

**Files:**
- Modify: `tests/test_analyze.py`
- Modify: `tools/analyze.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `entropy_arb/venue_hl.py`

- [x] **Step 1: Add regressions** proving invalid metrics cannot hide a second
market identity.
- [x] **Step 2: Run them and verify the mixed file is currently accepted.**
- [x] **Step 3: Validate identity before metric filtering and document manual
recovery for unreferenced Hyperliquid outcomes.**
- [x] **Step 4: Re-run analyzer tests.**

### Task 24: Final follow-up verification

**Files:**
- Review all files changed by Tasks 18-23

- [x] **Step 1: Run all focused regression groups.**
- [x] **Step 2: Run `python -m pytest -q -p no:cacheprovider -W error`.**
- [x] **Step 3: Run `python -m compileall -q entropy_arb tools main.py tests`.**
- [x] **Step 4: Run `git diff --check HEAD`, inspect status, and perform a
read-only final review of the resulting execution and recovery paths.**

### Task 25: Preserve post-submission books across terminal reordering

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a regression** that publishes a fresh book after order
submission but before the unresolved order reaches terminal status, provides
no later book update, and asserts shutdown drain submits the reduce-only hedge.
- [x] **Step 2: Run the focused test and verify it times out because
`last_traded_ts` advances to terminal-consumption time.**
- [x] **Step 3: Record the submission timestamp for executions that may need
residual repair, use it only as `_hedge()`'s book cutoff, and clear it after the
net position returns within tolerance.**
- [x] **Step 4: Re-run both terminal/book ordering regressions repeatedly, then
run the Engine suite and strict full suite.**

### Task 26: Tighten submission causality and pending-order backoff

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Add a controlled regression** that queues a book callback
before venue submission tasks run and proves the resulting book remains
ineligible until a truly post-submission update arrives.
- [x] **Step 2: Capture one cutoff inside each venue submission wrapper rather
than before `asyncio.gather` schedules the wrappers.**
- [x] **Step 3: Extend the pending-order backoff regression with continuous
book notifications and make shutdown ignore generic live progress until the
pending lookup interval expires.**
- [x] **Step 4: Repeat all ordering/backoff regressions, run the Engine suite,
then run strict full verification and diff checks.**

### Task 27: Preserve recovery phase and route progress by source

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/venues/base.py`

- [x] **Step 1: Add three failing regressions** proving that shutdown retains
feeds for a known residual waiting on a post-submission book, an account-order
terminal notification immediately wakes a pending lookup, and a resolved
pending order remains in post-order recovery without full REST position reads
or wakeups from another venue's book.
- [x] **Step 2: Run only those regressions** and confirm they fail respectively
because shutdown recovery is not armed, terminal and book notifications share
one event, and recovery phase is inferred from the transient pending list.
- [x] **Step 3: Implement source-aware progress routing** by tagging adapter
book/account callbacks with venue identity, waiting separately for order
terminal progress and eligible residual-book progress, treating the residual
state as an explicit persistent post-order recovery phase, and arming shutdown only
while an unattempted residual is blocked on its causal book.
- [x] **Step 4: Run the regressions, all recovery-ordering/backoff tests,
the Engine suite, then the strict full suite, compileall, and diff checks.**

### Task 28: Preserve atomic order-confirmation progress

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py:111-113,1399-1487`

- [x] **Step 1: Add a failing regression** with three pending references: the
first resolves to a filled terminal result, the second returns a non-terminal
`OrderResult`, and the third is not inspected.  Assert that the first fill is
applied exactly once and the remaining two confirmations retain their complete
metadata for manual recovery.
- [x] **Step 2: Run the focused test** with
`python -m pytest -q -p no:cacheprovider -W error tests/test_engine.py::test_contract_failure_preserves_atomic_pending_order_progress`
and verify that the current code leaves the first position unchanged and loses
all pending references.
- [x] **Step 3: Consume validated terminal confirmations one at a time.**
After each terminal result, update position/cash/volume and remove only that
confirmation from the active list.  On a contract invariant, move the current
and unprocessed confirmations into `_manual_order_confirmations`, log their
venue/reference metadata, disable automatic repair, and stop shutdown polling.
- [x] **Step 4: Re-run the focused test and existing pending-order recovery
tests** and verify the terminal fill is applied once, manual references remain,
and shutdown drain does not loop.

### Task 29: Reset persistence when market continuity breaks

**Files:**
- Modify: `tests/test_engine.py`
- Modify: `entropy_arb/engine.py:885-917`

- [x] **Step 1: Add failing regressions** that arm a direction, make a book
stale or a venue unready/down for longer than `premium_persist_sec`, restore a
fresh executable book, and assert the first restored scan only starts a new
arm interval.
- [x] **Step 2: Run the focused tests** and verify the current code immediately
returns an execution plan after restoration.
- [x] **Step 3: Clear the affected direction's `_armed` timestamp** before
continuing from stale-book, unready-venue, or outage branches.  Do not reset it
for execution-lock or rate-budget deferrals.
- [x] **Step 4: Re-run persistence and strategy tests** and verify an execution
plan appears only after a new continuous interval.

### Task 30: Separate recorder wall and monotonic clocks

**Files:**
- Modify: `tests/test_recorder.py`
- Modify: `entropy_arb/recorder.py:520-560`

- [x] **Step 1: Add a failing regression** that starts a signal with
`observe(now=time.time())`, closes it with `close()`, and asserts all
`elapsed_ms` values are non-negative.
- [x] **Step 2: Run the focused test** and verify the current code writes a
large negative shutdown duration.
- [x] **Step 3: Make `now` wall-clock-only** in `observe()` and `close()` and
always obtain lifecycle time from `time.monotonic()`.  Update deterministic
duration tests to patch `entropy_arb.recorder.time.monotonic` rather than using
the wall-time argument as a second clock.
- [x] **Step 4: Re-run all recorder tests** and verify timestamps, sampling,
shutdown rows, and mixed explicit/default calls remain correct.

### Task 31: Final state-continuity verification

**Files:**
- Review: `entropy_arb/engine.py`
- Review: `entropy_arb/recorder.py`
- Review: `tests/test_engine.py`
- Review: `tests/test_recorder.py`

- [x] **Step 1: Run the three focused regression groups.**
- [x] **Step 2: Run `python -m pytest -q -p no:cacheprovider -W error`.**
- [x] **Step 3: Run `python -m compileall -q entropy_arb tools main.py tests`.**
- [x] **Step 4: Run `git diff --check HEAD`, inspect `git status --short`, and
review the final recovery/disarming/clock paths for regression risk.**
