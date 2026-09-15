# Convergence Audit Semantics Fix

## Scope

Fix the audit ambiguity between the top-of-book convergence used to quote
entry slippage budgets and the final marginal convergence returned by the
depth planner. Trading signals, fee calculations, slippage budgets, order
directions, position limits and execution behavior remain unchanged.

## Root cause

Entry slippage budgets are calculated before depth planning from the current
top-of-book residual and exit target. After planning, `convergence_bps` is the
exact convergence at the final executable marginal prices. Persisting that
value beside the earlier budgets without retaining their input makes the audit
record appear internally inconsistent.

## Design

Keep `convergence_bps` as the final marginal convergence because it matches the
planner and `projected_net_bps`. Add `top_convergence_bps` as the explicit
pre-planning convergence used to quote entry slippage budgets.

- `StrategyDecision` carries both values for `OPEN` and `ADD` decisions.
- Strategy CSV events persist both values. Existing files with the old header
  are archived by the recorder's existing header-compatibility behavior.
- `PendingAuditContext` persists both values so recovery and settled execution
  events retain the original decision evidence.
- Pending state schema advances from v3 to v4. The loader accepts v3 because
  its entry direction, signed residual and exit target are sufficient to
  reconstruct `top_convergence_bps` deterministically. Non-entry v3 records
  receive `None`. v2 and unknown versions remain fail-closed.

The alternative of changing `convergence_bps` back to the top-of-book value is
rejected because it would again diverge from the planner's exact marginal
result. Deriving the top value only during offline analysis is also rejected
because pending execution evidence must be self-contained.

## Validation

Tests must first demonstrate the missing distinction, then verify:

1. multi-level entry decisions expose different top and marginal values;
2. strategy events write both columns;
3. pending v4 round-trips both values;
4. v3 pending state is upgraded deterministically without modifying the source
   file during load;
5. malformed, v2 and unknown pending schemas still fail closed;
6. the full test suite, compile check and `git diff --check` pass.
