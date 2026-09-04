# Engine 生命周期与记录可靠性设计

实现状态：已按本设计完成，验证记录见对应实施计划。

## 背景

当前记录器和 Engine 正常停机路径已经能传播 recorder 错误并关闭全部 venue，
但审查复现了四个未覆盖的失败路径：分钟 flush 瞬时失败会重复写行；初始化或
启动异常会绕过清理；普通后台任务失败不会触发停机或向上传播；Lighter 下单
发送可能无限等待，导致停机永远卡在在途执行。

本设计只修复这些失败路径，不改变套利方向、阈值、仓位、滑点、费率或下单
数量规则。

## 目标

- 每个分钟聚合最多尝试序列化一次，flush 结果不确定时不得重写同一行。
- venue 创建之后的所有退出路径都执行同一套有序清理。
- 任一长期后台任务异常或意外提前返回都会触发 stop，并成为可见错误。
- Lighter 订单发送有明确 deadline；超时按结果未知处理并进入现有 reconcile。
- 第一个业务/运行错误始终优先于后续任务取消、venue close 等清理错误。

## 非目标

- 不提供跨进程 CSV 文件锁；不同市场和进程仍必须使用独立输出路径。
- 不把 live 模式的分钟记录错误改成停机条件。
- 不增加新的配置键，不改变 `settle_timeout_sec` 的默认值或校验范围。
- 不改用 `asyncio.TaskGroup`，保持当前 Python 版本兼容和单异常传播接口。
- 不取消已提交但仍在 deadline 内的订单任务。

## 方案

### 1. 分钟行单次序列化

`MinuteRecorder._flush_agg()` 先确保文件已打开，再把当前 `_agg` 移到局部变量并
立即从待写状态移除，然后调用 `writerow()` 和 `flush()`。这样无论 `writerow`
部分写入或 `flush` 在数据已交给底层后报错，`close()` 和下一次采样都不会再次
序列化同一聚合。

CSV 追加无法在进程级 flush 失败时判断数据究竟是否已落盘，因此这里采用
“最多一次序列化尝试”，而不是不可实现的事务式 exactly-once。record-only
继续在任何写入错误时停机并上抛；live 继续记录错误但不阻止交易。只有
`flush()` 成功后才增加 `rows_written`。

### 2. 长期任务监督

Engine 为 recorder、行情、账户、策略、余额、状态、keepalive 和 reconcile
长期任务统一安装完成回调。回调遵循以下规则：

- 任务被 Engine 清理取消：不是错误。
- stop 已设置后正常结束：不是错误。
- stop 未设置时正常结束：转换为包含任务名的 `RuntimeError`。
- 任务以非取消异常结束：保存原异常及 traceback。

第一个非预期结果写入 Engine 的首要错误槽，并调用 `request_stop()`；后续错误
仅记录为次要错误。`http_keepalive_sec <= 0` 时不创建 keepalive 长期任务，避免
把配置允许的立即返回误判为故障。

任务回调会读取 `task.exception()`，因此不会产生 “Task exception was never
retrieved”；停机 gather 后仍遍历全部任务结果作为防漏检查，而不是只检查两个
recorder。

### 3. 统一生命周期与清理顺序

`Engine._run_inner()` 从创建第一个 venue 起进入统一 `try/finally`。venue 创建后
立即登记到 `self.venues`，确保第二个 venue 构造失败时第一个仍能被关闭。
并行 `load_market()` 使用显式 startup tasks；任一失败时取消并收集另一任务，
不留下游离协程。

退出顺序固定为：

1. 保存初始化、运行或外部取消产生的首要错误，并设置 stop/唤醒事件。
2. 保持行情和账户 feed 存活，等待已经提交的 `_exec_tasks` 在各自 deadline 内
   完成；drain/reconcile 异常只在没有更早错误时成为首要错误。
3. 取消并 gather 所有已创建的长期任务，检查全部非取消结果。
4. 按登记顺序逐个关闭所有 venue；一个 close 失败不阻止后续 venue。
5. 传播首要错误；只有不存在运行错误时才传播第一个清理错误。

若 Engine 协程本身收到 `CancelledError`，仍执行上述清理，完成后重新传播该
取消，不把它转换成普通运行错误。清理本身运行在独立 task 中并由 shield 保护；
即使清理期间再次收到取消，Engine 也会记录该取消并继续等待清理完成，避免
跳过长期任务收集或 venue close。

适配器必须在创建长期 task 之前完成所有可能失败的同步初始化。Lighter live
启动会先构造账户订单 feed，再创建 book/account tasks，避免账户 feed 初始化
失败后遗留 Engine 尚未取得引用的 book task。

### 4. Lighter 发送 deadline

`LighterVenue.send_taker()` 用 `asyncio.wait_for()` 包住
`signer.create_order()`，timeout 使用已有 `settle_timeout_sec`。发送等待和后续
成交确认各自拥有一个完整的 timeout 窗口。

`create_order()` 超时时，交易可能已经到达交易所，不能标记为安全的
`send_failed`。实现必须取消本地订单 watch，并返回
`OrderResult.unknown("order submission timed out")`。Engine 已有 unresolved
路径会设置 shutdown reconcile 标志并核对真实仓位。明确拒单、签名错误和
HTTP 429 的现有语义保持不变。

## 错误优先级

首要错误按实际发生先后保存一次，不被覆盖。来源包括：

- 初始化/启动错误；
- 信号同步回调错误；
- 任一长期任务异常或意外提前结束；
- drain/reconcile 错误；
- 外部取消。

长期任务 gather、venue close 等清理错误只在没有首要错误时向上传播，否则用
原 traceback 记录。正常用户 stop 没有首要错误时，清理成功即正常返回。

## 测试

- fail-once flush 在实际写入后抛错，断言 CSV 只有一条分钟数据行，且原
  `OSError` 仍传播。
- `load_market()` 失败时，断言所有已创建 venue 都关闭，另一 startup task
  已取消/收集。
- 第二个 `start_tasks()` 失败时，断言第一个 venue 已启动任务被取消并收集，
  两个 venue 都关闭。
- 普通长期任务异常且未主动设置 stop 时，Engine 自动停止并传播原异常。
- 长期任务正常提前返回且 stop 未设置时，Engine 传播带任务名的
  `RuntimeError`。
- 后台任务错误与 venue close 错误同时发生时，传播后台任务错误并关闭全部
  venue。
- 清理等待后台 task 取消期间再次取消 Engine，断言清理仍完成；若已有后台
  错误，该错误仍优先于后到的取消。
- Lighter 账户 feed 构造失败时，断言不会先创建并泄漏 book task。
- Lighter `create_order()` 永不返回时，在 `settle_timeout_sec` 后返回 unknown、
  清除订单 watch，且不会留下未完成协程。
- 完整测试在 warning-as-error 下通过，并检查无泄漏异步任务和未读取异常。

## 兼容与部署

配置文件和 CSV schema 不变。唯一外部行为变化是：以前可能静默存活、泄漏或
永久卡住的异常路径，现在会以非零错误退出；Lighter 发送超过现有
`settle_timeout_sec` 时按未知结果对账。部署前继续使用 record-only 验证，实盘
首次运行使用最小仓位上限。
