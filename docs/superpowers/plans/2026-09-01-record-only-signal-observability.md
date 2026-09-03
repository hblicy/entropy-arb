# Record-only Signal Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变实盘扫描、下单和风控行为的前提下，为 `--record-only` 增加方向独立、事件驱动并按秒采样的 `logs/signals.csv`，用于识别短暂价差和盘口更新时间错位。

**Architecture:** 在 `entropy_arb/recorder.py` 增加独立 `SignalRecorder`，直接读取两个 venue 的盘口和费率，复用 `plan_arb()` 计算可执行计划，只落 CSV、不调用下单接口。`Engine` 抽出记录器启动边界：分钟记录器保持原逻辑，信号记录器仅在 `record_only=True` 时启动，并复用现有行情更新事件加一秒定时唤醒。

**Tech Stack:** Python 3、asyncio、csv、pytest、PyYAML、现有 `OrderBook` / `plan_arb()`。

> **2026-09-04 完整性修订：** 后续实施计划
> `2026-09-04-recorder-data-integrity-fixes.md` 为分钟行补充市场身份，要求每个
> 市场使用独立文件，并让分析器拒绝混合市场；所有 CSV 采用不覆盖旧文件的递增
> 归档。`--record-only` 启动即验证分钟和信号输出，任一记录器 I/O 失败均停止并
> 上抛。Engine 清理时关闭全部交易所，且关闭异常不得覆盖更早的业务异常。

---

### Task 1: 扩展严格配置并保持向后兼容

**Files:**
- Modify: `entropy_arb/config.py`
- Modify: `tests/test_config.py`
- Modify: `config.example.yaml`

- [ ] **Step 1: 写配置默认值和严格 schema 的失败测试**

在 `tests/test_config.py` 的现有配置测试中增加以下断言：

```python
def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.recorder_signal_csv == "logs/signals.csv"


def test_recorder_signal_csv_can_be_overridden():
    cfg = load(
        MINIMAL + "\nrecorder:\n  signal_csv: data/custom-signals.csv\n",
    )
    assert cfg.recorder_signal_csv == "data/custom-signals.csv"
```

同时在 `test_example_config_loads` 中断言：

```python
assert cfg.recorder_signal_csv == "logs/signals.csv"
```

保留现有未知键测试，确保增加合法键后严格校验没有被放宽。

- [ ] **Step 2: 运行定向测试并确认失败**

Run: `python -m pytest tests/test_config.py -q`

Expected: FAIL，错误指出 `Config` 没有 `recorder_signal_csv`，或 `signal_csv` 被判定为未知键。

- [ ] **Step 3: 实现最小配置变更**

在 `Config` dataclass 的 recorder 字段旁增加：

```python
recorder_signal_csv: str
```

在 `_SCHEMA["recorder"]` 中加入 `signal_csv`，并在 `load_config()` 构造 `Config` 时加入：

```python
recorder_signal_csv=_get(
    raw, "recorder", "signal_csv", "logs/signals.csv"
),
```

在 `config.example.yaml` 中补齐：

```yaml
recorder:
  enabled: true
  csv: logs/minutes.csv
  signal_csv: logs/signals.csv
```

- [ ] **Step 4: 运行配置测试并确认通过**

Run: `python -m pytest tests/test_config.py -q`

Expected: PASS。

- [ ] **Step 5: 提交配置变更**

```bash
git add entropy_arb/config.py tests/test_config.py config.example.yaml
git commit -m "配置：增加信号记录文件路径"
```

### Task 2: 用 TDD 建立信号生命周期状态机

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1: 写 start/sample/end/shutdown 生命周期失败测试**

在 `tests/test_recorder.py` 增加本地 venue stub 和固定时间盘口 helper。helper 必须同时设置 `last_update_ts` 与 `alive_ts`，避免测试依赖系统时钟：

```python
class SignalVenue:
    def __init__(self, name: str, fee_bps: float = 0.0):
        self.name = name
        self.book = OrderBook()
        self.fee_bps = fee_bps


def set_signal_book(venue, *, bid, ask, ts):
    venue.book.apply_hl(
        [
            [{"px": str(bid), "sz": "100"}],
            [{"px": str(ask), "sz": "100"}],
        ]
    )
    venue.book.last_update_ts = ts
    venue.book.alive_ts = ts


def read_signal_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
```

