# 双腿硬额度与参考基差观测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保证每次套利计划的两条腿都不超过固定美元额度，并在不改变交易决策的前提下采集、记录和告警 Entropy oracle、对冲所 index、mark、资金费、参考基差与残差，同时让 `signals.csv` 按 UTC 日安全压缩轮转。

**Architecture:** 在 `reference.py` 中建立交易所无关的不可变参考快照、更新状态、派生指标和告警状态机；各 venue 负责 REST 原始数据解析，各 feed 在现有 WebSocket 上追加参考频道并写入同一状态。Engine 只负责启动独立恢复/告警协程，Recorder 读取快照写 CSV，参考链路任何可预期故障都不进入交易计划或订单调用链。

**Tech Stack:** Python 3.10+、asyncio、aiohttp、websockets、PyYAML、标准库 csv/gzip、pytest。

---

## 文件职责

- `entropy_arb/book.py`：逐档额度换算和双腿硬上限不变量。
- `entropy_arb/reference.py`：统一参考快照、原子状态更新、派生指标、状态化告警。
- `entropy_arb/feeds.py`：复用现有 WS 连接订阅并解析参考频道。
- `entropy_arb/venue_hl.py`、`entropy_arb/venue_lighter.py`：REST 初始化/恢复解析和参考状态所有权。
- `entropy_arb/venues/base.py`：声明 reference 与 REST 刷新能力。
- `entropy_arb/engine.py`：启动 REST 恢复和只记录日志的参考告警任务。
- `entropy_arb/config.py`、`config.example.yaml`：严格配置 schema 和默认值。
- `entropy_arb/recorder.py`：新 signal/minute 字段与分钟残差聚合。
- `entropy_arb/csv_rotation.py`：UTC 日轮转、gzip 校验、失败保留原始归档。
- `tools/analyze.py`：读取普通/压缩 CSV 并在新字段存在时输出参考统计。
- `tests/`：离线固定帧、模拟 HTTP、边界和回归测试。
- `README.md`、`README.zh-CN.md`：运行配置、字段语义和迁移说明。

## Task 1：修复双腿单笔美元硬上限

**Files:**
- Modify: `entropy_arb/book.py`
- Modify: `tests/test_book.py`

- [ ] **Step 1：先写会失败的多档和高溢价测试**

在 `tests/test_book.py` 增加三组断言：多档 ask 的平均买价上升、高价卖出腿、高负溢价反向调用。成功计划必须同时满足两个不变量。

```python
def assert_both_legs_capped(plan, cap):
    assert plan.buy_notional <= cap + 1e-9
    assert plan.sell_notional <= cap + 1e-9


def test_cap_uses_actual_multilevel_buy_notional():
    buy = make_book(bids=[(99, 10)], asks=[(100, 1), (120, 10)])
    sell = make_book(bids=[(130, 20)], asks=[(131, 20)])
    plan, reason = plan_arb(buy, sell, **common(cap_notional=500,
                                                size_step=0.01))
    assert reason == "ok"
    assert_both_legs_capped(plan, 500)


def test_cap_also_limits_more_expensive_sell_leg():
    buy = make_book(bids=[(99, 10)], asks=[(100, 10)])
    sell = make_book(bids=[(150, 10)], asks=[(151, 10)])
    plan, reason = plan_arb(buy, sell, **common(cap_notional=500,
                                                size_step=0.01))
    assert reason == "ok"
    assert plan.qty == 3.33
    assert_both_legs_capped(plan, 500)


def test_cap_holds_when_direction_is_reversed():
    buy = make_book(bids=[(149, 10)], asks=[(150, 10)])
    sell = make_book(bids=[(200, 10)], asks=[(201, 10)])
    plan, reason = plan_arb(buy, sell, **common(cap_notional=500,
                                                size_step=0.01))
    assert reason == "ok"
    assert_both_legs_capped(plan, 500)
```

- [ ] **Step 2：运行测试，确认旧算法失败**

Run: `python -m pytest tests/test_book.py -q`

Expected: 新增测试至少一项因 `buy_notional > 500` 或 `sell_notional > 500` 失败。

