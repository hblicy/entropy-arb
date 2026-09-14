# Dynamic Residual Live-Safety Review Fixes Design

## 1. Goal

Close the live-safety gaps found in the post-implementation review without
changing the residual strategy's trading rules, thresholds, fees, symbols, or
default shadow-only deployment posture.

The repaired system must preserve this invariant: an order may be sent only
when durable campaign state, refreshed exchange positions, and the proposed
intent agree about whether the order adds or removes risk.

## 2. Chosen approach

Use the existing engine, campaign, recovery, and venue abstractions and tighten
their boundaries. Do not rewrite the execution state machine.

Alternatives considered:

- Adding only `reduce_only` would prevent one reversal path but would leave
  stale campaign state, doubled slippage limits, and misleading replay results.
- Replacing the execution engine with a new transactional coordinator would
  provide a cleaner long-term model but is too broad and regression-prone for
  this bug-fix scope.

The selected approach makes each fix independently testable and keeps legacy
`fixed_premium` behavior unchanged.

## 3. Order protection

For dynamic `CLOSE` and `FORCED_CLOSE`, both venue orders must be submitted
with `reduce_only=True`. `OPEN`, `ADD`, and all legacy fixed-premium orders keep
`reduce_only=False`.

The dynamic slippage budget is one total per-leg allowance measured from the
decision-time best executable price. The depth planner may consume part of that
allowance. The final IOC protection price must therefore use only the remaining
budget:

```text
remaining_buy_bps = max(leg_budget_bps - buy_depth_slippage_bps, 0)
remaining_sell_bps = max(leg_budget_bps - sell_depth_slippage_bps, 0)
```

The submitted protection price relative to the decision-time best price must
never exceed the configured per-leg budget or the 20 bps hard maximum. Tests
will cover opening, normal close, and forced close.

## 4. Campaign and position reconciliation

After any complete two-venue position refresh, and after residual repair, the
engine must validate the active live campaign against both gross exchange
positions. A zero net sum alone is insufficient.

If positions cannot be explained by the campaign, the engine must:

- keep `_recovery_required` set;
- disable automatic new-risk execution;
- retain the durable campaign and pending execution evidence;
- log a critical error requiring explicit recovery rather than guessing state.

Residual repair for a dynamic close must also update the pending dynamic
execution's matched close quantity. The recovery sequence may clear a campaign
only after refreshed positions prove that both legs are flat within a strict
sub-step tolerance. Campaign quantities below one common executable size step
are normalized only from verified exchange positions, never from an assumed
fill.

## 5. Durable pending executions

Add a separate pending-execution journal derived from the configured campaign
state path. It records only non-secret recovery data:

- schema version, market identity, intent, direction, decision identifier;
- both venue keys, sides, client order references, terminal status and applied
  fill quantities;
- campaign identifier and enough frozen decision/plan data to apply confirmed
  matched fills exactly once.

The journal is atomically replaced. State transitions are persisted before an
order can become unrecoverable, after each order result changes, and after the
campaign update. On startup, a non-empty journal is resolved before strategy
evaluation. Ambiguous or incompatible data fails closed. The journal contains
no private keys, tokens, or API credentials.

## 6. Single-process live lock

Acquire a non-blocking OS file lock before live adapters can send orders and
hold it until resource cleanup completes. The lock identity is derived from the
non-secret venue account identifiers and market identity; it is not based only
on the campaign-state filename. Linux uses `fcntl`, Windows tests use
`msvcrt`, with a small internal wrapper and no new dependency.

Shadow/record-only runs do not take the live account lock. Lock contention
fails startup with an actionable error and sends no orders.

## 7. Model warm start and replay parity

Warm start must explicitly insert invalid observations for every missing minute
between the last accepted historical minute and the current minute. Five
consecutive missing real minutes must make the first startup snapshot
`REGIME_UNSTABLE`.

Historical replay remains a documented top-of-book approximation, but it must
match engine-level decision gates:

- insert missing invalid minutes;
- enforce `premium_persist_sec` for `OPEN` and `ADD`;
- enforce `cooldown_sec` after every simulated action;
- calculate remaining per-venue position headroom from the simulated campaign;
- report and assert maximum accumulated leg notional, in addition to per-order
  notional.

Replay does not claim actual fill PnL and does not train the live slippage model.

## 8. CLI and compatibility

Update `--record-only` help to state that residual mode runs the shadow strategy
while sending no orders. No configuration default enables live trading.

Existing campaign state remains readable. Any new durable journal has its own
schema so campaign JSON compatibility is not coupled to order recovery.

## 9. Error handling and observability

Expected lock contention and incompatible recovery state produce recognizable
startup errors. Unexpected I/O, serialization, or adapter failures keep their
exception context and stop or pause the engine according to the existing
recovery rules. Key transitions log the market, venue, intent, order reference,
and recovery state without logging credentials.

## 10. Verification

Each behavior is implemented through a red-green regression test. Required
coverage includes:

1. normal and forced closes submit both legs reduce-only;
2. final submitted prices stay within the single total slippage budget;
3. partial close plus residual repair cannot leave a tradable stale campaign;
4. paired manual position changes are detected despite zero net exposure;
5. a crash-era pending execution is recovered exactly once or fails closed;
6. a second live process/account lock is rejected before order capability;
7. a five-minute warm-start tail gap starts unstable;
8. replay enforces persistence, cooldown, missing-minute behavior, and
   cumulative position caps;
9. legacy fixed-premium and record-only behavior remain compatible.

Final verification runs the focused tests, complete pytest suite, Python compile
checks, `git diff --check`, and the supplied historical replay. Live remains
disabled after verification and requires a fresh multi-day shadow run before a
separate live-readiness decision.
