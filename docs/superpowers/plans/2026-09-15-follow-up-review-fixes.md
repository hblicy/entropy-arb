# Follow-up Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复动态残差策略复审确认的分钟恢复、市场状态隔离、输出路径、连续回放、恢复审计和 CSV 尾行问题。

**Architecture:** 新增集中式市场路径派生模块，模型只按新分钟推进状态；SignalRecorder 在原 CSV 中补充中性 snapshot，回放器显式区分连续与旧版截断数据；execution_id 同时作为正常与恢复审计的幂等键。所有生产改动都由先失败的回归测试驱动。

**Tech Stack:** Python 3、asyncio、dataclasses、CSV/JSON、pytest。

---

### Task 1: 修正模型分钟推进与 warm start 去重

**Files:**
- Modify: `tests/test_strategy.py`
- Modify: `entropy_arb/strategy.py:116-151,231-314`

- [ ] **Step 1: 写同分钟不推进恢复的失败测试**

在 `tests/test_strategy.py` 增加：

```python
def test_same_minute_replacement_does_not_advance_regime_recovery():
    model = make_model()
    seed(model, [float(index % 5) for index in range(180)])
    seed(model, [None] * 5, start=180)
    assert model.snapshot(now_minute=184).status == "REGIME_UNSTABLE"

    for value in range(15):
        model.observe(minute=185, residual_bps=float(value), valid=True)
    assert model.snapshot(now_minute=185).status == "REGIME_UNSTABLE"

    seed(model, [2.0] * 14, start=186)
    assert model.snapshot(now_minute=199).status == "READY"
```

- [ ] **Step 2: 写 warm start 唯一分钟及闭合分钟失败测试**

```python
def test_warm_start_last_duplicate_wins_and_excludes_current_minute(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [
        history_row(198, residual="1"),
        history_row(198, residual="9"),
        history_row(199, residual="3"),
        history_row(200, residual="7"),
    ])
    model = make_model(window_minutes=10, min_samples=1,
                       regime_window_minutes=2, recovery_minutes=1)
    loaded = warm_start_residual_model(
        model, path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200, max_age_sec=15, max_skew_sec=15)

    snapshot = model.snapshot(now_minute=200)
    assert loaded.accepted == 2
    assert loaded.rejected_time == 1
    assert snapshot.samples == 2
    assert snapshot.median_bps == pytest.approx(6.0)
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `python -m pytest tests/test_strategy.py -q`

Expected: 两个新测试分别因同分钟提前恢复、当前分钟被接受或 accepted 计数仍按行统计而失败。

- [ ] **Step 4: 最小实现分钟语义**

在 `ResidualModel.observe()` 中保存 `is_new_latest = minute > previous_latest`；同分钟或历史替换仍更新 observation/version，但只有 `is_new_latest` 才执行 `_instant_unstable()` 和 `_stable_recovery` 逻辑。

在 `warm_start_residual_model()` 中用 `dict[int, float]` 保存接受的分钟，最后合法行覆盖前值；时间上限改为 `minute >= now_minute` 时拒绝。读取结束后把 `accepted` 设为合并后的字典长度，再按分钟排序观察。

- [ ] **Step 5: 运行局部测试并确认 GREEN**

Run: `python -m pytest tests/test_strategy.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "修复：按唯一闭合分钟推进残差模型"
```

### Task 2: 增加市场隔离路径和旧状态迁移门禁

**Files:**
- Create: `entropy_arb/runtime_paths.py`
- Create: `tests/test_runtime_paths.py`
- Modify: `entropy_arb/config.py:438-467`
- Modify: `entropy_arb/engine.py:492-507`
- Modify: `tests/test_config.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: 写路径派生失败测试**

在新测试文件中覆盖：相同身份确定性、不同身份不同路径、危险字符被清洗但哈希仍不同、live/shadow/pending/event 两两不同。

```python
def test_strategy_paths_are_market_and_mode_scoped(tmp_path):
    identity = MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh")
    live = strategy_paths(tmp_path / "campaign.json",
                          tmp_path / "events.csv", identity, shadow=False)
    shadow = strategy_paths(tmp_path / "campaign.json",
                            tmp_path / "events.csv", identity, shadow=True)
    assert live.campaign != live.pending
    assert live.campaign != shadow.campaign
    assert live.events != shadow.events
    assert "ANTH" in live.campaign.name
```