- [ ] **Step 3：实现逐档 quote budget 计算和一次精度纠偏**

在 `entropy_arb/book.py` 增加：

```python
def quantity_within_notional(levels: List[Level], cap_notional: float) -> float:
    remaining = cap_notional
    qty = 0.0
    for px, size in levels:
        if remaining <= 0.0:
            break
        take = min(size, remaining / px)
        qty += take
        remaining -= take * px
        if take < size:
            break
    return qty


def _notional_within_cap(value: float, cap: float) -> bool:
    return value <= cap + max(1e-9, abs(cap) * 1e-12)
```

把 `plan_arb()` 的 target 计算替换为：

```python
    target = min(
        q_max * take_fraction,
        quantity_within_notional(asks, cap_notional),
        quantity_within_notional(bids, cap_notional),
    )
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        target = floor_step(target - size_step, size_step)
        if target < min_base:
            return None, "below_min_base"
        buy_limit, buy_notional = walk_depth(asks, target)
        sell_limit, sell_notional = walk_depth(bids, target)
    if (not _notional_within_cap(buy_notional, cap_notional)
            or not _notional_within_cap(sell_notional, cap_notional)):
        raise ArithmeticError("planned leg notional exceeds cap")
```

- [ ] **Step 4：补齐取整与最小金额边界并运行测试**

增加 `size_step` 取整后低于 `min_base`、额度缩量后低于 `min_notional`、极小 step 不发生线性退步循环的测试。

Run: `python -m pytest tests/test_book.py -q`

Expected: PASS。

- [ ] **Step 5：提交额度修复**

```bash
git add entropy_arb/book.py tests/test_book.py
git commit -m "修复：严格限制双腿单笔额度"
```

## Task 2：建立统一参考模型和严格配置

**Files:**
- Create: `entropy_arb/reference.py`
- Modify: `entropy_arb/config.py`
- Modify: `config.example.yaml`
- Create: `tests/test_reference.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1：写快照验证、乱序保护和配置失败测试**

测试覆盖：有效价格/资金费、非有限值、非正价格、较旧 `exchange_ts_ms` 不覆盖、REST 更新不刷新 WS 新鲜度；配置默认值、未知 key、零恢复周期、负告警阈值和 NaN。

```python
def test_reference_state_rejects_invalid_and_out_of_order_updates():
    state = ReferenceState()
    first = ReferenceUpdate(oracle_px=100.0, exchange_ts_ms=2000)
    assert state.apply(first, source="websocket", received_mono=10.0)
    assert not state.apply(
        ReferenceUpdate(oracle_px=float("nan"), exchange_ts_ms=3000),
        source="websocket", received_mono=11.0)
    assert not state.apply(
        ReferenceUpdate(oracle_px=99.0, exchange_ts_ms=1999),
        source="rest", received_mono=12.0)
    assert state.snapshot.oracle_px == 100.0
    assert state.last_ws_received_mono == 10.0
```

- [ ] **Step 2：运行测试，确认模块/配置字段尚不存在**

Run: `python -m pytest tests/test_reference.py tests/test_config.py -q`

Expected: collection/import 或字段断言失败。

- [ ] **Step 3：实现不可变快照和原子更新状态**

`entropy_arb/reference.py` 使用以下公开结构；`apply()` 先在局部构造完整候选并校验，验证通过后才替换 `_snapshot`。

```python
@dataclass(frozen=True)
class MarketReference:
    oracle_px: Optional[float] = None
    index_px: Optional[float] = None
    mark_px: Optional[float] = None
    funding_current_bps_per_hour: Optional[float] = None
    funding_last_bps_per_hour: Optional[float] = None
    funding_last_ts_ms: Optional[int] = None
    exchange_ts_ms: Optional[int] = None
    received_mono: float = 0.0
    source: str = ""


@dataclass(frozen=True)
class ReferenceUpdate:
    oracle_px: Optional[float] = None
    index_px: Optional[float] = None
    mark_px: Optional[float] = None
    funding_current_bps_per_hour: Optional[float] = None
    funding_last_bps_per_hour: Optional[float] = None
    funding_last_ts_ms: Optional[int] = None
    exchange_ts_ms: Optional[int] = None


