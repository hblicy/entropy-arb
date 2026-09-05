# Recorder Data Integrity Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复分钟数据混写、CSV 归档覆盖、记录器延迟发现 I/O 错误，以及关闭阶段异常覆盖问题。

**Architecture:** 分钟 CSV 与信号 CSV 都在记录器启动时初始化并采用相同的递增归档规则。分钟行携带市场身份，分析器在输入包含多个市场时明确拒绝，避免生成错误阈值。Engine 只在 `record-only` 下把分钟记录错误视为致命错误，并在关闭所有交易所后按原始错误优先级传播异常。

**Tech Stack:** Python 3、asyncio、pytest、CSV、argparse

---

### Task 1: 分钟 CSV 市场身份与分析器隔离

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tools/analyze.py`
- Test: `tests/test_recorder.py`
- Test: `tests/test_analyze.py`
- Test: `tests/test_engine.py`

- [x] **Step 1: 写分钟行市场身份失败测试**

在 `tests/test_recorder.py` 创建两个市场身份不同、写入同一路径的 `MinuteRecorder`，断言每行的 `symbol`、`entropy_dex`、`hedge_venue` 分别对应各自运行：

```python
assert [(row["symbol"], row["entropy_dex"], row["hedge_venue"])
        for row in rows] == [
    ("SNDK", "io", "lighter-rh"),
    ("XYZ100", "io", "tradexyz"),
]
```

- [x] **Step 2: 运行测试并确认因字段不存在而失败**

Run: `python -m pytest -q -W error tests/test_recorder.py -k minute_rows_identify_market`

Expected: FAIL，CSV 中没有市场身份字段。

- [x] **Step 3: 给分钟 schema 和 Engine 构造参数增加市场身份**

在 `entropy_arb/recorder.py` 中把分钟头部扩展为：

```python
HEADER = [
    "minute_ts", "time_utc", "symbol", "entropy_dex", "hedge_venue",
    # 其余现有价格和统计字段保持原顺序
]
```

`MinuteRecorder` 保存三个身份参数，`_MinuteAgg.row()` 接收并写入这些值；`Engine._start_recorders()` 从 `cfg.symbol`、`cfg.entropy.hl_dex`、`cfg.hedge_venue` 传入。

- [x] **Step 4: 写分析器拒绝混合市场失败测试**

在 `tests/test_analyze.py` 为纯函数 `validate_single_market(rows)` 增加：

```python
with pytest.raises(ValueError, match="multiple markets"):
    analyze.validate_single_market([
        {"symbol": "SNDK", "entropy_dex": "io", "hedge_venue": "lighter-rh"},
        {"symbol": "SNDK", "entropy_dex": "io", "hedge_venue": "tradexyz"},
    ])
```

同时断言全部为旧 schema 的空身份行仍可分析，以兼容历史 `minutes.csv.old`。

- [x] **Step 5: 运行测试并确认因校验函数不存在而失败**

Run: `python -m pytest -q -W error tests/test_analyze.py -k single_market`

Expected: FAIL，`validate_single_market` 尚不存在。

- [x] **Step 6: 实现混合市场拒绝**

`load_rows()` 保存三个可选身份字段；新增：

```python
def validate_single_market(rows: list) -> tuple[str, str, str]:
    markets = {(row["symbol"], row["entropy_dex"], row["hedge_venue"])
               for row in rows}
    if len(markets) > 1:
        raise ValueError("multiple markets found in one minute CSV")
    return next(iter(markets), ("", "", ""))
