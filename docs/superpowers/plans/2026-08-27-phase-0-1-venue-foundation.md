# Entropy Arb Phase 0–1 Venue Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. This repository forbids subagents unless the user explicitly approves them.

**Goal:** Restore a green test baseline and introduce a typed, registered venue boundary without changing the current Entropy-plus-one-hedge strategy or live execution behavior.

**Architecture:** Keep the existing two-venue engine and configuration contract intact while replacing dict-shaped order results with a typed value, defining the common venue protocol, moving venue construction into an explicit registry, and moving Hyperliquid peer-account setup behind the adapter boundary. No multi-hedge routing, dynamic thresholds, persistence, or live behavior changes belong in this phase.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, websockets, PyYAML, Rich, pytest, dataclasses, typing.Protocol

---

## Scope and execution rules

Implement this plan on a new branch or isolated worktree created from commit `538bd21` or its descendant. The suggested branch is `codex/multi-hedge-foundation`. Use `superpowers:using-git-worktrees` before implementation if an isolated worktree is appropriate.

The phase must preserve these externally visible behaviors:

- `python main.py --record-only --symbol SNDK --hedge lighter-rh` still starts one Entropy leg and one selected hedge leg.
- Configuration still uses the current `thresholds`, `entropy`, and `hedge` sections.
- `--hedge` still accepts only `lighter`, `lighter-rh`, and `tradexyz`.
- Order pricing, simultaneous two-leg submission, reconciliation, dashboard content, and CSV schemas remain unchanged.
- No dependency is added.

Do not begin multi-venue configuration or routing in this plan. Each task must leave the full test suite green before committing.

## File map

### Files created

- `entropy_arb/models.py` — typed normalized order result used by both venue implementations and the engine.
- `entropy_arb/venues/__init__.py` — venue-boundary package marker and public exports.
- `entropy_arb/venues/base.py` — runtime-checkable `VenueAdapter` protocol.
- `entropy_arb/venues/registry.py` — explicit `kind -> constructor` registry.
- `tests/test_models.py` — normalized order result tests.
- `tests/test_venue_contract.py` — adapter protocol, Hyperliquid parser, and peer-configuration tests.
- `tests/test_venue_registry.py` — registry selection and unsupported-kind tests.

### Files modified

- `entropy_arb/config.py` — explicit UTF-8 config reading only.
- `entropy_arb/dashboard.py` — injectable Rich console for deterministic rendering tests.
- `entropy_arb/venue_hl.py` — typed order results and adapter-owned peer setup.
- `entropy_arb/venue_lighter.py` — typed order results and no-op peer setup.
- `entropy_arb/engine.py` — typed result consumption and registry-based venue creation.
- `tests/test_config.py` — cross-platform UTF-8 regression fixture.
- `tests/test_dashboard.py` — render through the same fixed-width console used by `Dashboard`.
- `tests/test_engine.py` — typed execution result and raised-exception coverage.
- `README.md` — document the new internal venue boundary.
- `README.zh-CN.md` — mirror the directory documentation in Chinese.

---

### Task 1: Make configuration reading explicitly UTF-8

**Files:**

- Modify: `tests/test_config.py:18-22`
- Modify: `tests/test_config.py:38-48`
- Modify: `entropy_arb/config.py:258-266`

- [ ] **Step 1: Add a UTF-8 regression test and make temporary fixtures explicit**

Change `write_tmp` and add the new test below. Keeping the temporary writer explicit prevents the test fixture itself from depending on the host locale.

```python
def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


def test_utf8_config_loads_independently_of_system_locale():
    cfg = load("""
# 中文配置注释必须在 Windows 和 Linux 上一致读取
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
""")
    assert cfg.symbol == "SNDK"
    assert cfg.midline_bps == 5.0
```

- [ ] **Step 2: Run the config tests and observe the current Windows failure**

Run:

```powershell
python -m pytest tests/test_config.py -q
```

Expected before the implementation change on Windows: `test_example_config_loads` fails with `UnicodeDecodeError` from the default GBK decoder. The new temporary-file test may pass because its file is ASCII-compatible apart from the comment; the existing UTF-8 example remains the authoritative failing reproduction.

