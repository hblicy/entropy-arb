# Dynamic Strategy Audit Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 pending execution 的正常/恢复审计完全确定，并补齐回放覆盖起点、损坏状态校验、无扩展名路径和策略事件流式检查。

**Architecture:** pending schema v3 新增严格的不可变审计上下文和首次确认双腿终态的 `settled_at`，Engine 的三条终态路径统一从该状态构造策略事件。回放显式区分 legacy、mixed 和完整 snapshot 区间；路径与 CSV 检查只做针对性修复，不改变交易策略或交易所接口。

**Tech Stack:** Python 3、dataclasses、JSON/CSV、asyncio、pytest。

---

### Task 1: 定义并验证 pending schema v3

**Files:**
- Modify: `entropy_arb/recovery_state.py:16-254`
- Modify: `tests/test_recovery_state.py`
- Modify: `tests/test_engine.py` fixtures constructing `PendingExecutionState`

- [ ] **Step 1: 写 schema v3 round-trip 与 v2 fail-closed 失败测试**

在 `tests/test_recovery_state.py` 增加 `audit_context()` fixture，并让 `pending_state()` 带 `audit` 与 `settled_at=None`。新增：

```python
def test_pending_store_round_trips_v3_audit_context(tmp_path):
    state = pending_state()
    path = tmp_path / "campaign.pending.json"
    PendingExecutionStore(path).save(state)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 3
    assert raw["pending_execution"]["audit"]["reason"] == "RESIDUAL_EXIT_TARGET"
    assert PendingExecutionStore(path).load() == state


def test_pending_store_rejects_v2_without_modifying_file(tmp_path):
    path = tmp_path / "campaign.pending.json"
    payload = {"schema_version": 2, "pending_execution": None}
    original = json.dumps(payload)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(PendingExecutionStateError,
                       match=r"schema_version 2.*manual verification"):
        PendingExecutionStore(path).load()
    assert path.read_text(encoding="utf-8") == original
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_recovery_state.py -q`

Expected: `PendingAuditContext`、schema v3 和新字段尚不存在而失败。

- [ ] **Step 3: 实现 v3 数据结构和严格数值校验**

在 `recovery_state.py` 增加：

```python
SCHEMA_VERSION = 3

@dataclass(frozen=True)
class PendingAuditContext:
    reason: str
    signed_residual_bps: Optional[float]
    reference_basis_bps: Optional[float]
    convergence_bps: Optional[float]
    round_trip_fee_bps: Optional[float]
    buy_slippage_budget_bps: Optional[float]
    sell_slippage_budget_bps: Optional[float]
    planned_notional_usd: float
    projected_net_bps: Optional[float]
    projected_net_usd: Optional[float]
    estimated_campaign_pnl_usd: Optional[float]
    entropy_reference_age_ms: Optional[float]
    hedge_reference_age_ms: Optional[float]
    reference_update_skew_ms: Optional[float]
    net_funding_bps_per_hour: Optional[float]

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise PendingExecutionStateError("audit.reason must not be empty")
        _finite(
            "audit.planned_notional_usd", self.planned_notional_usd,
            positive=True)
        for name in (
            "signed_residual_bps", "reference_basis_bps",
            "projected_net_bps", "projected_net_usd",
            "estimated_campaign_pnl_usd", "net_funding_bps_per_hour",
        ):
            value = getattr(self, name)
            if value is not None:
                _signed_finite(f"audit.{name}", value)
        for name in (
            "convergence_bps", "round_trip_fee_bps",
            "buy_slippage_budget_bps", "sell_slippage_budget_bps",
            "entropy_reference_age_ms", "hedge_reference_age_ms",
            "reference_update_skew_ms",
        ):
            value = getattr(self, name)
            if value is not None:
                _finite(f"audit.{name}", value)
```

`PendingExecutionState` 增加 `audit: PendingAuditContext` 和 `settled_at: Optional[float]`。loader 严格验证 audit 字段集合并构造 dataclass。`PendingExecutionStore.load()` 的版本错误包含 `self.path`、实际版本和 `manual verification required`。

- [ ] **Step 4: 写 pending 结构不变量失败测试**

分别使用 `dataclasses.replace()` 覆盖：