```

`main()` 在统计前调用并把错误写入 stderr 后以状态码 2 退出。

- [x] **Step 7: 运行相关测试并确认通过**

Run: `python -m pytest -q -W error tests/test_recorder.py tests/test_analyze.py tests/test_engine.py`

Expected: PASS。

- [x] **Step 8: 提交**

```bash
git add entropy_arb/recorder.py entropy_arb/engine.py tools/analyze.py tests/test_recorder.py tests/test_analyze.py tests/test_engine.py
git commit -m "修复：隔离不同市场的分钟数据"
```

### Task 2: 启动即验证输出并安全归档

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `entropy_arb/engine.py`
- Test: `tests/test_recorder.py`
- Test: `tests/test_engine.py`

- [x] **Step 1: 写无信号时非法路径立即失败测试**

运行 `SignalRecorder.run()`，盘口不满足任何方向且 `signal_csv` 父路径是普通文件，断言任务立即抛出 `OSError` 并设置 `stop`。

- [x] **Step 2: 写分钟和交易 CSV 归档保留测试**

分别准备当前 CSV、已有 `.old`，触发表头迁移，断言 `.old` 内容不变、当前旧文件移动到 `.old.1`；该测试同时确保 Windows 上重命名前已关闭读句柄。

- [x] **Step 3: 运行三项测试并确认失败**

Run: `python -m pytest -q -W error tests/test_recorder.py -k "invalid_path_without_signal or minute_rotation" tests/test_engine.py -k trades_rotation`

Expected: FAIL，信号文件未立即打开，分钟/交易归档仍使用固定 `.old`。

- [x] **Step 4: 实现统一安全归档与启动初始化**

将 `_next_archive_path()` 公开为 `next_archive_path()`；分钟和信号记录器都先在 `with open(...)` 外保存表头、关闭句柄，再调用 `os.replace()`。`SignalRecorder.run()` 在进入循环前执行 `self._open()`；`Engine._log_csv()` 也使用递增归档路径。

- [x] **Step 5: 运行相关测试并确认通过**

Run: `python -m pytest -q -W error tests/test_recorder.py tests/test_engine.py`

Expected: PASS。

- [x] **Step 6: 提交**

```bash
git add entropy_arb/recorder.py entropy_arb/engine.py tests/test_recorder.py tests/test_engine.py
git commit -m "修复：启动校验记录文件并保留归档"
```

### Task 3: 传播分钟记录错误并保留原始异常

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `entropy_arb/engine.py`
- Test: `tests/test_recorder.py`
- Test: `tests/test_engine.py`

- [x] **Step 1: 写 record-only 分钟记录失败测试**

直接运行 `MinuteRecorder.run(stop, fail_fast=True)`，让 `_open()` 因非法父路径失败，断言设置 `stop` 并向上传播 `OSError`；再通过真实 `Engine._run_inner()` 断言同一异常可被调用方感知。

- [x] **Step 2: 写信号错误优先于交易所关闭错误测试**

使用现有 `InvalidBurstVenue` 触发信号计算的 `ZeroDivisionError`，让第一个 venue 的 `close()` 抛出 `OSError`，断言最终仍抛 `ZeroDivisionError` 且第二个 venue 也执行了关闭。

- [x] **Step 3: 运行测试并确认失败**

Run: `python -m pytest -q -W error tests/test_recorder.py -k minute_io_error tests/test_engine.py -k "minute_recorder_error or venue_close"`

Expected: FAIL，分钟错误被吞掉，交易所关闭错误覆盖原始错误。

- [x] **Step 4: 实现错误传播和关闭顺序**

`MinuteRecorder.run()` 增加 `fail_fast`：仅该模式下记录异常、设置 `stop` 并重新抛出。Engine 保存分钟任务，在 `record_only` 下读取其结果。关闭交易所时逐个捕获异常并继续，最终按以下顺序抛出：

```python
callback_error or signal_error or minute_error or venue_close_error
```

所有次要异常都用原 traceback 写入日志。

- [x] **Step 5: 运行相关测试并确认通过**

Run: `python -m pytest -q -W error tests/test_recorder.py tests/test_engine.py`

Expected: PASS。

- [x] **Step 6: 提交**

```bash
git add entropy_arb/recorder.py entropy_arb/engine.py tests/test_recorder.py tests/test_engine.py
git commit -m "修复：传播采集错误并完成安全关闭"
```

### Task 4: 文档、迁移说明与完整验证

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-09-01-record-only-signal-observability-design.md`
- Modify: `docs/superpowers/plans/2026-09-01-record-only-signal-observability.md`
- Modify: `docs/superpowers/plans/2026-09-04-recorder-data-integrity-fixes.md`

- [x] **Step 1: 更新迁移和分析说明**

说明新分钟 schema 含市场身份；升级后旧 `minutes.csv` 会安全归档；分析器允许纯旧 schema，但遇到混合市场会明确报错，用户应为每个市场使用独立文件。

- [x] **Step 2: 更新设计与实施记录**

同步启动即初始化、分钟记录失败在 record-only 下致命、递增归档和关闭错误优先级。

- [x] **Step 3: 严格验证**

Run: `python -m pytest -q -p no:cacheprovider -W error`

Expected: 所有测试通过且无 warning。

Run: `python -m compileall -q entropy_arb main.py tools tests`

Expected: exit 0，无输出。

Run: `git diff --check`

Expected: exit 0，无空白错误。

- [x] **Step 4: 独立只读复审**

审查本计划涉及的完整 diff，重点核对实盘隔离、旧 CSV 兼容、异常优先级和所有归档路径；复审代理不得修改文件。

复审发现分钟最终 flush 失败时句柄未关闭；已用回归测试复现并在提交
`9550cab` 中修复，随后独立复核无阻断问题。复审另报的初始化期失败清理和
非 recorder 后台任务异常处理均存在于基线 `40c6ef5`，不属于本计划确认的五项
修复，按范围约束记录为后续工作，未在本轮扩大 Engine 生命周期重构。

- [x] **Step 5: 提交**

```bash
git add README.md README.zh-CN.md docs/superpowers/specs/2026-09-01-record-only-signal-observability-design.md docs/superpowers/plans/2026-09-01-record-only-signal-observability.md docs/superpowers/plans/2026-09-04-recorder-data-integrity-fixes.md
git commit -m "文档：说明采集数据完整性保障"
```