- [ ] **Step 3: Read YAML with an explicit encoding**

In `load_config`, replace the existing open call with:

```python
    try:
        with open(config_file, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
```

Do not catch `UnicodeDecodeError`; malformed or non-UTF-8 configuration must fail visibly.

- [ ] **Step 4: Run the focused and full tests**

Run:

```powershell
python -m pytest tests/test_config.py -q
python -m pytest tests -q
```

Expected: config tests pass. The full suite still has only the pre-existing dashboard-width failure.

- [ ] **Step 5: Commit the encoding fix**

```powershell
git add entropy_arb/config.py tests/test_config.py
git commit -m "修复：统一使用 UTF-8 读取配置"
```

---

### Task 2: Make dashboard rendering deterministic in tests

**Files:**

- Modify: `entropy_arb/dashboard.py:125-132`
- Modify: `tests/test_dashboard.py:51-55`

- [ ] **Step 1: Change the render helper to inject its fixed-width console**

Replace the test helper with:

```python
def render(eng, lang="en") -> str:
    console = Console(record=True, width=120, force_terminal=True)
    dash = Dashboard(
        eng, BufferLogHandler(), "logs/engine.log", lang=lang,
        console=console)
    console.print(dash._safe_render())
    return console.export_text()
```

- [ ] **Step 2: Run the failing dashboard test**

Run:

```powershell
python -m pytest tests/test_dashboard.py::test_renders_key_numbers -q
```

Expected: FAIL with `TypeError` because `Dashboard.__init__` does not yet accept `console`.

- [ ] **Step 3: Add optional console injection without changing production defaults**

Update the constructor exactly as follows:

```python
class Dashboard:
    def __init__(self, eng, log_buffer: BufferLogHandler, log_file: str,
                 force_terminal: bool = False, lang: str = "en",
                 console: Optional[Console] = None) -> None:
        self.eng = eng
        self.log_buffer = log_buffer
        self.log_file = log_file
        self.lang = lang
        self.console = console or Console(
            force_terminal=True if force_terminal else None)
```

The production caller in `main.py` does not pass `console`, so terminal detection remains unchanged.

- [ ] **Step 4: Run dashboard and full tests**

Run:

```powershell
python -m pytest tests/test_dashboard.py -q
python -m pytest tests -q
```

Expected: all existing tests plus the new UTF-8 test pass; total at this point is `30 passed`.

- [ ] **Step 5: Commit the deterministic renderer**

```powershell
git add entropy_arb/dashboard.py tests/test_dashboard.py
git commit -m "修复：固定仪表盘测试渲染宽度"
```

---

### Task 3: Replace dict-shaped order results with `OrderResult`

**Files:**

- Create: `entropy_arb/models.py`
- Create: `tests/test_models.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/engine.py:411-498`
- Modify: `entropy_arb/engine.py:514-569`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write the model tests**

Create `tests/test_models.py`:

```python
import pytest

from entropy_arb.models import OrderResult


def test_order_result_exposes_normalized_fields():
    result = OrderResult(
        status="filled", filled_base=0.5, avg_px=100.25)
    assert result.status == "filled"
    assert result.filled_base == 0.5
    assert result.avg_px == 100.25
    assert result.err is None
    assert result.unresolved is False


def test_order_result_rejects_negative_fill():
    with pytest.raises(ValueError, match="filled_base"):
        OrderResult(status="filled", filled_base=-0.1)


def test_order_result_rejects_nonpositive_average_price():
    with pytest.raises(ValueError, match="avg_px"):
        OrderResult(status="filled", filled_base=0.1, avg_px=0.0)


def test_order_result_marks_rate_limit_errors_explicitly():
    result = OrderResult.send_failed("RATE_LIMITED: HTTP 429")
    assert result.status == "send-failed"
    assert result.rate_limited is True
```

- [ ] **Step 2: Run the new model tests and verify import failure**

Run:

```powershell
python -m pytest tests/test_models.py -q
```

Expected: collection fails with `ModuleNotFoundError: entropy_arb.models`.