class InvalidReference(ValueError):
    pass


class ReferenceState:
    def __init__(self) -> None:
        self._snapshot = MarketReference()
        self.last_ws_received_mono = 0.0

    @property
    def snapshot(self) -> MarketReference:
        return self._snapshot
```

在该类继续实现 `apply(update, source, received_mono=None)`、`age_ms(now_mono=None)` 和 `ws_is_fresh(stale_sec, now_mono=None)`。`apply()` 合并 update 中非空字段，在局部变量上完成全部校验后用一次赋值替换 `_snapshot`；`source` 只接受 `websocket`/`rest`，价格必须 finite 且 `> 0`，资金费必须 finite，时间戳必须为非负整数。无效更新抛 `InvalidReference`，乱序更新返回 `False`。只有成功的 websocket 更新写 `last_ws_received_mono`。`age_ms()` 在没有快照时返回 `None`，否则返回不小于零的单调时钟差；`ws_is_fresh()` 只检查最后一次有效 websocket 更新，不能被 REST 更新刷新。

- [ ] **Step 4：扩展 Config 和严格 schema**

在 `Config` 增加：

```python
    reference_rest_recovery_sec: float
    reference_stale_sec: float
    reference_residual_alert_bps: float
    reference_residual_persist_sec: float
    recorder_signal_rotate_daily: bool
```

在 `_SCHEMA` 增加 `reference` 四字段，并给 recorder 加 `signal_rotate_daily`。`load_config()` 默认值分别为 `15.0/60.0/20.0/30.0/True`；恢复周期和 stale 必须 finite `> 0`，两个告警数值必须 finite `>= 0`。

在 `config.example.yaml` 增加经确认的默认块。

- [ ] **Step 5：运行聚焦测试并提交**

Run: `python -m pytest tests/test_reference.py tests/test_config.py -q`

Expected: PASS。

```bash
git add entropy_arb/reference.py entropy_arb/config.py config.example.yaml tests/test_reference.py tests/test_config.py
git commit -m "新增：统一参考行情模型与配置"
```

## Task 3：在现有 WebSocket 中接入参考频道

**Files:**
- Modify: `entropy_arb/feeds.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `tests/test_feeds.py`
- Create: `tests/test_venue_hl.py`
- Create: `tests/test_venue_lighter.py`

- [ ] **Step 1：写订阅、过滤、倍率和错误隔离测试**

固定官方形状的帧，不访问公网：

```python
HL_CTX = {
    "channel": "activeAssetCtx",
    "data": {"coin": "xyz:ANTH", "ctx": {
        "oraclePx": "100.2", "markPx": "100.3", "funding": "0.000032"}},
}
LIGHTER_STATS = {
    "type": "update/market_stats", "channel": "market_stats:32",
    "market_stats": {"index_price": "100.0", "mark_price": "100.1",
                     "current_funding_rate": "0.0012",
                     "funding_rate": "0.0008", "funding_timestamp": 1234,
                     "timestamp": 5678},
}
```

断言 HL `0.000032 * 10000 == 0.32 bps/h`；Lighter WS `0.0012% * 100 == 0.12 bps/h`。错误参考帧不得改变 `book.bids`、`book.asks`、`book.ready`、Lighter `_nonce/_synced`。

- [ ] **Step 2：运行测试，确认缺少订阅/解析**

Run: `python -m pytest tests/test_feeds.py tests/test_venue_hl.py tests/test_venue_lighter.py -q`

Expected: 参考频道订阅或 reference 快照断言失败。

- [ ] **Step 3：扩展两个 feed 构造器和消息分发**

两个 feed 新增 `reference_state: ReferenceState`。Lighter `_subscribe()` 连续发送：

```python
await ws.send(json.dumps({"type": "subscribe",
                          "channel": f"order_book/{self.market_id}"}))
await ws.send(json.dumps({"type": "subscribe",
                          "channel": f"market_stats/{self.market_id}"}))
```

