# Convergence Audit Semantics Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the top-of-book convergence used for entry slippage budgets separately from the final marginal convergence returned by the depth planner.

**Architecture:** Keep `convergence_bps` as the final marginal value and add `top_convergence_bps` through the decision, CSV event and pending audit data path. Advance pending state to v4 while deterministically loading v3 entry records from their existing residual, direction and exit target.

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

### Task 3: Version and migrate pending audit state

**Files:**
- Modify: `tests/test_engine.py:1545-1574,1628-1638`
- Modify: `tests/test_recovery_state.py:36-143`
- Modify: `entropy_arb/engine.py:781-812,1171-1191`
- Modify: `entropy_arb/recovery_state.py:22-41,90-126,332-397`

- [ ] **Step 1: Write failing v4 round-trip and v3 migration tests**

Update the engine pending-audit fixture and expectations so both pending and
settled events retain `top_convergence_bps`. Update `audit_context()` to pass
`top_convergence_bps=2.0` and expect saved schema version 4. Add an entry-state
v3 compatibility test:

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

loaded = store.load()
assert loaded.audit.top_convergence_bps == pytest.approx(32.5)
assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3
```

- [ ] **Step 2: Run recovery tests and verify RED**

```bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py
```

Expected: FAIL because schema v4 and `top_convergence_bps` are not implemented.

- [ ] **Step 3: Implement v4 and deterministic v3 loading**

Set `SCHEMA_VERSION = 4`, add the field to `_AUDIT_FIELDS` and
`PendingAuditContext`, and validate it as an optional non-negative finite value.
Retain `_AUDIT_FIELDS_V3 = _AUDIT_FIELDS - {"top_convergence_bps"}`.

Pass `decision.top_convergence_bps` into `_pending_audit_context`; when a
pending execution settles, pass `audit.top_convergence_bps` to its strategy
event.

When loading schema v3, copy the audit dictionary and reconstruct entry values:

```python
if execution["intent"] in {"OPEN", "ADD"}:
    residual = audit["signed_residual_bps"]
    target = execution["exit_target_bps"]
    if residual is None or target is None:
        raise PendingExecutionStateError(
            "v3 entry audit cannot reconstruct top convergence")
    audit["top_convergence_bps"] = (
        residual - target
        if execution["direction"] == "sell_entropy"
        else target - residual
    )
else:
    audit["top_convergence_bps"] = None
```

Accept only versions 3 and 4; keep v2 and unknown versions on the existing
manual-verification error path. Loading must not rewrite the source file.

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