构造记录器的测试 helper 使用固定参数：`midline_bps=0`、`upper_bps=5`、`lower_bps=5`、`take_fraction=1`、足够大的名义金额上限、最小量为 0、`size_step=0.001`、`leg_slippage_bps=20`、`staleness_sec=3`、`sample_sec=1`。

测试顺序：

```python
rec.observe(now=1000.0)   # sell_entropy edge 超线 -> start
rec.observe(now=1000.5)   # 不足 1 秒 -> 不写
rec.observe(now=1001.0)   # 仍有效 -> sample
set_signal_book(entropy, bid=100.00, ask=100.01, ts=1001.2)
rec.observe(now=1001.2)   # edge 消失 -> end
```

断言事件依次为 `start, sample, end`，方向均为 `sell_entropy`，同一 `event_id`，`elapsed_ms` 为 `0, 1000, 1200`，最后 `end_reason == "edge_below_threshold"`。

另写关闭测试：信号激活后调用 `rec.close(now=1002.0)`，断言新增 `end`、`end_reason == "shutdown"`，再次 `close()` 不重复写。

- [ ] **Step 2: 运行生命周期测试并确认失败**

Run: `python -m pytest tests/test_recorder.py -q -k "signal and (lifecycle or shutdown)"`

Expected: FAIL，`SignalRecorder` 尚不存在。

- [ ] **Step 3: 增加固定 schema 和方向状态**

在 `entropy_arb/recorder.py` 增加：

```python
SIGNAL_HEADER = [
    "ts_ms", "time_utc", "symbol", "entropy_dex", "hedge_venue",
    "event_id", "event", "direction",
    "elapsed_ms", "end_reason", "entropy_bid", "entropy_ask",
    "hedge_bid", "hedge_ask", "entropy_book_age_ms",
    "hedge_book_age_ms", "book_update_skew_ms", "top_edge_bps",
    "net_threshold_bps", "total_fee_bps", "plan_status", "qty",
    "buy_limit", "sell_limit", "planned_notional_usd",
    "crossable_notional_usd", "buy_depth_slippage_bps",
    "sell_depth_slippage_bps", "leg_slippage_limit_bps",
    "expected_edge_usd",
]


@dataclass
class _SignalState:
    event_id: str
    started_at: float
    last_written_at: float
```

`SignalRecorder` 保存两个方向的独立状态：

```python
self._states = {
    "sell_entropy": None,
    "buy_entropy": None,
}
```

`observe(now=None)` 对每个方向执行相同状态迁移：

1. 空盘口或超过 `staleness_sec`：结束已激活信号，原因分别为 `empty_book` / `stale_book`。
2. `sell_entropy` 使用 `buy=hedge`、`sell=entropy`、净阈值 `midline_bps + upper_bps`。
3. `buy_entropy` 使用 `buy=entropy`、`sell=hedge`、净阈值 `lower_bps - midline_bps`。
4. 使用与 `plan_arb()` 相同的费后顶层判定：

```python
qualifies = sell_bid * (1 - sell_fee_bps / 1e4) >= (
    buy_ask
    * (1 + buy_fee_bps / 1e4)
    * (1 + net_threshold_bps / 1e4)
)
```

5. 未激活且合格写 `start`；激活且合格、距上次落盘至少 `sample_sec` 写 `sample`；激活且不合格写 `end`。

事件编号固定为：

```python
event_id = f"{direction}-{int(now * 1000)}-{run_id}-{direction_seq}"
```

其中 `run_id` 是每个记录器实例创建时生成的 UUID，`direction_seq` 是该方向
在本次运行内递增的序号，避免同毫秒重入和跨运行追加时发生冲突。

`close(now=None)` 对仍激活的两个方向各写一次 `shutdown` end，关闭文件，并保持幂等。

- [ ] **Step 4: 运行生命周期测试并确认通过**

Run: `python -m pytest tests/test_recorder.py -q -k "signal and (lifecycle or shutdown)"`

Expected: PASS。

- [ ] **Step 5: 写双方向独立状态失败测试**

构造一次 `sell_entropy` 信号，随后把盘口切换为 `buy_entropy` 信号。断言第一方向写自己的 `end`，第二方向写新的 `start`，两者 `event_id` 不同，且方向前缀正确。再构造两个方向都不满足的盘口，断言各自结束状态互不覆盖。

- [ ] **Step 6: 运行测试、实现缺失状态转换并确认通过**