HL 建连后连续发送：

```python
await ws.send(json.dumps({
    "method": "subscribe",
    "subscription": {"type": "l2Book", "coin": self.coin, "fast": True},
}))
await ws.send(json.dumps({
    "method": "subscribe",
    "subscription": {"type": "activeAssetCtx", "coin": self.coin},
}))
```

将协议字段转换集中在明确命名的 `parse_hl_asset_ctx(msg, coin)` 和 `parse_lighter_market_stats(msg, market_id)`。不属于目标 coin/market 的消息返回 `None`；属于目标的消息必须返回完整 `ReferenceUpdate` 或抛出可识别的 payload 错误。

只捕获 `(KeyError, TypeError, ValueError, InvalidReference)`，日志包含 venue/channel/market，不打印完整消息。参考帧不调用 `book.touch()`；订单簿心跳语义保持原样。

- [ ] **Step 4：让 venue 拥有 ReferenceState 并传给 feed**

在两个 venue 的 `__init__` 设置 `self.reference = ReferenceState()`，`start_tasks()` 创建 feed 时传入。不要增加 WebSocket 连接或交易通知事件。

- [ ] **Step 5：运行测试并提交**

Run: `python -m pytest tests/test_feeds.py tests/test_venue_hl.py tests/test_venue_lighter.py -q`

Expected: PASS。

```bash
git add entropy_arb/feeds.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py tests/test_feeds.py tests/test_venue_hl.py tests/test_venue_lighter.py
git commit -m "新增：复用行情连接采集参考数据"
```

## Task 4：实现 REST 初始化与 WS 失效恢复

**Files:**
- Modify: `entropy_arb/venues/base.py`
- Modify: `entropy_arb/venue_hl.py`
- Modify: `entropy_arb/venue_lighter.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_venue_contract.py`
- Modify: `tests/test_venue_hl.py`（Task 3 创建）
- Modify: `tests/test_venue_lighter.py`（Task 3 创建）
- Modify: `tests/test_engine.py`

- [ ] **Step 1：写 REST 单位、初始化和恢复调度测试**

覆盖：HL `metaAndAssetCtxs` 找到同一 universe 索引并转 bps；Lighter `orderBooks` 初始化 index/mark，`funding-rates` REST 小数费率乘 `10000`；启动失败只 warning；WS 超过 60 秒时每 15 秒恢复；REST 成功不能伪装 WS 新鲜；WS 更新后不再请求 REST。

Engine 测试使用 fake monotonic/time 和 stub venue，断言恢复协程不调用下单路径，HTTP 预期异常不设置全局 stop。

- [ ] **Step 2：运行测试，确认接口缺失**

Run: `python -m pytest tests/test_venue_contract.py tests/test_venue_hl.py tests/test_venue_lighter.py tests/test_engine.py -q`

Expected: `refresh_reference_rest` 或调度断言失败。

- [ ] **Step 3：给适配器添加 REST 刷新合同**

在 `VenueAdapter` 增加 `reference: ReferenceState` 属性，以及 `async refresh_reference_rest(self) -> bool` 方法合同；方法在应用有效 REST 快照时返回 `True`，无目标记录或乱序未更新时返回 `False`，协议实现不得用占位异常。

两个 venue 的 `load_market()` 在完成 market/coin 识别后调用一次 `refresh_reference_rest()`；只在该调用处捕获 `aiohttp.ClientError`、`asyncio.TimeoutError` 和参考 payload 错误，记录 warning 后继续市场加载。

- [ ] **Step 4：实现两个 REST 解析器**

使用独立命名的 `parse_hl_rest_asset_ctx(ctx)` 和 `parse_lighter_rest_market(ob, funding)`，避免混用倍率；前者只接受 HL REST ctx，后者只接受 Lighter order-book metadata 和可选 funding REST row。

Lighter WS 百分数字符串乘 `100`，REST funding 小数比例乘 `10000`；为两者保留独立测试。

- [ ] **Step 5：Engine 启动独立恢复任务**

新增：

