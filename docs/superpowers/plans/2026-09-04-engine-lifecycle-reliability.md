# Engine Lifecycle Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除分钟重复行，监督全部长期任务，并保证 Engine 所有启动、运行和停机失败路径都能有界清理及传播原始错误。

**Architecture:** `MinuteRecorder` 对每个聚合只做一次序列化尝试；Lighter 使用已有 `settle_timeout_sec` 限制订单提交。Engine 用统一的首错槽和 task done callback 监督长期任务，再由 `_run_inner()` 的单一 `finally` 按“等待在途执行、取消后台任务、关闭 venue、传播首错”的顺序清理。

**Tech Stack:** Python 3、asyncio、aiohttp、csv、pytest

---

### Task 1: 防止分钟 flush 重试产生重复行

**Files:**
- Modify: `entropy_arb/recorder.py:174-184`
- Test: `tests/test_recorder.py`

- [ ] **Step 1: 写瞬态 flush 失败回归测试**

在 `tests/test_recorder.py` 增加一次性失败流。真实 `flush()` 先执行，再抛一次错误，随后允许成功；跨分钟采样触发第一次写入，关闭触发潜在重试：

```python
def test_minute_transient_flush_failure_does_not_duplicate_row():
    class FailOnceAfterFlushBuffer(io.StringIO):
        def __init__(self):
            super().__init__()
            self.fail_next_flush = False

        def flush(self):
            result = super().flush()
            if self.fail_next_flush:
                self.fail_next_flush = False
                raise OSError("transient flush failure")
            return result

        def close(self):
            pass

    entropy, hedge = OrderBook(), OrderBook()
    set_book(entropy, 100.0, 100.02)
    set_book(hedge, 100.0, 100.02)
    rec = MinuteRecorder(
        os.path.join(tempfile.mkdtemp(), "minutes.csv"),
        entropy, hedge, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)
    stream = FailOnceAfterFlushBuffer()
    writer = csv.writer(stream)
    writer.writerow(HEADER)
    rec._fh = stream
    rec._writer = writer
    stream.fail_next_flush = True

    with pytest.raises(OSError, match="transient flush failure"):
        rec.sample(1_700_000_060.0)
    rec.close()

    rows = list(csv.reader(io.StringIO(stream.getvalue())))
    assert len(rows) == 2
    assert rows[1][0] == str((int(1_700_000_000.0 // 60)) * 60)
    assert rec.rows_written == 0
```

- [ ] **Step 2: 运行测试并确认重复行失败**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_recorder.py -k transient_flush`

Expected: FAIL，`len(rows) == 3`，证明同一聚合被第二次序列化。

- [ ] **Step 3: 在序列化前移走待写聚合**

把 `_flush_agg()` 改为：文件打开失败时保留聚合；文件成功打开后，在任何可能部分写入的 `writerow()` 之前清除待写引用，禁止错误后的盲重试：

```python
def _flush_agg(self) -> None:
    if self._agg is None or self._agg.n == 0:
        self._agg = None
        return
    if self._writer is None:
        self._open()
    agg = self._agg
    self._agg = None
    self._writer.writerow(agg.row(
        self.symbol, self.entropy_dex, self.hedge_venue))
    self._fh.flush()
    self.rows_written += 1
```

- [ ] **Step 4: 运行 recorder 测试**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_recorder.py`

Expected: PASS，包括永久 flush 失败仍关闭句柄、瞬态失败不重复行。

- [ ] **Step 5: 提交**

```bash
git add entropy_arb/recorder.py tests/test_recorder.py
git commit -m "修复：防止分钟记录重复写入"
```

### Task 2: 给 Lighter 订单提交增加 deadline

**Files:**
- Modify: `entropy_arb/venue_lighter.py:275-314`
- Test: `tests/test_venue_contract.py`

- [ ] **Step 1: 写挂起提交超时测试**

在 `tests/test_venue_contract.py` 增加 `sys`、`pytest` 导入和以下测试；使用真实 `LighterVenue.send_taker()`，只替换 SDK 边界：

```python
def test_lighter_submission_timeout_returns_unknown_and_unwatches(monkeypatch):
    class Constants:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    class HangingSigner:
        def __init__(self):
            self.cancelled = False

        async def create_order(self, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    class OrdersFeed:
        def __init__(self):
            self.future = None
            self.unwatched = []

        def watch(self, coi):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

        def unwatch(self, coi):
            self.unwatched.append(coi)
            if self.future is not None and not self.future.done():
                self.future.cancel()

    monkeypatch.setitem(
        sys.modules, "lighter",
        SimpleNamespace(SignerClient=Constants))

    async def go():
        cfg = make_cfg("lighter")
        venue = LighterVenue(cfg.hedge, object(), 0.01)
        venue.market_id = 7
        signer = HangingSigner()
        orders = OrdersFeed()
        venue.signer = signer
        venue.orders_feed = orders

        result = await asyncio.wait_for(
            venue.send_taker(is_buy=True, qty=0.5, limit_px=100.0),
            timeout=0.05)

        assert result.unresolved is True
        assert result.status == "order submission timed out"
        assert orders.unwatched
        assert signer.cancelled is True

    asyncio.run(go())
```