- [ ] **Step 2: 运行路径测试并确认 RED**

Run: `python -m pytest tests/test_runtime_paths.py -q`

Expected: FAIL，`entropy_arb.runtime_paths` 尚不存在。

- [ ] **Step 3: 实现集中式路径派生**

新增：

```python
@dataclass(frozen=True)
class StrategyPaths:
    campaign: Path
    pending: Path | None
    events: Path
    legacy_campaign: Path
    legacy_pending: Path | None

def _safe_piece(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return (cleaned or "market")[:24]

def market_scoped_path(path: str | Path, identity: MarketIdentity) -> Path:
    canonical = json.dumps(asdict(identity), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]
    readable = "--".join(_safe_piece(value) for value in (
        identity.entropy_dex, identity.entropy_symbol,
        identity.hedge_venue, identity.hedge_symbol))
    configured = Path(path)
    return configured.with_name(
        f"{configured.stem}.{readable}-{digest}{configured.suffix}")

def strategy_paths(campaign_path: str | Path, event_path: str | Path,
                   identity: MarketIdentity, *, shadow: bool) -> StrategyPaths:
    configured_campaign = Path(campaign_path)
    scoped_campaign = market_scoped_path(configured_campaign, identity)
    scoped_events = market_scoped_path(event_path, identity)
    if shadow:
        campaign = scoped_campaign.with_name(
            f"{scoped_campaign.stem}.shadow{scoped_campaign.suffix}")
        events = scoped_events.with_name(
            f"{scoped_events.stem}.shadow{scoped_events.suffix}")
        legacy = configured_campaign.with_name(
            f"{configured_campaign.stem}.shadow{configured_campaign.suffix}")
        return StrategyPaths(campaign, None, events, legacy, None)
    pending = scoped_campaign.with_name(
        f"{scoped_campaign.stem}.pending{scoped_campaign.suffix or '.json'}")
    legacy_pending = configured_campaign.with_name(
        f"{configured_campaign.stem}.pending"
        f"{configured_campaign.suffix or '.json'}")
    return StrategyPaths(
        scoped_campaign, pending, scoped_events,
        configured_campaign, legacy_pending)
```

市场标签由四个安全可读片段及规范 JSON 身份的 SHA-256 前 10 位组成；`strategy_paths()` 必须先做市场隔离，再派生 `.shadow` 或 `.pending`。

- [ ] **Step 4: 写最终输出碰撞失败测试**

在 `tests/test_config.py` 中构造 record-only 的 `recorder.csv == 最终 shadow campaign`，以及 live 的 `trades_csv == 最终 pending`，断言 `validate_output_paths()` 抛 `ConfigError`。

- [ ] **Step 5: 运行配置测试并确认 RED**

Run: `python -m pytest tests/test_config.py -q`

Expected: 新碰撞测试 FAIL，当前校验仍使用基础 state/event 路径。

- [ ] **Step 6: 让配置和引擎使用同一组最终路径**

`validate_output_paths()` 从 `cfg.entropy/cfg.hedge` 构造 `MarketIdentity`，调用 `strategy_paths()`，并按运行模式加入最终 campaign、pending、events。Engine 初始化同样只使用 `StrategyPaths`，不再直接用 `cfg.strategy_state_file` 派生 store。

- [ ] **Step 7: 写旧状态门禁失败测试**

在 `tests/test_engine.py` 覆盖 live legacy campaign、live legacy pending、record-only legacy shadow 三种文件存在场景，调用 `_initialize_dynamic_strategy()` 并断言异常同时包含旧路径和新路径；确认文件内容未改变。

- [ ] **Step 8: 实现旧状态门禁并确认 GREEN**

在 Engine 初始化 store 前调用小函数检查本模式的 legacy 路径。只要旧路径与新路径不同且旧文件存在，就抛带迁移说明的 `RuntimeError`；不移动、不删除。