- [ ] **Step 3: Implement the normalized result type**

Create `entropy_arb/models.py`:

```python
"""Normalized domain values shared across venue adapters and the engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderResult:
    status: str
    filled_base: float = 0.0
    avg_px: Optional[float] = None
    err: Optional[str] = None
    unresolved: bool = False

    def __post_init__(self) -> None:
        if not self.status:
            raise ValueError("status must not be empty")
        if self.filled_base < 0:
            raise ValueError("filled_base must be >= 0")
        if self.avg_px is not None and self.avg_px <= 0:
            raise ValueError("avg_px must be > 0 when present")

    @classmethod
    def send_failed(cls, err: str) -> "OrderResult":
        if not err:
            raise ValueError("send failure must include an error")
        return cls(status="send-failed", err=err)

    @classmethod
    def unknown(cls, status: str = "timeout") -> "OrderResult":
        return cls(status=status, unresolved=True)

    @property
    def rate_limited(self) -> bool:
        return bool(self.err and self.err.startswith("RATE_LIMITED"))
```

- [ ] **Step 4: Run the model tests**

Run:

```powershell
python -m pytest tests/test_models.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Add execution tests that require typed results**

In `tests/test_engine.py`, import `ArbPlan` and `OrderResult`:

```python
from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.models import OrderResult  # noqa: E402
```

Add this subclass and plan factory after `StubVenue`:

```python
class ExecutingVenue(StubVenue):
    def __init__(self, key, label, result):
        super().__init__(key, label)
        self.result = result

    def px_round(self, px, round_up):
        return px

    async def send_taker(self, **_kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def execution_plan():
    return ArbPlan(
        qty=0.5,
        buy_limit=100.0,
        sell_limit=100.2,
        buy_notional=50.0,
        sell_notional=50.1,
        q_max=0.5,
        q_max_notional=50.0,
        top_premium_bps=20.0,
        marginal_premium_bps=20.0,
        buy_fee=0.0,
        sell_fee=0.0,
    )
```

Add two tests:

```python
def test_execute_consumes_typed_order_results():
    eng = make_engine()
    buy = ExecutingVenue(
        "hedge", "RH",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.0))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    unresolved = asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert unresolved is False
    assert buy.position == 0.5
    assert sell.position == -0.5
    assert eng.trades == 1
    approx(eng.total_fill_edge, 0.1)


def test_execute_converts_raised_leg_exception_to_failure():
    eng = make_engine()
    buy = ExecutingVenue("hedge", "RH", RuntimeError("send exploded"))
    sell = ExecutingVenue(
        "entropy", "ENTROPY",
        OrderResult(status="filled", filled_base=0.5, avg_px=100.2))
    buy.set_book(99.9, 100.0)
    sell.set_book(100.2, 100.3)
    eng.entropy, eng.hedge = sell, buy
    eng.venues = {"entropy": sell, "hedge": buy}

    unresolved = asyncio.run(eng._execute(buy, sell, execution_plan()))

    assert unresolved is False
    assert eng.trades == 0
    assert eng.consec_errors == 1
    assert eng.recent_trades[-1]["status"] == "send-failed/filled"
```

- [ ] **Step 6: Run the new engine tests and verify the old dict contract fails**

Run:

```powershell
python -m pytest tests/test_engine.py::test_execute_consumes_typed_order_results tests/test_engine.py::test_execute_converts_raised_leg_exception_to_failure -q
```

Expected: FAIL because `engine.py` calls `.get()` and item access on `OrderResult`.

- [ ] **Step 7: Convert Hyperliquid returns to `OrderResult`**

Import the model:

```python
from .models import OrderResult
```

Change `send_taker` to return `OrderResult`. Replace every result dictionary with the corresponding constructor:

```python
return OrderResult.send_failed(f"signing failed: {e!r}")
return OrderResult.send_failed(err)
return OrderResult(status=status, filled_base=filled)
return OrderResult.unknown("timeout")
```

Replace the unresolved parse check:

```python
            res = self._parse(body)
            if not res.unresolved:
                return res