- [ ] **Step 2: 运行测试并确认超时失败**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_venue_contract.py -k submission_timeout`

Expected: FAIL，外层 `asyncio.wait_for()` 在 0.05 秒后抛 `TimeoutError`，而不是返回 unknown。

- [ ] **Step 3: 使用现有 settle timeout 包住 create_order**

将调用改为 `asyncio.wait_for()` 并在普通异常分支前处理超时：

```python
try:
    _tx, resp, err = await asyncio.wait_for(
        self.signer.create_order(
            market_index=self.market_id,
            client_order_index=coi,
            base_amount=base_amount,
            price=price,
            is_ask=not is_buy,
            order_type=SignerClient.ORDER_TYPE_MARKET,
            time_in_force=(
                SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL),
            reduce_only=reduce_only,
            order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
        ),
        timeout=self.settle_timeout,
    )
except asyncio.TimeoutError:
    if fut is not None:
        self.orders_feed.unwatch(coi)
    log.error("[%s] order submission timed out for coi %d after %.1fs",
              self.name, coi, self.settle_timeout)
    return OrderResult.unknown("order submission timed out")
except Exception as e:
    if fut is not None:
        self.orders_feed.unwatch(coi)
    msg = f"{type(e).__name__}: {e}"
    if getattr(e, "status", None) == 429 or "(429)" in str(e):
        msg = "RATE_LIMITED: " + msg
    return OrderResult.send_failed(msg)
```

- [ ] **Step 4: 运行 venue contract 测试**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_venue_contract.py tests/test_venue_registry.py`

Expected: PASS，原有 HLVenue 与 registry 合约不变。

- [ ] **Step 5: 提交**

```bash
git add entropy_arb/venue_lighter.py tests/test_venue_contract.py
git commit -m "修复：限制 Lighter 订单提交等待"
```

### Task 3: 监督所有长期后台任务

**Files:**
- Modify: `entropy_arb/engine.py:44-175`
- Test: `tests/test_engine.py`

- [ ] **Step 1: 写后台异常和意外返回测试**

在 `tests/test_engine.py` 增加两个 venue stub。测试必须在 `finally` 中取消红灯阶段遗留任务：

```python
class BackgroundOutcomeVenue(LifecycleVenue):
    def __init__(self, key, label, outcome):
        super().__init__(key, label)
        self.outcome = outcome

    def start_tasks(self, _stop, _notify, _live):
        if self.key != "entropy":
            return []

        async def finish():
            await asyncio.sleep(0)
            if isinstance(self.outcome, BaseException):
                raise self.outcome

        return [asyncio.create_task(finish(), name="book-entropy")]


@pytest.mark.parametrize(
    "outcome,match",
    [(RuntimeError("book failed"), "book failed"),
     (None, "book-entropy.*exited unexpectedly")],
)
def test_background_task_failure_or_early_exit_stops_engine(outcome, match):
    async def go():
        cfg = make_cfg(midline=0.0, upper=5.0, lower=5.0)
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": BackgroundOutcomeVenue(
                "entropy", "ENTROPY", outcome),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match=match):
                await asyncio.wait_for(eng._run_inner(), timeout=0.2)
        finally:
            eng.request_stop()
            engine_module.create_venue = original
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())
```

- [ ] **Step 2: 运行测试并确认 Engine 超时失败**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_engine.py -k background_task_failure_or_early_exit`

Expected: FAIL；当前 Engine 不监督普通任务，外层得到 `TimeoutError` 或正常返回而非原错误。

- [ ] **Step 3: 增加首错槽和任务完成回调**

在 `Engine.__init__` 增加：

```python
self._primary_error: Optional[BaseException] = None
self._task_failures: Dict[asyncio.Task, BaseException] = {}
```

删除 `_signal_callback_error`；所有同步回调与任务失败都进入同一个
`_primary_error` 槽。

增加以下方法：

```python
def _remember_error(self, label: str, error: BaseException) -> None:
    if error is self._primary_error:
        return
    if self._primary_error is None:
        self._primary_error = error
        log.error("%s failed", label,
                  exc_info=(type(error), error, error.__traceback__))
    else:
        log.error("%s also failed; preserving the primary error", label,
                  exc_info=(type(error), error, error.__traceback__))