Run: `python -m pytest tests/test_recorder.py -q -k "signal and independent"`

Expected first run: FAIL；补足两个方向分别计算、分别维护 `_SignalState` 后 PASS。

- [ ] **Step 7: 提交生命周期实现**

```bash
git add entropy_arb/recorder.py tests/test_recorder.py
git commit -m "功能：记录价差信号生命周期"
```

### Task 3: 记录计划、深度、盘口时间和失败原因

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1: 写指标计算失败测试**

设置有多档深度的盘口与不同更新时间，例如 Entropy 更新于 `2000.8`、hedge 更新于 `2000.5`，在 `now=2001.0` 观察。让计划跨越多档后断言：

```python
assert float(row["entropy_book_age_ms"]) == pytest.approx(200)
assert float(row["hedge_book_age_ms"]) == pytest.approx(500)
assert float(row["book_update_skew_ms"]) == pytest.approx(300)
assert float(row["top_edge_bps"]) == pytest.approx(expected_top_edge)
assert float(row["net_threshold_bps"]) == 5
assert float(row["total_fee_bps"]) == pytest.approx(
    entropy.fee_bps + hedge.fee_bps
)
assert row["plan_status"] == "ok"
assert float(row["qty"]) == pytest.approx(expected_plan.qty)
assert float(row["planned_notional_usd"]) == pytest.approx(
    expected_plan.buy_notional
)
assert float(row["crossable_notional_usd"]) == pytest.approx(
    expected_plan.q_max_notional
)
assert float(row["buy_depth_slippage_bps"]) == pytest.approx(
    (expected_plan.buy_limit / buy_best_ask - 1) * 1e4
)
assert float(row["sell_depth_slippage_bps"]) == pytest.approx(
    (sell_best_bid / expected_plan.sell_limit - 1) * 1e4
)
assert float(row["expected_edge_usd"]) == pytest.approx(
    expected_plan.exp_edge_usd
)
```

额外断言盘口年龄来自 `last_update_ts`：把 `alive_ts` 改成更近的时间后，年龄字段仍不变。

- [ ] **Step 2: 写计划不足但仍记录的失败测试**

让顶层 edge 合格，但把 `min_notional` 设置得高于可成交金额。断言仍写 `start`，`plan_status == "below_min_notional"`，计划专属数值字段为空字符串，盘口、edge、阈值和费率字段仍完整。

- [ ] **Step 3: 运行定向测试并确认失败**

Run: `python -m pytest tests/test_recorder.py -q -k "signal and (metrics or below_min)"`

Expected: FAIL，指标或计划原因尚未写入。

- [ ] **Step 4: 复用 `plan_arb()` 构造每行快照**

为每个方向调用现有 `plan_arb()`，传入与引擎一致的：盘口、买卖费率、`net_threshold_bps`、`take_fraction`、`max_order_notional`、`min_base`、`min_notional`、`size_step`。`leg_slippage_bps` 只原样写入 `leg_slippage_limit_bps` 字段，因为现有 `plan_arb()` 不接受该参数；不得借本功能改变计划算法。

行字段规则固定为：

```python
planned_notional_usd = plan.buy_notional if plan else ""
crossable_notional_usd = plan.q_max_notional if plan else ""
buy_depth_slippage_bps = (
    (plan.buy_limit / buy_book.best_ask - 1) * 1e4 if plan else ""
)
sell_depth_slippage_bps = (
    (sell_book.best_bid / plan.sell_limit - 1) * 1e4 if plan else ""
)
expected_edge_usd = plan.exp_edge_usd if plan else ""
```

`plan_status` 始终保存 `plan_arb()` 返回的 reason。结束时如果盘口仍有效则写当前快照；空盘口或过期时允许价格/计划字段为空，但保留最后的 `event_id`、持续时间和明确 `end_reason`。

- [ ] **Step 5: 运行全部 recorder 测试并确认通过**

Run: `python -m pytest tests/test_recorder.py -q`

Expected: PASS，包括原有 `MinuteRecorder` 测试。

- [ ] **Step 6: 提交指标实现**

```bash
git add entropy_arb/recorder.py tests/test_recorder.py
git commit -m "功能：记录信号执行质量指标"
```

### Task 4: 实现 CSV 兼容、异步唤醒和失败即停

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `tests/test_recorder.py`