```

Replace `_parse` with this typed implementation while preserving all existing status meanings:

```python
    @staticmethod
    def _parse(body: dict) -> OrderResult:
        def fail(msg: str) -> OrderResult:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return OrderResult.send_failed(msg)

        if body.get("status") == "err":
            return fail(str(body.get("response")))
        if body.get("status") != "ok":
            return fail(f"unexpected response: {str(body)[:200]}")
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return fail(f"malformed response: {str(body)[:200]}")
        if "filled" in st:
            fill = st["filled"]
            return OrderResult(
                status="filled",
                filled_base=float(fill.get("totalSz") or 0.0),
                avg_px=(float(fill["avgPx"])
                        if fill.get("avgPx") else None),
            )
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                return OrderResult(status="canceled")
            return fail(msg)
        if "resting" in st:
            return OrderResult.unknown("resting?")
        return fail(f"unknown status: {str(st)[:150]}")
```

Update the module docstring to say that `send_taker()` returns `OrderResult`, not a dictionary shape.

- [ ] **Step 8: Convert Lighter returns to `OrderResult`**

Import `OrderResult` and change `send_taker` to return it. Keep `AccountOrdersFeed` internal messages as dictionaries because they are private websocket parsing state. At the public method boundary, use:

```python
return OrderResult.send_failed(msg)
```

for signing or API rejection, then:

```python
        if fut is None:
            return OrderResult.unknown("sent-unconfirmed")
        try:
            info = await asyncio.wait_for(fut, timeout=self.settle_timeout)
            return OrderResult(
                status=info["status"],
                filled_base=info["filled_base"],
                avg_px=info.get("avg_px"),
            )
        except asyncio.TimeoutError:
            self.orders_feed.unwatch(coi)
            log.warning("[%s] no settle confirmation for coi %d in %.1fs",
                        self.name, coi, self.settle_timeout)
            return OrderResult.unknown("timeout")
```

Update the module docstring to name `OrderResult`.

- [ ] **Step 9: Convert the engine to attribute access and fail on invalid adapter returns**

Import `OrderResult`:

```python
from .models import OrderResult
```

After `asyncio.gather`, normalize only raised exceptions. Do not accept legacy dictionaries silently:

```python
        raw_results = await asyncio.gather(
            buy.send_taker(is_buy=True, qty=plan.qty, limit_px=buy_bound),
            sell.send_taker(is_buy=False, qty=plan.qty, limit_px=sell_bound),
            return_exceptions=True)
        results = []
        for result in raw_results:
            if isinstance(result, BaseException):
                results.append(OrderResult.send_failed(repr(result)))
            elif isinstance(result, OrderResult):
                results.append(result)
            else:
                raise TypeError(
                    f"venue returned {type(result).__name__}, expected OrderResult")
        binfo, sinfo = results
```

Replace dictionary access throughout `_execute`:

```python
        for venue, info, side in ((buy, binfo, "buy"),
                                  (sell, sinfo, "sell")):
            if info.err:
                log.error("[%s] %s leg: %s", venue.name, side, info.err)
        bfill = binfo.filled_base
        sfill = sinfo.filled_base
```

Replace the fill accounting and settlement log with:

```python
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.avg_px or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
        if sfill:
            spx = sinfo.avg_px or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx

        matched = min(bfill, sfill)
        fill_edge = 0.0
        if matched > 0 and binfo.avg_px and sinfo.avg_px:
            fill_edge = matched * (
                sinfo.avg_px * (1 - plan.sell_fee)
                - binfo.avg_px * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | "
                 "sell %s %s %.6g/%.6g | matched %.6g | fill edge $%.4f",
                 direction, buy.name, binfo.status, bfill, plan.qty,
                 sell.name, sinfo.status, sfill, plan.qty, matched, fill_edge)
```

The final status and CSV calls become:

```python
        unresolved = binfo.unresolved or sinfo.unresolved
        hard_err = binfo.err is not None or sinfo.err is not None
        rate_limited = False
        for venue, info in ((buy, binfo), (sell, sinfo)):
            if info.rate_limited:
                rate_limited = True
                self._mark_limited(venue)
            elif "margin" in info.status.lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", venue.name)
                self._mark_limited(venue)
