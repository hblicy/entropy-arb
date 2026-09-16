# Post-Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Close the stable-lock, overflow-normalization, and schema-documentation findings confirmed after the final audit.

**Architecture:** Keep account-scoped locks and the pending v4 model. Use Linux abstract Unix-domain sockets and Windows machine-wide named mutexes, perform audit arithmetic on validated floats, and update both operator READMEs.

**Tech Stack:** Python 3, Linux `AF_UNIX`, Windows kernel mutexes, frozen dataclasses, JSON pending journal, pytest, Markdown.

---

### Task 1: Make the default account-lock namespace host-stable

**Files:**
- Modify: tests/test_live_lock.py
- Modify: entropy_arb/live_lock.py

- [x] **Step 1: Add a failing default-namespace test**

Create two default LiveProcessLock instances for the same identity while
tempfile.tempdir points at two different directories. Assert their kernel
object names are identical and independent of both temporary roots.

- [x] **Step 2: Verify RED**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_live_lock.py::test_default_lock_namespace_does_not_follow_process_temp_root
~~~

Expected: FAIL because the two instances currently use different roots.

- [x] **Step 3: Add the minimal platform-specific default**

Use account-derived abstract Unix socket names on Linux and initially owned
named mutexes in the Windows `Global\\` namespace. Preserve explicit
directory= behavior for isolated file-lock tests.

- [x] **Step 4: Verify GREEN**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_live_lock.py
~~~

Expected: all live-lock tests PASS.

### Task 2: Normalize overflowing pending-audit derivations

**Files:**
- Modify: tests/test_recovery_state.py
- Modify: entropy_arb/recovery_state.py

- [x] **Step 1: Add three failing loader cases**

Start from a valid v4 OPEN journal and independently inject finite-convertible
integers whose top-convergence subtraction, fee sum, or projected-USD
multiplication overflows. Each load() must raise PendingExecutionStateError
naming the affected audit field.

- [x] **Step 2: Verify RED**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py -k derived_overflow
~~~

Expected: ERROR with a bare OverflowError.

- [x] **Step 3: Use validated floats for all four relationships**

Convert the required audit values, exit target, and venue fees through
_signed_finite() or _finite() and use those returned floats in the relationship
calculations. Existing math.isclose checks then reject non-finite derived
results as inconsistent evidence.

- [x] **Step 4: Verify GREEN**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_recovery_state.py tests/test_engine.py
~~~

Expected: all recovery and engine tests PASS.

### Task 3: Correct pending-schema operator documentation

**Files:**
- Modify: README.md
- Modify: README.zh-CN.md

- [x] **Step 1: Replace the obsolete schema-v3 instructions**

Document v4 as current, empty v3 as readable, active v3 as blocked without
modification, and v2/unknown/malformed states as fail-closed.

- [x] **Step 2: Verify the obsolete statement is gone**

Run:

~~~bash
rg -n "auto-loads only pending schema v3|只会自动读取 schema v3" README.md README.zh-CN.md
~~~

Expected: no matches.

### Task 4: Final verification and commit

**Files:**
- Verify all changed source, tests, documentation, and design files.

- [x] **Step 1: Run complete verification**

~~~bash
python -m pytest -q -p no:cacheprovider -W error
python -m compileall -q entropy_arb main.py tools tests
git diff --check
~~~

Expected: all commands exit 0.

- [x] **Step 2: Review scope and commit**

Confirm only the two fixes, regression tests, schema documentation, and these
design/plan files changed. Commit with:

~~~bash
git commit -m "修复：收尾实盘锁与恢复校验"
~~~