```python
@pytest.mark.parametrize("buy_key,sell_key", [
    ("foo", "hedge"), ("entropy", "bar"),
])
def test_pending_requires_exact_venue_pair(buy_key, sell_key):
    state = pending_state()
    with pytest.raises(PendingExecutionStateError, match="entropy and hedge"):
        replace(
            state,
            buy=replace(state.buy, venue_key=buy_key),
            sell=replace(state.sell, venue_key=sell_key),
        )


def test_terminal_pending_requires_settled_at():
    state = pending_state()
    terminal_buy = replace(
        state.buy, status="filled", filled_base=1.0, applied_fill=1.0,
        unresolved=False)
    with pytest.raises(PendingExecutionStateError, match="settled_at"):
        replace(state, buy=terminal_buy)


def test_unresolved_pending_rejects_settled_at():
    with pytest.raises(PendingExecutionStateError, match="settled_at"):
        replace(pending_state(), settled_at=1020.0)


def test_pending_fill_must_not_exceed_requested_qty():
    state = pending_state()
    oversized = replace(
        state.buy, filled_base=1.1, applied_fill=1.1, avg_px=100.0)
    with pytest.raises(PendingExecutionStateError, match="requested qty"):
        replace(state, buy=oversized)


def test_pending_direction_must_match_leg_sides():
    with pytest.raises(PendingExecutionStateError, match="leg direction"):
        replace(pending_state(), direction="sell_entropy")


def test_close_direction_must_reverse_campaign():
    state = pending_state()
    with pytest.raises(PendingExecutionStateError, match="reverse campaign"):
        replace(
            state,
            direction="sell_entropy",
            buy=replace(state.buy, venue_key="hedge"),
            sell=replace(state.sell, venue_key="entropy"),
        )
```

预期分别匹配 `venue`, `settled_at`, `qty`, `direction`。

- [ ] **Step 5: 运行并确认 RED**

Run: `python -m pytest tests/test_recovery_state.py -q`

Expected: 当前仅验证两 venue 不同，没有上述不变量，测试失败。

- [ ] **Step 6: 实现 fail-closed 不变量**

在 `PendingExecutionState.__post_init__()` 中：

```python
if {self.buy.venue_key, self.sell.venue_key} != {"entropy", "hedge"}:
    raise PendingExecutionStateError(
        "pending legs must be exactly entropy and hedge")
expected_buy = "entropy" if self.direction == "buy_entropy" else "hedge"
expected_sell = "hedge" if self.direction == "buy_entropy" else "entropy"
if (self.buy.venue_key, self.sell.venue_key) != (expected_buy, expected_sell):
    raise PendingExecutionStateError("pending leg direction is inconsistent")
if any(leg.filled_base > self.qty + 1e-12 for leg in (self.buy, self.sell)):
    raise PendingExecutionStateError("pending fill exceeds requested qty")
terminal = not self.buy.unresolved and not self.sell.unresolved
if terminal != (self.settled_at is not None):
    raise PendingExecutionStateError(
        "settled_at is required exactly when both legs are terminal")
if self.settled_at is not None:
    _finite("settled_at", self.settled_at)
if self.intent == "ADD" and self.direction != self.campaign_before.direction:
    raise PendingExecutionStateError("ADD direction must match campaign")
if (self.intent in {"CLOSE", "FORCED_CLOSE"}
        and self.direction == self.campaign_before.direction):
    raise PendingExecutionStateError("close direction must reverse campaign")
```

初始 `sending` 两腿保持 unresolved，因此 `settled_at=None` 合法。同步更新所有 test fixtures，不能降低生产校验。

- [ ] **Step 7: 运行并确认 GREEN**

Run: `python -m pytest tests/test_recovery_state.py tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add entropy_arb/recovery_state.py tests/test_recovery_state.py tests/test_engine.py
git commit -m "修复：升级未决执行状态为审计版本三"
```

### Task 2: 发单前持久化完整审计上下文

**Files:**
- Modify: `entropy_arb/engine.py:959-1044,2197-2288`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: 写 pending audit 与原始 decision 一致的失败测试**

扩展正常动态开仓测试，在 `_evaluate()` 发单后读取 pending：

```python
assert pending.audit.reason == decision_reason_from_event
assert pending.audit.signed_residual_bps is not None
assert pending.audit.reference_basis_bps is not None
assert pending.audit.planned_notional_usd > 0
assert pending.audit.projected_net_bps is not None
assert pending.audit.projected_net_usd == pytest.approx(
    pending.audit.planned_notional_usd * pending.audit.projected_net_bps / 1e4)
assert pending.audit.entropy_reference_age_ms is not None
assert pending.audit.hedge_reference_age_ms is not None
```