```

```python
        self._record_trade(
            direction, plan, None if unresolved else fill_edge,
            f"{binfo.status}/{sinfo.status}", sent_ok)
        self._log_csv(
            direction, buy, sell, plan, sent_ok, bfill, sfill,
            binfo.status, sinfo.status, fill_edge, inv_bps)
```

Convert `_hedge` in the same strict way:

```python
                info = await v.send_taker(
                    is_buy=not is_sell, qty=qty,
                    limit_px=limit, reduce_only=True)
                if not isinstance(info, OrderResult):
                    raise TypeError(
                        f"venue returned {type(info).__name__}, "
                        "expected OrderResult")
                if info.err or info.unresolved:
                    log.error("[HEDGE] %s: %s", v.name,
                              info.err or "unresolved")
                    if info.rate_limited:
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                else:
                    fill = info.filled_base
```

Complete the successful hedge branch with typed attribute access:

```python
                else:
                    fill = info.filled_base
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = info.avg_px or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info.status, fill, qty)
```

- [ ] **Step 10: Run typed-result and full regression tests**

Run:

```powershell
python -m pytest tests/test_models.py tests/test_engine.py -q
python -m pytest tests -q
```

Expected: `36 passed` and no dictionary-shaped venue result remains in the public adapter path. Confirm with:

```powershell
rg -n 'return \{"status"' entropy_arb/venue_hl.py entropy_arb/venue_lighter.py
rg -n 'info\.get\(|binfo\[|sinfo\[' entropy_arb/engine.py
```

Expected: neither command returns a match. Private `AccountOrdersFeed` dictionaries remain internal to `venue_lighter.py` and are converted to `OrderResult` at the public `send_taker` boundary.

- [ ] **Step 11: Commit the normalized result contract**

```powershell
git add entropy_arb/models.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py entropy_arb/engine.py tests/test_models.py tests/test_engine.py
git commit -m "重构：统一交易所订单结果模型"
```

---

### Task 4: Define the venue protocol and move peer-account setup into adapters

**Files:**

- Create: `entropy_arb/venues/__init__.py`
- Create: `entropy_arb/venues/base.py`
- Create: `tests/test_venue_contract.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/engine.py:147-162`

- [ ] **Step 1: Write venue contract and Hyperliquid peer tests**

Create `tests/test_venue_contract.py`:

```python
import os
import tempfile
from types import SimpleNamespace

from entropy_arb.config import load_config
from entropy_arb.models import OrderResult
from entropy_arb.venue_hl import HLVenue, NonceAllocator
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.venues.base import VenueAdapter


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
MINIMAL = """
thresholds:
  midline_bps: 0
  upper_bps: 4
  lower_bps: 4
"""


def make_cfg(hedge):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(MINIMAL)
    f.close()
    return load_config(
        f.name, NO_ENV, symbol="SNDK", hedge_venue=hedge)


def test_existing_venues_satisfy_runtime_protocol():
    hl_cfg = make_cfg("tradexyz")
    lighter_cfg = make_cfg("lighter")
    hl = HLVenue(hl_cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    lighter = LighterVenue(lighter_cfg.hedge, object(), 5.0)
    assert isinstance(hl, VenueAdapter)
    assert isinstance(lighter, VenueAdapter)


def test_hl_parse_returns_typed_fill():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.5", "avgPx": "100.25"}}
    ]}}}
    result = HLVenue._parse(body)
    assert result == OrderResult(
        status="filled", filled_base=0.5, avg_px=100.25)


def test_hl_parse_marks_resting_ioc_as_unknown():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 1}}
    ]}}}
    result = HLVenue._parse(body)
    assert result.status == "resting?"
    assert result.unresolved is True


def test_hl_peer_setup_shares_nonce_and_deduplicates_equity():
    cfg = make_cfg("tradexyz")
    anchor = HLVenue(cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    hedge = HLVenue(cfg.hedge, "https://api", "wss://ws", object(), 5.0)
    anchor.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())
    hedge.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())

    anchor.configure_peer(hedge)

    assert hedge.account.nonces is anchor.account.nonces
    assert hedge.include_core_equity is False