Run: `python -m pytest tests/test_runtime_paths.py tests/test_config.py tests/test_engine.py -q`

Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add entropy_arb/runtime_paths.py entropy_arb/config.py entropy_arb/engine.py tests/test_runtime_paths.py tests/test_config.py tests/test_engine.py
git commit -m "修复：按市场隔离策略状态与审计路径"
```

### Task 3: 记录连续 snapshot 并修正回放覆盖语义

**Files:**
- Modify: `entropy_arb/recorder.py:478-835`
- Modify: `tools/replay_strategy.py:35-65,146-182,406-560`
- Modify: `tests/test_recorder.py`
- Modify: `tests/test_replay_strategy.py`

- [ ] **Step 1: 写无 raw signal 仍记录 snapshot 的失败测试**

复用 recorder 测试中的 venue/book helper，让两个方向都低于 fixed 阈值；连续两次 `observe()`，通过 monkeypatch 的 monotonic 时间跨过 `sample_sec`，断言只有中性 `snapshot` 行，且 direction 为空、四个最优价及正的 `crossable_notional_usd` 已写入。

- [ ] **Step 2: 运行 recorder 测试并确认 RED**

Run: `python -m pytest tests/test_recorder.py -q`

Expected: FAIL，当前无活跃 lifecycle 时不写行。

- [ ] **Step 3: 最小实现中性 snapshot**

为 SignalRecorder 增加 `_last_snapshot_mono`。提取公共盘口字段生成中性 row；每 `sample_sec` 队列一条 `event="snapshot"`、空 direction、唯一 event_id。`crossable_notional_usd` 取四个最优档 `price * size` 的最小值。`_seconds_until_next_sample()` 同时考虑 snapshot 和活跃 signal 的 due time；尾行校验允许 snapshot。

- [ ] **Step 4: 写回放覆盖失败测试**

在 `tests/test_replay_strategy.py` 增加：

```python
def test_replay_marks_legacy_signal_timeline_as_censored(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    signals = write_signals(tmp_path / "signals.csv", [
        signal_row(300_000, "sell_entropy", 45, "legacy-start")])
    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(), now_ts=400)
    assert result.timeline_complete is False
    assert "threshold-censored legacy" in result.approximation

def test_replay_uses_snapshot_coverage_end_instead_of_now_ts(tmp_path):
    minutes = write_minutes(tmp_path / "minutes.csv", [-20, 0, 20, 40])
    snapshot = signal_row(360_000, "sell_entropy", 45, "snapshot-1")
    snapshot.update(event="snapshot", direction="")
    signals = write_signals(tmp_path / "signals.csv", [snapshot])
    result = replay_files(
        minutes_path=str(minutes), signal_paths=[str(signals)],
        config=replay_config(), now_ts=600)
    assert result.timeline_complete is False
    assert result.coverage_end_ts == pytest.approx(360)
    assert result.requested_end_ts == pytest.approx(600)
```

同时增加 snapshot 不进入 raw direction coverage、snapshot 行允许空 direction 的测试。

- [ ] **Step 5: 运行回放测试并确认 RED**

Run: `python -m pytest tests/test_replay_strategy.py -q`

Expected: FAIL，ReplayResult 尚无覆盖字段且 loader 拒绝空 direction。

- [ ] **Step 6: 实现回放覆盖元数据**

`_load_signals()` 对 `event=snapshot` 要求 direction 为空，对 lifecycle 行仍要求合法方向。ReplayResult 增加 `requested_end_ts`、`coverage_end_ts`、`timeline_complete`；snapshot 存在时 approximation 为连续 top-of-book snapshot，缺少 snapshot 时标记 legacy censored。回放结束不使用最后盘口推进到 `now_ts`，CLI 输出实际截止时间和 incomplete 警告。raw coverage 只统计 lifecycle 行。

- [ ] **Step 7: 局部测试确认 GREEN**

Run: `python -m pytest tests/test_recorder.py tests/test_replay_strategy.py -q`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add entropy_arb/recorder.py tools/replay_strategy.py tests/test_recorder.py tests/test_replay_strategy.py
git commit -m "修复：记录连续盘口快照并标明回放覆盖"
```

### Task 4: 保护策略事件尾行并按 decision_id 去重

**Files:**
- Modify: `entropy_arb/strategy_recorder.py:77-129`
- Modify: `tests/test_strategy_recorder.py`

- [ ] **Step 1: 写残缺尾行归档失败测试**

先用 recorder 写一条合法记录，再以二进制追加不完整 CSV 片段。重启 recorder 后断言原文件被移动到 `events.csv.old`，新文件只有表头且归档内容逐字节保留。

- [ ] **Step 2: 写 decision_id 跨重启去重失败测试**

扩展测试 helper 接收 decision_id。第一次运行写 `execution-abc`，关闭重开后再次写同 ID，断言第二次 `record()` 返回 False，最终只有一行。