再构造固定 `StrategyDecision` 和 reference timestamps，直接调用新 helper，断言 funding 对 `buy_entropy` 正确反号。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_engine.py -q`

Expected: Engine 尚未构造 `PendingAuditContext`。

- [ ] **Step 3: 提取决策审计上下文构造器**

新增 `_pending_audit_context(decision)`，复用当前 `_record_dynamic_decision()` 中的参考年龄、skew、funding、planned/projected 计算：

```python
def _pending_audit_context(self, decision):
    plan = decision.plan
    planned = max(plan.buy_notional, plan.sell_notional)
    projected_bps = getattr(plan, "projected_net_bps", None)
    return PendingAuditContext(
        reason=decision.reason,
        signed_residual_bps=decision.signed_residual_bps,
        reference_basis_bps=decision.reference_basis_bps,
        convergence_bps=decision.convergence_bps,
        round_trip_fee_bps=decision.round_trip_fee_bps,
        buy_slippage_budget_bps=decision.buy_slippage_budget_bps,
        sell_slippage_budget_bps=decision.sell_slippage_budget_bps,
        planned_notional_usd=planned,
        projected_net_bps=projected_bps,
        projected_net_usd=(None if projected_bps is None
                           else planned * projected_bps / 1e4),
        estimated_campaign_pnl_usd=decision.estimated_campaign_pnl_usd,
        entropy_reference_age_ms=self.entropy.reference.age_ms(),
        hedge_reference_age_ms=self.hedge.reference.age_ms(),
        reference_update_skew_ms=reference_skew_ms,
        net_funding_bps_per_hour=net_funding_bps_per_hour,
    )
```

其中 `reference_skew_ms` 由两个 reference snapshot 的 `received_mono` 差值计算；`net_funding_bps_per_hour` 由两腿 current funding 差值计算，并在 `buy_entropy` 时反号。创建 `PendingExecutionState` 时调用该 helper，并设置 `settled_at=None`。该 durable write 仍必须发生在任一 submit coroutine 创建前。

- [ ] **Step 4: 确认发单前持久化顺序并 GREEN**

保留/扩展现有“pending save 失败不发送订单”测试，同时断言初始 pending 已有 audit 且 `settled_at is None`。

Run: `python -m pytest tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "修复：发单前持久化决策审计上下文"
```

### Task 3: 统一终态时间与策略事件构造

**Files:**
- Modify: `entropy_arb/engine.py:609-779,959-1044,1142-1237,2364-2460,2928-2995`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: 写正常与延迟终态 settled_at 失败测试**

使用 monkeypatch 固定 wall clock：decision 为 `1000.0`，两腿立即终态为 `1002.0`；另一路 unknown order 在 `1060.0` 才确认。断言：

```python
assert pending.settled_at == pytest.approx(1002.0)
assert event["ts_ms"] == "1002000"

assert delayed_pending.settled_at == pytest.approx(1060.0)
assert delayed_event["ts_ms"] == "1060000"
```

确认事件时间不再使用 `decided_at`。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_engine.py -q`

Expected: 当前事件使用 `pending.decided_at`，失败。

- [ ] **Step 3: 让终态结果与 settled_at 原子持久化**

扩展 `_pending_execution_with_results()`，增加必填 keyword 参数 `now_wall`：只有双腿均 terminal 时设置 `settled_at=pending.settled_at or max(now_wall, pending.decided_at)`；否则保持 null。调用方在同一次 `replace()` 后只做一次 atomic store save。

立即终态路径、进程内 unknown-order 恢复、启动 unresolved 恢复都必须先保存 terminal legs + settled_at，再调用 `_campaign_after_pending()`。

- [ ] **Step 4: 写正常/启动恢复完整事件等价失败测试**

先以正常执行产生 event；另一个临时目录保存相同 pending 的 terminal 状态但不写 event，模拟 campaign 已落盘后重启。忽略 CSV 行天然不同的文件位置，仅比较以下全部业务字段：

```python
AUDIT_FIELDS = {
    "ts_ms", "event", "intent", "reason", "decision_id", "campaign_id",
    "direction", "model_version", "model_samples", "model_status",
    "model_median_bps", "model_lower_bps", "model_upper_bps",
    "model_iqr_bps", "signed_residual_bps", "reference_basis_bps",
    "entry_boundary_bps", "exit_target_bps", "convergence_bps",
    "round_trip_fee_bps", "buy_slippage_budget_bps",
    "sell_slippage_budget_bps", "projected_net_bps", "projected_net_usd",
    "estimated_campaign_pnl_usd", "qty", "planned_notional_usd",
    "entropy_reference_age_ms", "hedge_reference_age_ms",
    "reference_update_skew_ms", "net_funding_bps_per_hour",
    "entropy_fill_px", "hedge_fill_px", "hold_seconds",
    "realized_pnl_usd",
}
assert {key: normal[key] for key in AUDIT_FIELDS} == {
    key: recovered[key] for key in AUDIT_FIELDS}
}
```