def _task_done(self, task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        if self.stop.is_set():
            return
        error = RuntimeError(
            f"background task {task.get_name()} exited unexpectedly")
    self._task_failures[task] = error
    self._remember_error(f"background task {task.get_name()}", error)
    self.request_stop()

def _track_task(self, tasks: List[asyncio.Task],
                task: asyncio.Task) -> None:
    tasks.append(task)
    task.add_done_callback(self._task_done)
```

把 `_record_only_book_update()` 的异常保存改为：

```python
except Exception as exc:
    self._remember_error("signal recorder book callback", exc)
    self.request_stop()
```

所有 `_start_recorders()`、venue `start_tasks()` 和 Engine 自建长期任务都通过
`_track_task()` 加入列表。`http_keepalive_sec <= 0` 时不创建 keepalive 任务。

- [ ] **Step 4: 让现有停机结果使用统一首错槽**

在当前清理末尾，遍历全部 `tasks/results`，把尚未由回调记录的非取消异常交给
`_remember_error()`；venue close 失败也交给该方法。最终只传播
`self._primary_error`。这个中间步骤保持当前 `_run_inner()` 结构，Task 4 再统一
启动失败清理。

```python
for task, result in zip(tasks, results):
    if (isinstance(result, BaseException)
            and not isinstance(result, asyncio.CancelledError)
            and task not in self._task_failures):
        self._task_failures[task] = result
        self._remember_error(
            f"background task {task.get_name()}", result)

if self._primary_error is not None:
    raise self._primary_error
```

- [ ] **Step 5: 运行 Engine 测试**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_engine.py`

Expected: PASS；普通任务错误、意外退出和原 recorder 错误都可见。

- [ ] **Step 6: 提交**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "修复：监督引擎后台任务"
```

### Task 4: 统一初始化、运行和停机清理

**Files:**
- Modify: `entropy_arb/engine.py:178-312`
- Test: `tests/test_engine.py`

- [ ] **Step 1: 写 load_market 失败清理测试**

```python
class LoadFailVenue(LifecycleVenue):
    async def load_market(self):
        if self.key == "entropy":
            raise RuntimeError("market load failed")
        await asyncio.sleep(0)
        await super().load_market()


def test_market_load_failure_closes_every_created_venue():
    async def go():
        cfg = make_cfg()
        venues = {
            "entropy": LoadFailVenue("entropy", "ENTROPY"),
            "hedge": LoadFailVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match="market load failed"):
                await eng._run_inner()
        finally:
            engine_module.create_venue = original
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())
```

- [ ] **Step 2: 写部分 start_tasks 失败清理测试**

```python
class StartFailVenue(LifecycleVenue):
    def __init__(self, key, label):
        super().__init__(key, label)
        self.started_task = None
        self.started_task_cancelled = False

    def start_tasks(self, _stop, _notify, _live):
        if self.key == "hedge":
            raise RuntimeError("task startup failed")

        async def wait_forever():
            try:
                await asyncio.Event().wait()
            finally:
                self.started_task_cancelled = True

        self.started_task = asyncio.create_task(
            wait_forever(), name="book-entropy")
        return [self.started_task]


def test_partial_task_start_failure_cancels_started_tasks_and_closes_venues():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": StartFailVenue("entropy", "ENTROPY"),
            "hedge": StartFailVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        try:
            with pytest.raises(RuntimeError, match="task startup failed"):
                await eng._run_inner()
        finally:
            eng.request_stop()
            engine_module.create_venue = original
            leftovers = [task for task in (
                venues["entropy"].started_task,
                eng._recorder_task,
                eng._signal_task,
            ) if task is not None and not task.done()]
            for task in leftovers:
                task.cancel()
            if leftovers:
                await asyncio.gather(*leftovers, return_exceptions=True)

        assert venues["entropy"].started_task_cancelled is True
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())
```

- [ ] **Step 3: 写外部取消仍清理并重新传播测试**

```python
def test_external_cancellation_closes_venues_and_remains_cancelled():
    async def go():
        cfg = make_cfg()
        directory = tempfile.mkdtemp()
        cfg.recorder_csv = os.path.join(directory, "minutes.csv")
        cfg.recorder_signal_csv = os.path.join(directory, "signals.csv")
        venues = {
            "entropy": LifecycleVenue("entropy", "ENTROPY"),
            "hedge": LifecycleVenue("hedge", "RH"),
        }
        eng = Engine(cfg, record_only=True)
        original = engine_module.create_venue
        engine_module.create_venue = lambda conf, _runtime: venues[conf.key]
        task = asyncio.create_task(eng._run_inner())
        try:
            while not eng.markets_ready:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            engine_module.create_venue = original
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert all(venue.closed for venue in venues.values())

    asyncio.run(go())
