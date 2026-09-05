# Distinct Leg Symbols Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Entropy and the selected hedge venue use different native symbols while keeping existing same-symbol commands compatible.

**Architecture:** Keep `Config.symbol` as the canonical Entropy symbol, pass an optional hedge override into `Config.hedge.symbol`, and let the existing adapters consume their own venue configuration. Replace ambiguous recorder identity with explicit per-leg symbols while keeping the analyzer able to read legacy minute files.

**Tech Stack:** Python 3, argparse, dataclasses, csv, pytest

---

### Task 1: CLI and configuration mapping

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_main.py`
- Modify: `entropy_arb/config.py`
- Modify: `main.py`

- [x] **Step 1: Write failing configuration tests**

Add tests proving that omission preserves `SNDK` on both legs, an explicit
`hedge_symbol="ANTHROPIC"` maps only the hedge leg, and blank/control-character
hedge symbols raise `ConfigError`.

```python
def test_hedge_symbol_can_differ_from_entropy_symbol():
    cfg = load_config(
        EXAMPLE, NO_ENV, symbol="ANTH", hedge_symbol="ANTHROPIC",
        hedge_venue="lighter-rh")
    assert cfg.symbol == "ANTH"
    assert cfg.entropy.symbol == "ANTH"
    assert cfg.hedge.symbol == "ANTHROPIC"
```

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL because `load_config` does not accept `hedge_symbol`.

- [x] **Step 3: Implement the minimal configuration mapping**

Extend the keyword-only signature and normalize the effective symbol:

```python
def load_config(..., *, symbol: str, hedge_venue: str,
                hedge_symbol: Optional[str] = None, ...):
    ...
    effective_hedge_symbol = (
        symbol if hedge_symbol is None else hedge_symbol.strip())
    if not effective_hedge_symbol:
        raise ConfigError("--hedge-symbol must not be empty")
    _validate_identity("--hedge-symbol", effective_hedge_symbol)
```

Use `effective_hedge_symbol` for both Lighter and trade.xyz hedge
`VenueConf.symbol`; leave `Config.symbol` and Entropy on `symbol`.

- [x] **Step 4: Verify configuration GREEN**

Run: `python -m pytest tests/test_config.py -q`

Expected: PASS.

- [x] **Step 5: Write and verify a failing CLI forwarding test**

Patch `sys.argv`, `main.load_config`, output validation, logging and the
application runner. Assert that this command forwards the override:

```python
["main.py", "--record-only", "--symbol", "ANTH", "--hedge",
 "lighter-rh", "--hedge-symbol", "ANTHROPIC", "--no-dashboard"]
```

Expected before implementation: argparse exits on unknown `--hedge-symbol`.

- [x] **Step 6: Add and forward the CLI option**

```python
p.add_argument(
    "--hedge-symbol",
    help="hedge venue symbol when it differs from --symbol")
```

Pass `hedge_symbol=args.hedge_symbol` to `load_config` and update the module
examples/help to describe `--symbol` as the Entropy symbol.

- [x] **Step 7: Verify CLI GREEN**

Run: `python -m pytest tests/test_main.py tests/test_config.py -q`

Expected: PASS.

### Task 2: Explicit identity in minute and signal CSVs

**Files:**
- Modify: `tests/test_recorder.py`
- Modify: `entropy_arb/recorder.py`

- [x] **Step 1: Write failing recorder tests**

Update recorder helpers and identity assertions to request
`entropy_symbol="ANTH"` and `hedge_symbol="ANTHROPIC"`, then assert both new
columns exist and contain those exact values for minute and signal rows.
Add a migration test that creates a legacy header and verifies `_open()`
archives it before creating the new schema.

- [x] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_recorder.py -q`

Expected: FAIL because recorder constructors and headers do not contain the
two per-leg symbols.

- [x] **Step 3: Implement minute schema and constructor changes**

Replace the identity prefix with:

```python
HEADER = ["minute_ts", "time_utc", "entropy_symbol", "entropy_dex",
          "hedge_symbol", "hedge_venue", ...]
```

Change `_MinuteAgg.row` and `MinuteRecorder` to accept/store/write
`entropy_symbol` and `hedge_symbol`.

