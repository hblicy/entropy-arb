# Convergence Audit Semantics Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the top-of-book convergence used for entry slippage budgets separately from the final marginal convergence returned by the depth planner.

**Architecture:** Keep `convergence_bps` as the final marginal value and add `top_convergence_bps` through the decision, CSV event and pending audit data path. Advance pending state to v4, allow only empty v3 journals to load, and fail closed on active v3 evidence whose convergence semantics cannot be identified.

**Tech Stack:** Python 3, frozen dataclasses, JSON pending journal, CSV strategy recorder, pytest.

---

### Task 1: Separate strategy decision convergence values

**Files:**
- Modify: `tests/test_strategy.py:466-488`
- Modify: `entropy_arb/strategy.py:336-351,540-630`

- [ ] **Step 1: Write the failing strategy test**

Extend `test_entry_decision_records_final_marginal_convergence` with the expected top value:

```python
assert result.top_convergence_bps == pytest.approx(32.5)
assert result.convergence_bps == pytest.approx(
    result.plan.convergence_bps)
assert result.top_convergence_bps != result.convergence_bps
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python -m pytest -q -p no:cacheprovider tests/test_strategy.py::test_entry_decision_records_final_marginal_convergence
```

Expected: FAIL because `StrategyDecision` has no `top_convergence_bps`.

- [ ] **Step 3: Add the minimal decision field and assignment**

Add an optional field without changing defaults for skip and close decisions:

```python
top_convergence_bps: Optional[float] = None
convergence_bps: Optional[float] = None
```

Populate it only in `_entry_decision`:

```python
top_convergence_bps=convergence,
convergence_bps=plan.convergence_bps,
```

- [ ] **Step 4: Run the strategy test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit the strategy decision change**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "修复：区分顶层与边际收敛值"
```

### Task 2: Carry both values through strategy events

**Files:**
- Modify: `tests/test_strategy_recorder.py`
- Modify: `tests/test_engine.py:963-969`
- Modify: `entropy_arb/strategy_recorder.py:19-81`
- Modify: `entropy_arb/engine.py:1099-1132`

- [ ] **Step 1: Write failing recorder and engine mapping tests**

Add a recorder assertion using a real `StrategyEvent`:

```python
recorded = StrategyEvent(
    ts=60.1, mode="shadow", event="decision", intent="OPEN",
    top_convergence_bps=32.5, convergence_bps=27.5,
)
recorder.record(recorded)
recorder.close()
row = read_rows(path)[0]
assert float(row["top_convergence_bps"]) == pytest.approx(32.5)
assert float(row["convergence_bps"]) == pytest.approx(27.5)
```

Update the strategy-event header expectation and decision event test so the
event preserves `decision.top_convergence_bps` independently of
`decision.convergence_bps`.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_strategy_recorder.py tests/test_engine.py -k "convergence or pending_audit or strategy_event"
```

Expected: FAIL because strategy events do not expose the new field.

- [ ] **Step 3: Add the event field and engine mappings**

Insert `top_convergence_bps` immediately before `convergence_bps` in
`STRATEGY_EVENT_HEADER` and `StrategyEvent`. Pass it in the live/shadow
decision event mapping:

```python
top_convergence_bps=decision.top_convergence_bps,
convergence_bps=decision.convergence_bps,
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit the event data-path change**

```bash
git add entropy_arb/engine.py entropy_arb/strategy_recorder.py tests/test_engine.py tests/test_strategy_recorder.py
git commit -m "修复：记录顶层收敛审计值"
```

### Task 3: Version pending audit state and reject ambiguous v3 evidence

**Files:**
- Modify: `tests/test_engine.py:1545-1574,1628-1638`
- Modify: `tests/test_recovery_state.py:36-143`
- Modify: `entropy_arb/engine.py:781-812,1171-1191`
- Modify: `entropy_arb/recovery_state.py:22-41,90-126,332-397`

- [ ] **Step 1: Write failing v4 and fail-closed compatibility tests**

Update the engine pending-audit fixture and expectations so both pending and
settled events retain `top_convergence_bps`. Update `audit_context()` to pass
`top_convergence_bps=2.0` and expect saved schema version 4. Verify empty v3
state remains readable, while an active v3 state is rejected without rewriting
the source file:

```python
state = replace(
    pending_state(), intent="OPEN", direction="sell_entropy",
    campaign_before=None, campaign_id="campaign-open",
    exit_target_bps=10.0,
    audit=replace(
        audit_context(), signed_residual_bps=42.5,
        top_convergence_bps=32.5,
    ),
)
store.save(state)
payload = json.loads(path.read_text(encoding="utf-8"))
payload["schema_version"] = 3
del payload["pending_execution"]["audit"]["top_convergence_bps"]
path.write_text(json.dumps(payload), encoding="utf-8")