- [ ] **Step 1: 写追加和旧 schema 旋转失败测试**

测试一：创建记录器、写一组事件、关闭，再对同一路径创建记录器写第二组事件；断言文件只有一个 `SIGNAL_HEADER`。

测试二：先写入 `old,header\n1,2\n`，再创建记录器并观察合格信号；断言新文件首行等于 `SIGNAL_HEADER`，旁边产生未占用的 `signals.csv.old`、`.old.1` 等归档且保留所有旧内容。

- [ ] **Step 2: 写异步每秒采样和错误传播失败测试**

异步采样测试在没有第二次行情事件的情况下等待超过 `sample_sec`，断言激活信号仍产生 `sample`。测试使用较短 `sample_sec`（例如 `0.02`）避免拖慢套件。

错误传播测试让 `_write_row()` 抛出 `OSError("disk full")`，运行 `SignalRecorder.run(stop, update_evt)`，断言：

```python
with pytest.raises(OSError, match="disk full"):
    await task
assert stop.is_set()
```

不得断言日志文本替代异常传播。

- [ ] **Step 3: 运行定向测试并确认失败**

Run: `python -m pytest tests/test_recorder.py -q -k "signal and (append or rotate or async or io_error)"`

Expected: FAIL。

- [ ] **Step 4: 实现文件初始化和异步运行循环**

沿用 `MinuteRecorder` 的父目录创建和表头检查规则，但使用独立 `SIGNAL_HEADER`；旋转时选择未占用的 `.old`、`.old.1` 等路径，不能覆盖已有归档。打开文件时采用 append/newline/UTF-8；每行 `writerow()` 后立即 `flush()`。

`run(stop, update_evt)` 必须先清事件再读取盘口，从而避免行情更新发生在观察与等待之间时丢失唤醒：

```python
try:
    while not stop.is_set():
        update_evt.clear()
        self.observe()
        if stop.is_set():
            break
        timeout = self.seconds_until_next_sample()
        if update_evt.is_set():
            continue
        try:
            await asyncio.wait_for(update_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
except Exception:
    log.exception("signal recorder failed")
    stop.set()
    update_evt.set()
    raise
finally:
    self.close()
```

`seconds_until_next_sample()` 在无活跃信号时返回一个有限但较长的等待值，确保关机事件可以唤醒；有活跃信号时返回到最近一个方向下次采样点的非负秒数。实现时避免 `timeout=0` 的忙循环，并在 `observe()` 后更新 `last_written_at`。

关闭阶段若写 shutdown end 失败，同样不得吞掉异常。实际实现需用保存主异常或嵌套 `try` 的方式保证：无主异常时向上传播关闭异常；已有主异常时记录关闭异常并重新抛出原始异常，不能用关闭异常覆盖根因。

- [ ] **Step 5: 运行 recorder 测试并确认通过**

Run: `python -m pytest tests/test_recorder.py -q`

Expected: PASS，无未关闭文件或异步任务警告。

- [ ] **Step 6: 提交 CSV 与运行循环**

```bash
git add entropy_arb/recorder.py tests/test_recorder.py
git commit -m "功能：持久化并按秒采样信号"
```

### Task 5: 仅在 record-only 集成到 Engine

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: 写记录器启动边界失败测试**

在 `tests/test_engine.py` 使用现有 stub venue 和配置 helper，构造完成市场加载后所需的最小 engine 状态。将现有记录器启动代码抽成 `_start_recorders(tasks)` 后，测试：

```python
record_engine._start_recorders(record_tasks)
assert record_engine.signal_recorder is not None
assert any(task.get_name() == "signal-recorder" for task in record_tasks)

live_engine._start_recorders(live_tasks)
assert live_engine.signal_recorder is None
assert all(task.get_name() != "signal-recorder" for task in live_tasks)
```

测试结束必须设置 stop、唤醒 `_update_evt` 并 await/cancel 自己创建的任务，避免泄漏。

- [ ] **Step 2: 运行定向测试并确认失败**

Run: `python -m pytest tests/test_engine.py -q -k "signal_recorder"`

Expected: FAIL，Engine 尚无信号记录器集成。

- [ ] **Step 3: 实现最小 Engine 集成**

在 `Engine.__init__` 增加：

```python
self.signal_recorder: Optional[SignalRecorder] = None
```