```

- [ ] **Step 4: 运行测试并确认清理失败**

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_engine.py -k "market_load_failure or partial_task_start_failure or external_cancellation"`

Expected: FAIL，venue 未关闭或已启动任务未由 Engine 取消。

- [ ] **Step 5: 增加有界 startup gather**

增加 helper，任一市场加载失败时取消并收集另一加载任务：

```python
async def _load_markets(self) -> None:
    tasks = [
        asyncio.create_task(self.entropy.load_market(),
                            name="load-market-entropy"),
        asyncio.create_task(self.hedge.load_market(),
                            name="load-market-hedge"),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
```

- [ ] **Step 6: 用单一 finally 包住 venue 全生命周期**

在 `_run_inner()` 开头创建 `tasks = []`。第一个 venue 创建后立即写入
`self.venues["entropy"]`，第二个同理；调用 `_load_markets()`。把现有市场计算、
日志、启动长期任务和 `await self.stop.wait()` 保持原顺序放入 `try`。

把从第一个 `create_venue()` 到 `await self.stop.wait()` 的现有代码整体放入
`try`，紧接着使用以下 `except/finally` 清理尾部，替换当前仅在正常 stop 后执行
的清理：

```python
except BaseException as exc:
    self._remember_error("engine lifecycle", exc)
finally:
    self.request_stop()
    if self._primary_error is None:
        for task in tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._remember_error(
                        f"background task {task.get_name()}", error)
                    break
    try:
        await self._drain_executions()
    except BaseException as exc:
        self._remember_error("execution drain", exc)
    for task in tasks:
        if not task.done():
            task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if (isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
                and task not in self._task_failures):
            self._task_failures[task] = result
            self._remember_error(
                f"background task {task.get_name()}", result)
    for venue in self.venues.values():
        try:
            await venue.close()
        except BaseException as exc:
            self._remember_error(f"[{venue.name}] close", exc)

if self._primary_error is not None:
    raise self._primary_error
```

不得保留旧的 recorder 专用结果索引或第二套 venue close 循环。停机汇总日志放在
venue close 之后、最终 raise 之前。

- [ ] **Step 7: 增加后台错误优先于 close 错误的断言并运行测试**

扩展 Task 3 的异常 venue，让 entropy `close()` 抛 `OSError("close failed")`，
hedge 正常关闭；断言最终仍是 `RuntimeError("book failed")` 且两个 venue 的
`closed` 都为真。

Run: `python -m pytest -q -p no:cacheprovider -W error tests/test_engine.py tests/test_recorder.py tests/test_venue_contract.py`

Expected: PASS，无未完成任务或未读取异常 warning。

- [ ] **Step 8: 提交**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "修复：统一引擎生命周期清理"
```

### Task 5: 文档、完整验证与复审

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-09-04-engine-lifecycle-reliability-design.md`
- Modify: `docs/superpowers/plans/2026-09-04-engine-lifecycle-reliability.md`

- [ ] **Step 1: 更新运行与停机说明**

在中英文 README 的安全停机部分明确：后台任务异常会使进程非零退出；启动失败
同样关闭已创建 venue 和任务；Lighter 提交超过 `settle_timeout_sec` 按未知结果
对账。不要承诺 CSV 在 I/O 错误时事务式 exactly-once，只说明不会盲目重写同一
分钟聚合。

- [ ] **Step 2: 严格全量验证**

Run: `python -m pytest -q -p no:cacheprovider -W error`

Expected: 所有测试通过，无 warning。

Run: `python -m compileall -q main.py entropy_arb tools tests`

Expected: exit 0，无输出。

Run: `git diff --check`

Expected: exit 0。

- [ ] **Step 3: 检查计划不变量**

人工核对完整 diff：

- `_scan()`、`_plan()`、订单数量、费率、滑点和库存规则无变化。
- live 分钟记录错误仍只记录日志，不触发 stop。
- record-only recorder 错误、后台任务错误和启动错误向上传播。
- 已提交订单在 timeout 内不被 shutdown 取消；Lighter 超时结果进入 reconcile。
- 所有已创建长期任务被 gather，所有已登记 venue 都尝试关闭。

- [ ] **Step 4: 独立只读复审**

复审完整 diff，重点验证四个原始复现、异常优先级、外部取消和无任务泄漏；复审
代理不得修改文件。

- [ ] **Step 5: 提交文档**

```bash
git add README.md README.zh-CN.md docs/superpowers/specs/2026-09-04-engine-lifecycle-reliability-design.md docs/superpowers/plans/2026-09-04-engine-lifecycle-reliability.md
git commit -m "文档：说明引擎故障清理语义"
```