with pytest.raises(
        PendingExecutionStateError,
        match=r"schema_version 3.*manual verification required"):
    store.load()
assert path.read_bytes() == original
```

Parameterize malformed versions with `{}`, `[]`, `3.0` and `True`; all must
raise `PendingExecutionStateError` through the manual-verification path.

- [ ] **Step 2: Run recovery tests and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py
```

Expected: FAIL because active v3 currently loads and an object schema version
raises an uncontrolled `TypeError`.

- [ ] **Step 3: Implement strict version validation and v3 fail-closed loading**

Set `SCHEMA_VERSION = 4`, add the field to `_AUDIT_FIELDS` and
`PendingAuditContext`, and validate it as an optional non-negative finite value.

Pass `decision.top_convergence_bps` into `_pending_audit_context`; when a
pending execution settles, pass `audit.top_convergence_bps` to its strategy
event.

Validate the version before set membership:

```python
if isinstance(schema_version, bool) or not isinstance(schema_version, int):
    raise PendingExecutionStateError(
        f"pending execution state {self.path} has unsupported "
        f"schema_version {schema_version!r}; manual verification required")
if schema_version not in {3, SCHEMA_VERSION}:
    raise PendingExecutionStateError(
        f"pending execution state {self.path} has unsupported "
        f"schema_version {schema_version!r}; manual verification required")
if schema_version == 3:
    if pending is not None:
        raise PendingExecutionStateError(
            f"pending execution state {self.path} has active schema_version "
            "3 evidence; manual verification required")
    return None
```

Only empty v3 and valid v4 are readable. Active v3, v2, malformed and unknown
versions use the manual-verification error path. Loading never rewrites the
source file.

- [ ] **Step 4: Run recovery tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Run all directly affected tests**

```bash
python -m pytest -q -p no:cacheprovider tests/test_strategy.py tests/test_strategy_recorder.py tests/test_recovery_state.py tests/test_engine.py
```

Expected: all tests PASS.

- [ ] **Step 6: Commit pending schema compatibility**

```bash
git add entropy_arb/engine.py entropy_arb/recovery_state.py tests/test_engine.py tests/test_recovery_state.py
git commit -m "修复：升级并兼容收敛审计状态"
```

### Task 4: Final verification

**Files:**
- Verify: all changed source, tests and design documents

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 2: Run syntax and diff checks**

```bash
python -m compileall -q entropy_arb main.py tools tests
git diff --check b08a68eb9c202ebebec5247f5b4005dcca2f85e0..HEAD
git status --short
```

Expected: compile and diff checks exit 0; status is clean after final commits.

- [ ] **Step 3: Review the final diff for scope**

Confirm the diff changes only audit naming, persistence compatibility, tests
and their two design/plan documents. Verify no trading formula or order path
was changed.

### Task 5: Require complete entry audit evidence

**Files:**
- Modify: `tests/test_recovery_state.py`
- Modify: `entropy_arb/recovery_state.py:301-307`

- [x] **Step 1: Write a failing entry-audit test**

Parameterize every value that the production OPEN/ADD path always records:

```python
@pytest.mark.parametrize("field", [
    "signed_residual_bps", "reference_basis_bps",
    "top_convergence_bps", "convergence_bps", "round_trip_fee_bps",
    "buy_slippage_budget_bps", "sell_slippage_budget_bps",
    "projected_net_bps", "projected_net_usd",
])
@pytest.mark.parametrize("intent", ["OPEN", "ADD"])
def test_pending_entry_requires_complete_audit(intent, field):
    state = pending_state()
    changes = {
        "intent": intent,
        "direction": "sell_entropy",
        "buy": replace(state.buy, venue_key="hedge"),
        "sell": replace(state.sell, venue_key="entropy"),
        "audit": replace(state.audit, **{field: None}),
    }
    if intent == "OPEN":
        changes["campaign_before"] = None
    with pytest.raises(
            PendingExecutionStateError,
            match=rf"audit\.{field}.*OPEN.*ADD"):
        replace(state, **changes)
```

- [x] **Step 2: Run the test and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py::test_pending_entry_requires_complete_audit
```

Expected: all cases except `top_convergence_bps` fail because the incomplete
state is currently accepted.

- [x] **Step 3: Implement the minimal intent-aware validation**

For OPEN/ADD, collect the required audit field names whose values are `None`
and raise `PendingExecutionStateError` naming those fields. Leave CLOSE and
FORCED_CLOSE compatibility unchanged.

- [x] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2. Expected: all parameterized cases PASS.

### Task 6: Normalize JSON recursion failures

**Files:**
- Modify: `tests/test_recovery_state.py`
- Modify: `entropy_arb/recovery_state.py:385-394`

- [x] **Step 1: Write a failing malformed-JSON test**

Write a pending state file containing 5,000 nested arrays and assert that
`PendingExecutionStore.load()` raises `PendingExecutionStateError` containing
the file path and `not valid JSON`.

- [x] **Step 2: Run the test and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py::test_pending_loader_wraps_excessive_json_nesting
```