- [x] **Step 4: Implement signal schema and constructor changes**

Use the same four identity fields in `SIGNAL_HEADER`, `SignalRecorder`, and
`_queue_row`. Keep event column lookup by name so lifecycle behavior is
unchanged; update tail validation to read the event fields by their new
indices.

- [x] **Step 5: Verify recorder GREEN**

Run: `python -m pytest tests/test_recorder.py -q`

Expected: PASS.

### Task 3: Analyzer compatibility and engine wiring

**Files:**
- Modify: `tests/test_analyze.py`
- Modify: `tests/test_engine.py`
- Modify: `tools/analyze.py`
- Modify: `entropy_arb/engine.py`

- [x] **Step 1: Write failing analyzer tests**

Add a new-schema fixture with the identity
`("ANTH", "io", "ANTHROPIC", "lighter-rh")`. Assert loading succeeds,
deduplication preserves both symbols, different hedge symbols are rejected as
mixed markets, and partial new identity columns are rejected. Retain legacy
three-column and no-identity tests.

- [x] **Step 2: Run analyzer tests and verify RED**

Run: `python -m pytest tests/test_analyze.py -q`

Expected: FAIL because only the legacy three-field schema is recognized.

- [x] **Step 3: Implement dual-schema analyzer parsing**

Recognize exactly one of:

```python
NEW_IDENTITY = (
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue")
LEGACY_IDENTITY = ("symbol", "entropy_dex", "hedge_venue")
```

Normalize legacy rows to a four-tuple by using the legacy symbol for both
legs. Reject partial or overlapping identity schemas, validate market
identity before time/sample/metric filters, and include all four fields in
the duplicate-minute key.

- [x] **Step 4: Verify analyzer GREEN**

Run: `python -m pytest tests/test_analyze.py -q`

Expected: PASS.

- [x] **Step 5: Write a failing engine wiring assertion**

In the record-only startup test set:

```python
cfg.entropy.symbol = "ANTH"
cfg.hedge.symbol = "ANTHROPIC"
```

Assert both `MinuteRecorder` and `SignalRecorder` receive those values.

- [x] **Step 6: Wire venue-native symbols into both recorders**

Pass `cfg.entropy.symbol` and `cfg.hedge.symbol` to each recorder instead of
the canonical `cfg.symbol`.

- [x] **Step 7: Verify engine GREEN**

Run: `python -m pytest tests/test_engine.py -q`

Expected: PASS.

### Task 4: Operator documentation and final verification

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [x] **Step 1: Update operator documentation**

Document that `--symbol` selects Entropy, `--hedge-symbol` is optional and
defaults to it, and show the exact record-only example:

```bash
python3 main.py --record-only --symbol ANTH --hedge lighter-rh \
  --hedge-symbol ANTHROPIC --no-dashboard
```

Update the minute/signal CSV identity description and explain that a previous
schema at the configured path is archived to `.old` on first write.

- [x] **Step 2: Run focused tests**

Run: `python -m pytest tests/test_main.py tests/test_config.py tests/test_recorder.py tests/test_analyze.py tests/test_engine.py -q`

Expected: PASS.

- [x] **Step 3: Run the full test suite**

Run: `python -m pytest tests -q`

Expected: all tests PASS with no warnings attributable to this change.

- [x] **Step 4: Run static and patch checks**

Run: `python -m py_compile main.py entropy_arb/config.py entropy_arb/recorder.py entropy_arb/engine.py tools/analyze.py`

Run: `git diff --check`

Expected: both commands exit 0.

- [ ] **Step 5: Review final scope and commit**

Confirm the diff contains only the planned CLI, config, recorder, analyzer,
tests, and documentation changes. Commit with a Chinese message:

```bash
git add main.py entropy_arb/config.py entropy_arb/recorder.py \
  entropy_arb/engine.py tools/analyze.py tests/test_main.py \
  tests/test_config.py tests/test_recorder.py tests/test_analyze.py \
  tests/test_engine.py README.md README.zh-CN.md \
  docs/superpowers/plans/2026-09-05-distinct-leg-symbols.md
git commit -m "支持双腿使用不同交易符号"
```