把现有分钟记录器启动块移动到 `_start_recorders(tasks)`，保持其启用条件不变。仅当 `self.record_only` 时创建 `SignalRecorder`，参数必须来自已解析的 engine/config 状态：

```python
self.signal_recorder = SignalRecorder(
    self.cfg.recorder_signal_csv,
    self.entropy,
    self.hedge,
    symbol=self.cfg.symbol,
    entropy_dex=self.cfg.entropy.hl_dex,
    hedge_venue=self.cfg.hedge_venue,
    midline_bps=self.cfg.midline_bps,
    upper_bps=self.cfg.upper_bps,
    lower_bps=self.cfg.lower_bps,
    take_fraction=self.cfg.take_fraction,
    max_order_notional=self.cfg.max_order_notional,
    min_base=self._min_base,
    min_notional=self._min_notional,
    size_step=self._step,
    leg_slippage_bps=self.cfg.leg_slippage_bps,
    staleness_sec=self.cfg.staleness_sec,
)
tasks.append(
    asyncio.create_task(
        self.signal_recorder.run(self.stop, self._update_evt),
        name="signal-recorder",
    )
)
```

在 `_run_inner()` 中市场元数据解析完成、feed 启动之后调用 `_start_recorders(tasks)`。不要改 `_scan()`、`_strategy_loop()` 或实盘任务启用条件。

- [ ] **Step 4: 运行 engine 与 recorder 测试**

Run: `python -m pytest tests/test_engine.py tests/test_recorder.py -q`

Expected: PASS；实盘模式任务集合没有 `signal-recorder`。

- [ ] **Step 5: 提交 Engine 集成**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "功能：只读模式启动信号记录器"
```

### Task 6: 更新中英文运行文档

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: 更新 record-only 输出说明**

在两份 README 的 `--record-only` 说明中明确：

- `logs/minutes.csv` 仍是分钟聚合，用于 `tools/analyze.py`。
- `logs/signals.csv` 是信号生命周期明细：越过费后门槛立即写 `start`，持续时每秒写 `sample`，消失/过期/关闭写 `end`。
- 该文件只用于观察，不会阻止开仓，也不会改变实盘逻辑。
- `recorder.signal_csv` 可修改路径，只在 `--record-only` 被消费。

同步更新 recorder 配置表格的默认值，不能把 `tools/analyze.py` 描述成会分析 `signals.csv`。

- [ ] **Step 2: 检查文档与配置一致**

Run: `rg -n "signals.csv|signal_csv|minutes.csv" README.md README.zh-CN.md config.example.yaml`

Expected: 中英文说明和示例配置都包含新文件；`tools/analyze.py` 仍只指向 `minutes.csv`。

- [ ] **Step 3: 提交文档**

```bash
git add README.md README.zh-CN.md
git commit -m "文档：说明只读信号明细记录"
```

### Task 7: 完整验证与范围审查

**Files:**
- Verify only: all changed files

- [ ] **Step 1: 运行完整测试并把警告视为错误**

Run: `python -m pytest -q -W error`

Expected: PASS，零 warning。

- [ ] **Step 2: 编译全部 Python 文件**

Run: `python -m compileall -q main.py entropy_arb tools tests`

Expected: exit 0，无输出。

- [ ] **Step 3: 检查补丁格式和工作树**

Run: `git diff --check`

Expected: exit 0，无空白错误。

Run: `git status --short`

Expected: 只有计划内文件；如果测试生成 `__pycache__` 等忽略文件，不纳入提交。

- [ ] **Step 4: 审查关键不变量**

Run: `git diff origin/codex/multi-hedge-foundation...HEAD -- entropy_arb/engine.py entropy_arb/recorder.py entropy_arb/config.py`

人工确认：

- `_scan()`、订单发送、仓位管理和风控门槛没有变化。
- 实盘模式不创建 `SignalRecorder`。
- 信号判断使用与 `plan_arb()` 一致的手续费语义。
- 生命周期过期判断与实盘一致地读取 `alive_ts`；观测字段中的盘口年龄和更新时间差读取 `last_update_ts`。
- 所有 CSV 写入/flush 异常都会停止并向上传播。
- 不包含密钥、凭据或服务器环境文件。

- [ ] **Step 5: 必要时提交最终测试微调**

仅当验证暴露本功能直接相关问题时，先补回归测试再做最小修复，并使用中文提交信息。若无问题，不创建空提交。