```python
async def _reference_recovery_loop(self, venue) -> None:
    while not self.stop.is_set():
        if not venue.reference.ws_is_fresh(self.cfg.reference_stale_sec):
            try:
                await venue.refresh_reference_rest()
            except (aiohttp.ClientError, asyncio.TimeoutError,
                    InvalidReference) as exc:
                log.warning("[%s] reference REST recovery failed: %s",
                            venue.name, exc)
            delay = self.cfg.reference_rest_recovery_sec
        else:
            delay = min(self.cfg.reference_rest_recovery_sec,
                        self.cfg.reference_stale_sec)
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
```

将两个任务加入 Engine 现有监督 task 列表，命名 `reference-rest-<venue.key>`。不得捕获宽泛 `Exception`；未预期错误保留调用链。

- [ ] **Step 6：运行测试并提交**

Run: `python -m pytest tests/test_venue_contract.py tests/test_venue_hl.py tests/test_venue_lighter.py tests/test_engine.py -q`

Expected: PASS。

```bash
git add entropy_arb/venues/base.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py entropy_arb/engine.py tests/test_venue_contract.py tests/test_venue_hl.py tests/test_venue_lighter.py tests/test_engine.py
git commit -m "新增：参考行情 REST 初始化与恢复"
```

## Task 5：实现参考派生指标和状态化告警

**Files:**
- Modify: `entropy_arb/reference.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_reference.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1：写两个方向、缺失值、持续和恢复测试**

```python
def test_reference_metrics_preserve_direction_signs():
    metrics = calculate_reference_metrics(
        direction="sell_entropy",
        entropy_bid=101.0, entropy_ask=101.2,
        hedge_bid=99.8, hedge_ask=100.0,
        entropy=MarketReference(oracle_px=100.5,
                                funding_current_bps_per_hour=0.3),
        hedge=MarketReference(index_px=100.0,
                              funding_current_bps_per_hour=0.1),
    )
    assert metrics.reference_basis_bps == pytest.approx(50.0)
    assert metrics.signed_executable_premium_bps == pytest.approx(100.0)
    assert metrics.signed_residual_bps == pytest.approx(50.0)
    assert metrics.residual_edge_bps == pytest.approx(50.0)
    assert metrics.net_funding_bps_per_hour == pytest.approx(0.2)
```

另测 `buy_entropy` 使用 `(entropy_ask / hedge_bid - 1)*1e4` 且 residual edge 取负，缺任一 oracle/index/funding 时相应结果为 `None`。告警状态机在 29.9 秒不告警、30 秒告警一次、持续不重复、回阈值内恢复一次；stale/recovered 同理。

- [ ] **Step 2：运行测试，确认函数和状态机缺失**

Run: `python -m pytest tests/test_reference.py tests/test_engine.py -q`

Expected: import/属性失败。

- [ ] **Step 3：实现纯计算和状态机**

`reference.py` 增加：

```python
@dataclass(frozen=True)
class ReferenceMetrics:
    reference_basis_bps: Optional[float]
    signed_executable_premium_bps: Optional[float]
    signed_residual_bps: Optional[float]
    residual_edge_bps: Optional[float]
    net_funding_bps_per_hour: Optional[float]


def calculate_reference_metrics(*, direction: str, entropy_bid: float,
                                entropy_ask: float, hedge_bid: float,
                                hedge_ask: float,
                                entropy: MarketReference,
                                hedge: MarketReference) -> ReferenceMetrics:
    if direction not in {"sell_entropy", "buy_entropy"}:
        raise ValueError(f"unknown direction {direction!r}")
    basis = None
    if entropy.oracle_px is not None and hedge.index_px is not None:
        basis = (entropy.oracle_px / hedge.index_px - 1.0) * 1e4
    signed_premium = ((entropy_bid / hedge_ask - 1.0) * 1e4
                      if direction == "sell_entropy"
                      else (entropy_ask / hedge_bid - 1.0) * 1e4)
    signed_residual = None if basis is None else signed_premium - basis
    residual_edge = (signed_residual if direction == "sell_entropy"
                     else (-signed_residual
                           if signed_residual is not None else None))
    funding = None
    if (entropy.funding_current_bps_per_hour is not None
            and hedge.funding_current_bps_per_hour is not None):
        funding = (entropy.funding_current_bps_per_hour
                   - hedge.funding_current_bps_per_hour)
        if direction == "buy_entropy":
            funding = -funding
    return ReferenceMetrics(basis, signed_premium, signed_residual,
                            residual_edge, funding)