```

- [ ] **Step 2: Run the contract tests and verify the missing boundary**

Run:

```powershell
python -m pytest tests/test_venue_contract.py -q
```

Expected: collection fails because `entropy_arb.venues.base` does not exist.

- [ ] **Step 3: Create the protocol package**

Create `entropy_arb/venues/__init__.py`:

```python
"""Common venue boundaries and explicit adapter registration."""

from .base import VenueAdapter

__all__ = ["VenueAdapter"]
```

Create `entropy_arb/venues/base.py`:

```python
"""Structural contract implemented by every trading venue adapter."""
from __future__ import annotations

import asyncio
from typing import Callable, List, Optional, Protocol, runtime_checkable

from ..book import OrderBook
from ..config import VenueConf
from ..models import OrderResult


@runtime_checkable
class VenueAdapter(Protocol):
    kind: str
    conf: VenueConf
    key: str
    name: str
    book: OrderBook
    position: float
    cash: float
    volume_usd: float
    equity: Optional[float]
    free: Optional[float]
    start_equity: Optional[float]
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    last_traded_ts: float
    size_decimals: int
    min_base: float
    min_quote: float

    async def load_market(self) -> None: ...

    def init_signer(self) -> None: ...

    def configure_peer(self, other: "VenueAdapter") -> None: ...

    def start_tasks(
            self, stop: asyncio.Event, notify: Callable[[], None],
            live: bool) -> List[asyncio.Task]: ...

    def ready_to_trade(self) -> bool: ...

    async def warm_http(self) -> None: ...

    def px_round(self, px: float, round_up: bool) -> float: ...

    async def send_taker(
            self, *, is_buy: bool, qty: float, limit_px: float,
            reduce_only: bool = False) -> OrderResult: ...

    async def fetch_equity(self): ...

    async def fetch_position(self) -> float: ...

    async def close(self) -> None: ...
```

- [ ] **Step 4: Implement adapter-owned peer setup**

Add this method to `HLVenue` immediately after `share_nonces_with`:

```python
    def configure_peer(self, other) -> None:
        """Configure shared Hyperliquid signer and account accounting."""
        if not isinstance(other, HLVenue):
            return
        self.share_nonces_with(other)
        address = self._query_address()
        if address and address == other._query_address():
            other.include_core_equity = False
```

Add a no-op implementation to `LighterVenue` after `init_signer`:

```python
    def configure_peer(self, other) -> None:
        """Lighter deployments do not share Hyperliquid account state."""
        return
```

This no-op is the explicit implementation of the common lifecycle contract, not an error fallback.

- [ ] **Step 5: Replace exchange-specific peer branches in the engine**

After both signers are initialized, and outside the `if live` block so record-only account-address accounting remains equivalent, use one adapter call:

```python
        if live:
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
        self.entropy.configure_peer(self.hedge)