为 CLOSE 增加 `settled_at - opened_at` 的 hold 断言；重复恢复仍只有一个 decision_id。

- [ ] **Step 5: 运行并确认 RED**

Run: `python -m pytest tests/test_engine.py -q`

Expected: recovery 事件缺失 decision-time payload 或时间不同。

- [ ] **Step 6: 用 pending 直接构造 StrategyEvent**

将 `_record_pending_campaign_event()` 改为不创建残缺 `StrategyDecision`、不读取当前 reference。它从 `pending.audit`、`pending.frozen_model`、terminal legs、`campaign_before` 和 `settled_at` 直接构造完整 `StrategyEvent`。`campaign_status` 使用 campaign_after 在 settled_at 的状态；事件 ID 固定为 `execution-<execution_id>`。

正常、进程内 unknown-order 和启动恢复都调用同一 helper。保留非 execution 的 `_record_dynamic_decision()` 行为。

- [ ] **Step 7: 运行直接影响测试并确认 GREEN**

Run: `python -m pytest tests/test_engine.py tests/test_recovery_state.py tests/test_strategy_recorder.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "修复：统一成交终态时间与恢复审计事件"
```

### Task 4: 显式报告混合 signals 的连续覆盖起点

**Files:**
- Modify: `tools/replay_strategy.py:35-85,421-452,598-636,692-701`
- Modify: `tests/test_replay_strategy.py`

- [ ] **Step 1: 写 mixed prefix 失败测试**

构造 300 秒 lifecycle，360/361 秒 snapshots，requested end 361：

```python
result = replay_files(
    minutes_path=str(minutes),
    signal_paths=[str(signals)],
    config=replay_config(),
    now_ts=361,
)
assert result.coverage_start_ts == pytest.approx(360)
assert result.coverage_end_ts == pytest.approx(361)
assert result.censored_prefix is True
assert result.timeline_complete is False
assert "censored-prefix" in result.approximation
assert result.signal_rows == 2
```

再扩展 CLI 测试，断言输出包含 `coverage start` 和 `censored prefix: yes`。

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_replay_strategy.py -q`

Expected: ReplayResult 尚无新字段，且 mixed 可能报告 complete。

- [ ] **Step 3: 实现覆盖起点和 mixed 语义**

新增常量：

```python
MIXED_APPROXIMATION = (
    "censored-prefix continuous top-of-book snapshot approximation")
```

ReplayResult 增加 `coverage_start_ts: Optional[float]` 与 `censored_prefix: bool`。`censored_prefix` 仅在第一条 snapshot 前存在 lifecycle row 时为 true。策略指标仍只遍历第一条 snapshot 到 requested end；`timeline_complete` 额外要求 `not censored_prefix`。CLI 始终输出 start/end 和前缀状态。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python -m pytest tests/test_replay_strategy.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tools/replay_strategy.py tests/test_replay_strategy.py
git commit -m "修复：标明回放连续覆盖起点"
```

### Task 5: 修正无扩展名路径派生

**Files:**
- Modify: `entropy_arb/runtime_paths.py:30-77`
- Modify: `tests/test_runtime_paths.py`
- Run: `tests/test_config.py`, `tests/test_engine.py`

- [ ] **Step 1: 写 extensionless 失败测试**

```python
def test_strategy_paths_preserve_market_tag_before_markers_without_suffix(
        tmp_path):
    live = strategy_paths(tmp_path / "state", tmp_path / "events",
                          identity(), shadow=False)
    shadow = strategy_paths(tmp_path / "state", tmp_path / "events",
                            identity(), shadow=True)
    assert live.pending.name == f"{live.campaign.name}.pending.json"
    assert shadow.campaign.name == f"{live.campaign.name}.shadow"
    assert shadow.events.name == f"{live.events.name}.shadow"
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_runtime_paths.py -q`

Expected: 当前 `Path.suffix` 把市场标签误识别为扩展名。

- [ ] **Step 3: 按原始 suffix 派生 marker**

让 `_with_marker()` 接受显式 `original_suffix`：有 suffix 时在 suffix 前加入 marker；无 suffix 时把 marker 追加到完整文件名，并仅对 pending 添加 `.json`。