@dataclass(frozen=True)
class ReferenceAlertEvent:
    kind: str
    active: bool
    direction: Optional[str] = None
    value_bps: Optional[float] = None


class ReferenceAlertState:
    """Constructor stores alert/persistence thresholds and per-key state."""
```

实现 `observe(now_mono, sell_residual_bps, buy_residual_bps, stale)` 并返回事件列表。状态按方向独立跟踪 residual 候选起点/active，stale 单独跟踪；阈值判断使用 `abs(signed_residual)`，日志事件携带方向和值。候选值回到阈值内时清掉计时；active 回到阈值内才发一次 recovery。

- [ ] **Step 4：Engine 每秒观测但不参与交易判断**

新增 `_reference_monitor_loop()`，只读取 books/reference、调用状态机并记录 start/recovery 日志；不写 `plan_arb()` 输入、不设置 stop、不调用通知/下单。

测试同一盘口/阈值下启用参考任务前后的 `plan_arb` 方向、qty（额度修复允许缩小）一致，并确认 stale reference 不改变交易 eligibility。

- [ ] **Step 5：运行测试并提交**

Run: `python -m pytest tests/test_reference.py tests/test_engine.py -q`

Expected: PASS。

```bash
git add entropy_arb/reference.py entropy_arb/engine.py tests/test_reference.py tests/test_engine.py
git commit -m "新增：参考残差计算与状态化告警"
```

## Task 6：扩展 signals/minutes CSV 且保留 schema 迁移

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_recorder.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1：写新表头、空值和残差聚合测试**

断言 `SIGNAL_HEADER` 按设计追加 18 个字段，`HEADER` 包含参考 close、age/skew、basis、funding diff 和 residual OHLC/mean/std。测试：

- signal 两方向符号与 `reference.calculate_reference_metrics()` 一致；
- reference 缺失时字段为空但原 signal 行仍写；
- minute 原订单簿样本始终计入，只有有效 reference 样本计入 residual；
- 整分钟无参考时 residual 列为空；
- 旧 header 仍移动到 `.old`/`.old.1`，新文件表头完整。

- [ ] **Step 2：运行测试，确认 schema 失败**

Run: `python -m pytest tests/test_recorder.py tests/test_engine.py -q`

Expected: header/row 字段断言失败。

- [ ] **Step 3：扩展 SignalRecorder 快照**

读取 `self.entropy.reference.snapshot` 和 `self.hedge.reference.snapshot`，通过统一函数计算指标。年龄/偏差均使用 `time.monotonic()`：

```python
def _blank_if_none(value):
    return "" if value is None else value

e_ref = self.entropy.reference.snapshot
h_ref = self.hedge.reference.snapshot
metrics = calculate_reference_metrics(
    direction=direction, entropy_bid=e_bid, entropy_ask=e_ask,
    hedge_bid=h_bid, hedge_ask=h_ask,
    entropy=e_ref, hedge=h_ref)
```

mark 只记录，不回填 oracle/index；保留原 `top_edge_bps` 和 lifecycle 状态。

- [ ] **Step 4：扩展 _MinuteAgg 的独立 residual 样本计数**

为 reference close 字段保存最后一个有效参考快照；为 residual 使用独立 `r_n/r_open/r_high/r_low/r_close/r_sum/r_sumsq`。`n` 仍表示原订单簿 fresh 样本数，不能因为 reference 缺失降低或丢弃分钟行。

固定 `funding_diff_close_bps_per_hour = entropy current - hedge current`，不按 signal 方向翻转。

- [ ] **Step 5：Engine 传入 reference 对象和 rotation 配置**

MinuteRecorder 构造器增加两个 `ReferenceState`；SignalRecorder 使用 venue 已有状态；现有路径与 fail-fast 语义保持。

- [ ] **Step 6：运行测试并提交**

Run: `python -m pytest tests/test_recorder.py tests/test_engine.py -q`

Expected: PASS。

```bash
git add entropy_arb/recorder.py entropy_arb/engine.py tests/test_recorder.py tests/test_engine.py
git commit -m "新增：记录参考基差残差与资金费"
```

## Task 7：实现 signals.csv UTC 日轮转和安全 gzip

**Files:**
- Create: `entropy_arb/csv_rotation.py`
- Modify: `entropy_arb/recorder.py`
- Create: `tests/test_csv_rotation.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1：写跨日、命名冲突和压缩失败测试**