```

Delete these concrete checks from `engine.py`:

```python
            if self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
        if (self.hedge.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False
```

- [ ] **Step 6: Run contract and full tests**

Run:

```powershell
python -m pytest tests/test_venue_contract.py -q
python -m pytest tests -q
```

Expected: venue contract tests report `4 passed`; full suite reports `40 passed`.

- [ ] **Step 7: Commit the common venue boundary**

```powershell
git add entropy_arb/venues/__init__.py entropy_arb/venues/base.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py entropy_arb/engine.py tests/test_venue_contract.py
git commit -m "重构：定义统一交易所适配器协议"
```

---

### Task 5: Move venue construction into an explicit registry

**Files:**

- Create: `entropy_arb/venues/registry.py`
- Create: `tests/test_venue_registry.py`
- Modify: `entropy_arb/engine.py:28-31`
- Modify: `entropy_arb/engine.py:123-144`

- [ ] **Step 1: Write registry selection and fail-fast tests**

Create `tests/test_venue_registry.py`:

```python
import os
import tempfile
from dataclasses import replace

import pytest

from entropy_arb.config import load_config
from entropy_arb.venue_hl import HLVenue
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.venues.registry import VenueRuntime, create_venue


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
MINIMAL = """
thresholds:
  midline_bps: 0
  upper_bps: 4
  lower_bps: 4
"""


def make_cfg(hedge):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(MINIMAL)
    f.close()
    return load_config(
        f.name, NO_ENV, symbol="SNDK", hedge_venue=hedge)


def runtime():
    return VenueRuntime(
        session=object(),
        hl_api_url="https://api",
        hl_ws_url="wss://ws",
        settle_timeout_sec=5.0,
    )


def test_registry_creates_hyperliquid_adapter():
    cfg = make_cfg("tradexyz")
    assert isinstance(create_venue(cfg.entropy, runtime()), HLVenue)


def test_registry_creates_lighter_adapter():
    cfg = make_cfg("lighter")
    assert isinstance(create_venue(cfg.hedge, runtime()), LighterVenue)


def test_registry_rejects_unknown_kind():
    cfg = make_cfg("lighter")
    unknown = replace(cfg.hedge, kind="unknown")
    with pytest.raises(ValueError, match="unsupported venue kind 'unknown'"):
        create_venue(unknown, runtime())
```

- [ ] **Step 2: Run the registry tests and verify import failure**

Run:

```powershell
python -m pytest tests/test_venue_registry.py -q
```

Expected: collection fails because `entropy_arb.venues.registry` does not exist.

- [ ] **Step 3: Implement the explicit registry**

Create `entropy_arb/venues/registry.py`:

```python
"""Explicit venue factory registry; configuration cannot import code."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

import aiohttp

from ..config import VenueConf
from ..venue_hl import HLVenue
from ..venue_lighter import LighterVenue
from .base import VenueAdapter


@dataclass(frozen=True)
class VenueRuntime:
    session: aiohttp.ClientSession
    hl_api_url: str
    hl_ws_url: str
    settle_timeout_sec: float


VenueFactory = Callable[[VenueConf, VenueRuntime], VenueAdapter]


def _create_hl(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    return HLVenue(
        conf, runtime.hl_api_url, runtime.hl_ws_url,
        runtime.session, runtime.settle_timeout_sec)


def _create_lighter(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    return LighterVenue(
        conf, runtime.session, runtime.settle_timeout_sec)


ADAPTER_FACTORIES: Dict[str, VenueFactory] = {
    "hl": _create_hl,
    "lighter": _create_lighter,
}


def create_venue(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    try:
        factory = ADAPTER_FACTORIES[conf.kind]
    except KeyError as exc:
        raise ValueError(
            f"unsupported venue kind {conf.kind!r}") from exc
    venue = factory(conf, runtime)
    if not isinstance(venue, VenueAdapter):
        raise TypeError(
            f"adapter {type(venue).__name__} does not satisfy VenueAdapter")
    return venue
```

The runtime test intentionally passes `object()` as the session; constructors only store it, and no network request is made.

- [ ] **Step 4: Replace engine construction with the registry**

Remove concrete adapter imports from `engine.py`:

```python
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue
```

Add:

```python
from .venues.base import VenueAdapter
from .venues.registry import VenueRuntime, create_venue
```

Type the venue state in `Engine.__init__`:

```python
        self.entropy: Optional[VenueAdapter] = None
        self.hedge: Optional[VenueAdapter] = None
        self.venues: Dict[str, VenueAdapter] = {}
```

Delete `_make_venue`. At the start of `_run_inner`, construct the shared runtime and both adapters through the registry:

```python
        runtime = VenueRuntime(
            session=self.session,
            hl_api_url=cfg.hl_api_url,
            hl_ws_url=cfg.hl_ws_url,
            settle_timeout_sec=cfg.settle_timeout_sec,
        )
        self.entropy = create_venue(cfg.entropy, runtime)
        self.hedge = create_venue(cfg.hedge, runtime)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
```

Keep all subsequent market loading, signer setup, strategy, execution, recorder, dashboard, and shutdown code unchanged.

- [ ] **Step 5: Run registry, engine, and full tests**

Run:

```powershell
python -m pytest tests/test_venue_registry.py tests/test_engine.py -q
python -m pytest tests -q
```

Expected: registry tests report `3 passed`; full suite reports `43 passed`.

Check that `engine.py` no longer selects concrete adapters:

```powershell
rg -n 'HLVenue|LighterVenue|def _make_venue|kind == "hl"|kind == "lighter"' entropy_arb/engine.py
```

Expected: no matches.

- [ ] **Step 6: Commit registry-based creation**

```powershell
git add entropy_arb/venues/registry.py entropy_arb/engine.py tests/test_venue_registry.py
git commit -m "重构：通过注册表创建交易所适配器"
```

---

### Task 6: Update architecture documentation

**Files:**

- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Update the English directory structure**

In the README directory tree, add the new files without claiming multi-hedge support:

```text
entropy_arb/models.py    normalized order-result domain values
entropy_arb/venues/base.py  common venue adapter protocol
entropy_arb/venues/registry.py  explicit adapter factory registry
```

Add this paragraph immediately below the tree:

```markdown
The current CLI still runs exactly two legs. Venue construction now goes
through a common protocol and an explicit registry; this is the compatibility
foundation for the staged multi-hedge design in
`docs/superpowers/specs/2026-08-27-multi-hedge-arbitrage-design.md`.
```

- [ ] **Step 2: Mirror the documentation in Chinese**

Add these entries to the Chinese directory tree:

```text
entropy_arb/models.py    标准化订单结果数据结构
entropy_arb/venues/base.py  统一交易所适配器协议
entropy_arb/venues/registry.py  显式适配器工厂注册表
```

Add:

```markdown
当前命令行仍然只运行两条腿。交易所创建已经统一经过适配器协议和显式注册表；
这是后续按阶段实现多对冲交易所架构的兼容基础，完整设计见
`docs/superpowers/specs/2026-08-27-multi-hedge-arbitrage-design.md`。
```

- [ ] **Step 3: Verify documentation claims against the code**

Run:

```powershell
rg -n 'models.py|venues/base.py|venues/registry.py|multi-hedge-arbitrage-design' README.md README.zh-CN.md
```

Expected: both READMEs contain all four references. Confirm that neither README says multiple hedge venues are already live.

- [ ] **Step 4: Commit the living documentation update**

```powershell
git add README.md README.zh-CN.md
git commit -m "文档：说明交易所适配器基础架构"
```

---

### Task 7: Final verification and handoff

**Files:**

- Verify only; no planned code changes.

- [ ] **Step 1: Run the complete test suite from a clean process**

Run:

```powershell
python -m pytest tests -q
```

Expected: `43 passed`, zero failures, zero errors.

- [ ] **Step 2: Compile every Python entry point and package module**

Run:

```powershell
python -m compileall -q main.py entropy_arb tools tests
```

Expected: exit code `0` and no output.

- [ ] **Step 3: Verify the adapter boundary mechanically**

Run:

```powershell
rg -n 'HLVenue|LighterVenue|def _make_venue|kind == "hl"|kind == "lighter"' entropy_arb/engine.py
```

Expected: no matches.

Run:

```powershell
rg -n 'return \{"status"' entropy_arb/venue_hl.py entropy_arb/venue_lighter.py
rg -n 'info\.get\(|binfo\[|sinfo\[' entropy_arb/engine.py
```

Expected: neither command returns a match. Private websocket parsing dictionaries do not cross the adapter boundary.

- [ ] **Step 4: Inspect the final branch diff**

Run:

```powershell
git status --short
git diff --check main...HEAD
git diff --stat main...HEAD
```

Expected: no uncommitted files, no whitespace errors, and changes limited to the files listed in this plan.

- [ ] **Step 5: Record the verified baseline for Phase 2**

In the implementation handoff message, report:

- the exact test and compile commands with their exit status;
- the final test count;
- the branch name and commit list;
- confirmation that CLI/config/live strategy behavior did not intentionally change;
- the next planning boundary: Phase 2 multi-hedge market ingestion in `observe-only` mode.

Do not merge, push, or begin Phase 2 without a separate user instruction.