```python
def _with_marker(path, marker, *, original_suffix, default_suffix=""):
    if original_suffix:
        base = path.name[:-len(original_suffix)]
        return path.with_name(f"{base}.{marker}{original_suffix}")
    return path.with_name(f"{path.name}.{marker}{default_suffix}")
```

`strategy_paths()` 分别传入 `configured_campaign.suffix` 和 `Path(event_path).suffix`。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python -m pytest tests/test_runtime_paths.py tests/test_config.py tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add entropy_arb/runtime_paths.py tests/test_runtime_paths.py
git commit -m "修复：正确派生无扩展名策略路径"
```

### Task 6: 流式检查策略事件文件并记录归档

**Files:**
- Modify: `entropy_arb/strategy_recorder.py:1-135`
- Modify: `tests/test_strategy_recorder.py`

- [ ] **Step 1: 写“不使用 read_bytes”与 warning 失败测试**

```python
def test_existing_strategy_events_are_streamed_without_read_bytes(
        tmp_path, monkeypatch):
    path = tmp_path / "events.csv"
    with StrategyEventRecorder(path) as recorder:
        recorder.record(make_event(decision_id="execution-1"))
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: pytest.fail("read_bytes called"))
    with StrategyEventRecorder(path) as recorder:
        assert not recorder.record(make_event(decision_id="execution-1"))


def test_invalid_strategy_event_file_logs_archive_target(
        tmp_path, caplog):
    path = write_valid_then_partial(tmp_path / "events.csv")
    with StrategyEventRecorder(path):
        pass
    assert str(path) in caplog.text
    assert str(tmp_path / "events.csv.old") in caplog.text
```

- [ ] **Step 2: 运行并确认 RED**

Run: `python -m pytest tests/test_strategy_recorder.py -q`

Expected: 当前调用 `read_bytes()` 且无 warning。

- [ ] **Step 3: 实现二进制尾字节检查加文本流式 CSV 扫描**

使用二进制 seek 检查末字节：

```python
with self.path.open("rb") as raw:
    raw.seek(-1, os.SEEK_END)
    if raw.read(1) not in {b"\n", b"\r"}:
        return False, set()
```

随后 `with self.path.open("r", newline="", encoding="utf-8")`，严格 reader 逐行验证 header/字段/时间/event，并只累积 decision IDs。捕获 `UnicodeError` 和 `csv.Error` 返回 invalid。归档成功后调用：

```python
log.warning("invalid strategy event CSV archived: %s -> %s",
            self.path, archive)
```

不读取损坏归档中的 ID，不改变新活动审计文件的权威语义。

- [ ] **Step 4: 运行并确认 GREEN**

Run: `python -m pytest tests/test_strategy_recorder.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add entropy_arb/strategy_recorder.py tests/test_strategy_recorder.py
git commit -m "修复：流式校验策略事件文件"
```

### Task 7: 文档、兼容提示与完整验证

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `config.example.yaml` only if pending compatibility is described there

- [ ] **Step 1: 更新部署说明**

明确：

- 新代码只自动读取 schema v3 pending；v2 保留原文件并要求人工核对；
- `settled_at` 是引擎首次确认双腿终态的时间；
- mixed signals 的 coverage start 和 censored prefix 含义；
- 无 pending 的 record-only 升级流程不受影响。

- [ ] **Step 2: 运行最终完整验证**

Run: `python -m pytest -q`

Expected: 全部测试 PASS。

Run: `python -m compileall -q entropy_arb main.py tools tests`

Expected: exit 0，无输出。

Run: `git diff --check 88d36b3..HEAD`

Expected: exit 0，无输出。

- [ ] **Step 3: 范围与敏感文件检查**

Run:

```bash
git status --short
git diff --stat 88d36b3..HEAD
git diff --name-only 88d36b3..HEAD
```

Expected: 仅出现本计划列出的代码、测试和文档；不包含 `.env`、日志、CSV 数据、私钥或采集文件。

- [ ] **Step 4: 提交文档**

```bash
git add README.md README.zh-CN.md config.example.yaml
git commit -m "文档：说明未决执行版本三与回放覆盖"
```

- [ ] **Step 5: 最终只读复审**

从 `88d36b3` 到最终 HEAD 逐项检查：发单前 durable 顺序、终态时间只写一次、正常/恢复事件完全等价、v2 和损坏 pending fail-closed、mixed replay 不误报完整、扩展名路径兼容、CSV 检查不吞异常。发现问题时新增最小失败测试并重复 RED/GREEN。