测试默认 `logs/signals.csv -> logs/signals-20260910.csv.gz`，自定义 `data/anth.csv -> data/anth-20260910.csv.gz`，冲突生成 `signals-20260910.csv.gz.1`。模拟 gzip 写入异常后断言 `signals-20260910.csv` 原始归档仍在、新 `signals.csv` 有正确 header 且可以继续写。

- [ ] **Step 2：运行测试，确认轮转模块不存在**

Run: `python -m pytest tests/test_csv_rotation.py tests/test_recorder.py -q`

Expected: import/归档文件断言失败。

- [ ] **Step 3：实现安全压缩发布函数**

`csv_rotation.py` 定义不可变 `RotationResult(archive_path: str, compressed: bool)`，公开入口为 `rotate_csv_gzip(path: str, utc_day: date) -> RotationResult`；实现中不得保留占位异常。

实现顺序必须是：选择未占用 raw/gz 名称 → `os.replace(path, raw)` → 同目录唯一 temp gz → 流式复制 → flush/close → 从头完整读取 gzip 校验 → `os.replace(temp, final_gz)` → 删除 raw。压缩/校验失败捕获 `OSError`/`gzip.BadGzipFile`，清理仅限本次 temp，保留 raw 并返回 `compressed=False`；不要删除历史文件。

- [ ] **Step 4：在 SignalRecorder 写行前检查 UTC 日期**

记录 `self._current_utc_day`。打开已有 `signals.csv` 时从最后完整数据行恢复它；新文件在写第一行时建立它。日期取最后已写入记录对应的 `datetime.fromtimestamp(ts_ms/1000, timezone.utc).date()`。新行属于下一日且 `signal_rotate_daily` 为真时：flush/close → rotate → `_open()` 新文件 → 写新行。正常 `close()` 不轮转。

归档失败只指 gzip 阶段可恢复；当前 CSV 的序列化、flush、close 继续 fail-fast。

- [ ] **Step 5：运行测试并提交**

Run: `python -m pytest tests/test_csv_rotation.py tests/test_recorder.py -q`

Expected: PASS。

```bash
git add entropy_arb/csv_rotation.py entropy_arb/recorder.py tests/test_csv_rotation.py tests/test_recorder.py
git commit -m "新增：按 UTC 日安全轮转信号日志"
```

## Task 8：让分析器兼容 gzip 并输出新参考统计

**Files:**
- Modify: `tools/analyze.py`
- Modify: `tests/test_analyze.py`

- [ ] **Step 1：写旧 CSV、新 CSV、gzip 三类测试**

旧文件输出保持 premium/threshold；新文件有非空字段时额外输出 `reference basis`、`signed residual`、`funding difference` 的 mean/std/median/p5/p95；无有效参考值时不打印伪统计。相同内容 `.csv` 和 `.csv.gz` 的 `load_rows()` 结果相同。

- [ ] **Step 2：运行测试，确认 gzip 或统计失败**

Run: `python -m pytest tests/test_analyze.py -q`

Expected: `.gz` 解码或新统计断言失败。

- [ ] **Step 3：按后缀选择文本读取器并可选解析新列**

```python
import gzip


def open_csv_text(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, newline="", encoding="utf-8")
```

`load_rows()` 对新字段使用可选有限 float 解析。参考统计只使用每分钟 close 字段，因此合并重复分钟时与现有 premium close 一样取最后一行，不需要新增 `residual_samples`，也不能拿订单簿 `samples` 对残差做错误加权。