Expected: ERROR with an unwrapped `RecursionError`.

- [x] **Step 3: Implement the minimal exception normalization**

Catch `RecursionError` alongside JSON `ValueError` and wrap it in the existing
`PendingExecutionStateError` message. Do not catch `MemoryError` or unrelated
runtime failures.

- [x] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [x] **Step 5: Repeat Task 4 final verification against the current HEAD**

### Task 7: Require entry reference timing evidence

**Files:**
- Modify: `tests/test_recovery_state.py:118-145`
- Modify: `tests/test_engine.py:882-895`
- Modify: `entropy_arb/recovery_state.py:304-318`

- [x] **Step 1: Extend the failing parameterized test**

Add the three values guaranteed by the entry reference gate:

```python
"entropy_reference_age_ms",
"hedge_reference_age_ms",
"reference_update_skew_ms",
```

- [x] **Step 2: Run the focused test and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py::test_pending_entry_requires_complete_audit
```

Expected: the six new OPEN/ADD combinations fail because they do not raise
`PendingExecutionStateError`.

- [x] **Step 3: Complete the entry fixture and required set**

Give `pending_entry_audit_context()` finite timing values:

```python
entropy_reference_age_ms=15.0,
hedge_reference_age_ms=20.0,
reference_update_skew_ms=5.0,
```

Add the same three field names to `entry_audit_fields` so OPEN/ADD journals
with missing timing evidence fail closed. Keep CLOSE/FORCED_CLOSE optional.

- [x] **Step 4: Verify GREEN and all directly affected tests**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py tests/test_engine.py
```

Expected: all tests PASS.

- [x] **Step 5: Run the full suite and repository checks**

```bash
python -m pytest -q -p no:cacheprovider
python -m compileall -q entropy_arb main.py tools tests
git diff --check
git status --short
```

Expected: all tests and checks PASS; only the intended source, tests and
existing convergence design/plan documents are changed before commit.

### Task 8: Validate entry audit cross-field consistency

**Files:**
- Modify: `tests/test_recovery_state.py`
- Modify: `tests/test_engine.py:882-898,1024-1035`
- Modify: `entropy_arb/recovery_state.py:301-325`

- [x] **Step 1: Write four failing relationship tests**

Build a valid OPEN state from `pending_state()` and independently corrupt each
derived field. Assert that construction rejects the journal with the affected
field name:

```python
@pytest.mark.parametrize(("field", "value"), [
    ("top_convergence_bps", 9.0),
    ("convergence_bps", 11.0),
    ("round_trip_fee_bps", 0.9),
    ("projected_net_usd", 999.0),
])
def test_pending_entry_rejects_inconsistent_audit(field, value):
    state = valid_open_pending_state()
    with pytest.raises(PendingExecutionStateError, match=field):
        replace(state, audit=replace(state.audit, **{field: value}))
```

- [x] **Step 2: Run the test and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py::test_pending_entry_rejects_inconsistent_audit
```

Expected: four failures because each inconsistent state is currently accepted.

- [x] **Step 3: Implement the four minimal OPEN/ADD validations**

For `sell_entropy`, require
`top_convergence_bps == signed_residual_bps - exit_target_bps`; for
`buy_entropy`, require
`top_convergence_bps == exit_target_bps - signed_residual_bps`. Require
`convergence_bps <= top_convergence_bps`,
`round_trip_fee_bps == 2 * (entropy_fee_bps + hedge_fee_bps)`, and
`projected_net_usd == planned_notional_usd * projected_net_bps / 1e4`.
Use `math.isclose(rel_tol=1e-12, abs_tol=1e-9)` for equality and the same
tolerance at the convergence boundary.

- [x] **Step 4: Replace the impossible engine OPEN fixture**

Use a reachable sell-entry residual of `45.0`, top convergence `20.0`, and
marginal convergence `18.0` for the fixture whose exit target is `25.0` and
entry boundary is `34.0`. Where a test customizes top/marginal values, customize
the signed residual consistently.

- [x] **Step 5: Verify focused and full suites**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py tests/test_engine.py
python -m pytest -q -p no:cacheprovider
python -m compileall -q entropy_arb main.py tools tests
git diff --check
```

Expected: all tests and checks PASS.