- [ ] **Step 3: 运行并确认 RED**

Run: `python -m pytest tests/test_strategy_recorder.py -q`

Expected: 残缺尾行被拼接或异常，重复 decision_id 被再次写入。

- [ ] **Step 4: 实现尾行验证和幂等 ID**

读取 header 后迭代完整 CSV，收集非空 decision_id 并保留最后一行；验证字段数、非负有限 ts_ms、非空 event。无效时使用 `next_archive_path()` 与 `os.replace()` 归档整个文件，然后创建新文件。`record()` 在写入前检查 `_decision_ids`，成功写入后再加入集合；重复 ID 返回 False。

- [ ] **Step 5: 运行并确认 GREEN**

Run: `python -m pytest tests/test_strategy_recorder.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add entropy_arb/strategy_recorder.py tests/test_strategy_recorder.py
git commit -m "修复：保护策略审计尾行并去重事件"
```

### Task 5: 补齐 pending execution 启动恢复事件

**Files:**
- Modify: `entropy_arb/engine.py:707-761,941-1026,1124-1215`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: 写正常与恢复路径同 ID 的失败测试**

扩展现有 pending recovery fixtures：正常 `_apply_live_matched_fill()` 后读取 event CSV，断言 decision_id 为 `execution-<id>`；模拟 campaign 已保存但事件缺失的 pending，运行 `_resolve_startup_pending_execution()`，断言补写 `campaign_changed` 或 `campaign_closed`；再次恢复同一 pending，断言仍只有一条相同 decision_id。

- [ ] **Step 2: 写恢复 CLOSE 分析字段失败测试**

构造带 `campaign_before` 的 CLOSE pending，双腿等量终态成交；恢复后断言事件为 `campaign_closed`，包含 campaign_id、filled_qty、双腿成交价、hold_seconds 和 realized_pnl_usd。

- [ ] **Step 3: 运行并确认 RED**

Run: `python -m pytest tests/test_engine.py -q`

Expected: 当前事件 ID 随机且启动恢复不写事件，因此新断言失败。

- [ ] **Step 4: 实现确定性 execution 审计**

给 `_record_dynamic_decision()` 增加可选 `decision_id`，默认仍生成 UUID。所有有 pending state 的 `_apply_live_matched_fill()` 调用传入 `execution-<pending.execution_id>`。提取一个内部 helper，根据 pending 计算与正常路径相同的事件类型和字段；`_resolve_startup_pending_execution()` 在 durable campaign 校验/保存后调用它。StrategyEventRecorder 负责抑制崩溃重试产生的相同 ID。

- [ ] **Step 5: 运行直接影响测试并确认 GREEN**

Run: `python -m pytest tests/test_engine.py tests/test_strategy_recorder.py tests/test_analyze.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "修复：补齐未决执行恢复审计事件"
```

### Task 6: 更新运行文档并执行完整验证

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `config.example.yaml` only if comments describe final paths

- [ ] **Step 1: 更新升级与回放说明**

文档明确：配置中的 state/event 是基础路径；展示实际市场隔离文件示例；升级时遇到旧状态启动错误必须先核对持仓再手工移动；新 `snapshot` 数据才具有连续回放时间轴；旧 signals 仍可读取但结果标记 censored；每日 gzip 行为不变。

- [ ] **Step 2: 运行完整验证**

Run: `python -m pytest -q`

Expected: 全部测试通过，无失败和错误。

Run: `python -m compileall -q entropy_arb main.py tools tests`

Expected: exit 0，无输出。

Run: `git diff --check HEAD~5..HEAD`

Expected: exit 0，无输出。

- [ ] **Step 3: 检查工作树与范围**

Run: `git status --short` 和 `git diff --stat HEAD~5..HEAD`

Expected: 仅包含本计划列出的代码、测试和文档；没有日志、凭据、采集数据或无关格式化。

- [ ] **Step 4: 提交文档**

```bash
git add README.md README.zh-CN.md config.example.yaml
git commit -m "文档：说明市场隔离状态与连续回放"
```

- [ ] **Step 5: 最终独立只读复审**

审查范围从本计划开始前提交到最终 HEAD，重点检查：实盘状态恢复、路径迁移门禁、snapshot 数据量、旧 CSV 兼容性、审计幂等及所有失败路径是否 fail-closed。发现问题时新增回归测试并重复 RED/GREEN，而不是直接修改实现。