- [ ] **Step 4：抽取统一分布打印函数**

```python
def describe(values: list[float]) -> tuple[float, float, float, float, float]:
    ordered = sorted(values)
    mean = sum(ordered) / len(ordered)
    std = math.sqrt(sum((x - mean) ** 2 for x in ordered) / len(ordered))
    return mean, std, pctl(ordered, 50), pctl(ordered, 5), pctl(ordered, 95)
```

资金费保持 `bps/hour`，只报告实测分布，不自动乘 1–6 小时或改变建议阈值。

- [ ] **Step 5：运行测试并提交**

Run: `python -m pytest tests/test_analyze.py -q`

Expected: PASS。

```bash
git add tools/analyze.py tests/test_analyze.py
git commit -m "增强：分析压缩数据与参考指标"
```

## Task 9：更新文档并完成全量验证

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-09-10-reference-observability-and-hard-cap-design.md`（仅当实现发现并确认了规格勘误）

- [ ] **Step 1：更新用户文档**

说明：

- `max_order_notional_usd` 是每条腿实际计划名义金额硬上限；
- reference 的四个配置项和 `signal_rotate_daily`；
- WebSocket 主、REST 初始化/恢复且参考异常不阻止交易；
- Lighter/HL 资金费统一为 bps/hour；
- 新 CSV 字段、旧 schema `.old` 迁移、UTC gzip 命名；
- `python tools/analyze.py --csv logs/minutes.csv` 和直接分析 `.csv.gz` 示例；
- 上线前仍先 `--record-only` 检查 reference 字段和日志，不把本次改动表述为无风险实盘许可。

- [ ] **Step 2：运行聚焦测试集合**

Run:

```bash
python -m pytest tests/test_book.py tests/test_reference.py tests/test_config.py tests/test_feeds.py tests/test_venue_contract.py tests/test_venue_hl.py tests/test_venue_lighter.py tests/test_recorder.py tests/test_csv_rotation.py tests/test_analyze.py tests/test_engine.py -q
```

Expected: PASS，无 warning 被当作 error。

- [ ] **Step 3：运行完整测试和编译检查**

Run:

```bash
python -m pytest -q
python -m py_compile entropy_arb/book.py entropy_arb/reference.py entropy_arb/feeds.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py entropy_arb/venues/base.py entropy_arb/engine.py entropy_arb/config.py entropy_arb/recorder.py entropy_arb/csv_rotation.py tools/analyze.py
```

Expected: 全部 PASS，`py_compile` 无输出且退出码 0。

- [ ] **Step 4：检查范围和敏感信息**

Run:

```bash
git status --short
git diff --check
git diff --stat origin/codex/multi-hedge-foundation...HEAD
git diff origin/codex/multi-hedge-foundation...HEAD -- . ':!docs/superpowers/specs/*' ':!docs/superpowers/plans/*'
```

Expected: 只有计划内文件；无凭据、Token、用户日志数据；交易阈值、方向、仓位、冷却和下单调用链除硬额度缩量外没有变化。

- [ ] **Step 5：提交文档**

```bash
git add README.md README.zh-CN.md
git commit -m "文档：说明参考观测与日志轮转"
```

- [ ] **Step 6：按完成前验证技能复核结果**

使用 `superpowers:verification-before-completion`，重新运行其要求的最新验证命令并保存实际退出码；之后使用 `superpowers:requesting-code-review` 做独立只读复核。发现问题时回到对应 Task 的红灯测试，不直接宣称完成。

## 完成定义

- 所有成功 `ArbPlan` 同时满足 `buy_notional`、`sell_notional <= max_order_notional_usd`。
- WS/REST 两种协议单位均由固定测试锁定，错误和乱序不污染最后有效参考状态。
- 参考 stale/残差日志状态化且永不进入交易判断。
- 新旧 CSV、gzip 分析、schema 归档和跨日轮转都有回归测试。
- 完整 pytest、编译检查、`git diff --check` 在最终提交上通过。
