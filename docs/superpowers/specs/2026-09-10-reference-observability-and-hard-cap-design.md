# 双腿硬额度与参考基差观测设计

## 目标

本次改动同时解决三个直接相关的问题：

1. `max_order_notional_usd` 当前按最优买价换算数量，吃多档盘口后实际名义金额可能超过配置上限。
2. 当前记录只包含两边订单簿价格，无法区分可执行价差、两边 oracle/index 的结构性基差和资金费影响。
3. `signals.csv` 在信号持续存在时每秒增长，缺少有界的单文件轮转和压缩。

参考数据在本阶段只用于记录和告警。除严格执行双腿单笔额度上限外，不改变开仓、平仓、仓位、冷却、滑点或熔断逻辑。

## 非目标

- 不把参考基差或资金费加入开平仓门槛。
- 不自动修改用户的阈值或手续费配置。
- 不使用 mark price 代替缺失的 oracle/index。
- 不自动删除历史 CSV 或压缩包。
- 不引入新的 WebSocket 连接；复用现有每个交易所的订单簿连接。
- 不修改多交易所选择、路由或执行架构。

## 一、双腿单笔名义金额硬上限

### 问题

`plan_arb()` 目前使用第一档 ask 计算目标数量：

```text
target = cap_notional / best_ask
```

当买入需要跨越多档 ask 时，实际 `buy_notional` 会高于该估算值。两边存在较大溢价时，`sell_notional` 也可能明显高于 `buy_notional`。因此 `max_order_notional_usd` 必须约束每条腿的实际计划名义金额，而不是只约束按第一档买价估算的金额。

### 语义

对每个计划同时保证：

```text
buy_notional  <= cap_notional
sell_notional <= cap_notional
```

两条腿继续使用相同 base 数量，确保不会为满足美元上限而主动制造 base 敞口。

### 算法

1. 保留现有可成交深度与费后边际阈值计算，得到 `q_max`。
2. 分别沿买方 asks 和卖方 bids 逐档计算，在累计名义金额不超过 `cap_notional` 时可成交的最大 base 数量。
3. 目标数量取以下三者的最小值：
   - `q_max * take_fraction`
   - 买入腿额度允许的最大数量
   - 卖出腿额度允许的最大数量
4. 按交易所共同 `size_step` 向下取整。
5. 重新走两边深度，计算实际 `buy_notional` 和 `sell_notional`。
6. 使用小的浮点比较容差验证两腿均未超限。若精度边界仍超限，减少一个 `size_step` 后重新计算；若数量低于 `min_base` 或任一腿低于 `min_notional`，返回可识别的无计划原因。

额度计算按盘口逐档完成，不能通过从初始数量反复减去大量 `size_step` 的线性循环实现，避免极小数量步长下出现不必要的高开销。

## 二、统一参考数据模型

### 模型

新增统一的市场参考快照，由每个交易所适配器持有。字段包括：

- `oracle_px`
- `index_px`
- `mark_px`
- `funding_current_bps_per_hour`
- `funding_last_bps_per_hour`
- `funding_last_ts_ms`
- `exchange_ts_ms`
- `received_mono`
- `source`：`websocket` 或 `rest`

价格必须为有限正数。资金费率必须有限，允许为正、零或负。交易所没有提供的字段保持为空。收到格式错误、非有限值、非正价格或早于最后有效交易所时间的数据时，不覆盖最后有效快照。

所有状态在同一 asyncio 事件循环中更新和读取，不引入线程锁。记录器只读取不可变快照，避免观察到一半更新的字段组合。

`reference_age_ms` 使用本地单调时钟与 `received_mono` 计算，`reference_update_skew_ms` 使用两边 `received_mono` 的差值计算。交易所时间只用于乱序检测和审计，不直接用于跨交易所时效差，避免服务器时钟偏差污染结果。

### 单位

适配器负责将交易所原生资金费率转换为 `bps/小时`：

- Hyperliquid `funding` 按小时小数比例转换为 bps。
- Lighter `market_stats.current_funding_rate` 按其 WebSocket 百分比字段转换为 bps/小时。
- Lighter REST 的 index/mark 来自 `orderBookDetails`；`funding-rates` 返回跨交易所的 8 小时等效小数费率，筛选 `exchange=lighter` 后除以 8 并转换为 bps/小时。

WebSocket 和 REST 解析器分别测试，不能因为字段名称相同而共用未经验证的倍率。日志可保留原始值用于诊断，CSV 只写统一后的 bps/小时。

## 三、WebSocket 主通道和 REST 恢复

### Hyperliquid / Entropy

在现有 Hyperliquid WebSocket 连接中同时订阅：

- `l2Book`
- `activeAssetCtx`

`activeAssetCtx` 更新 `oracle_px`、`mark_px` 和当前 `funding`。该频道不提供的上一期资金费字段保持为空。订单簿与参考消息分别解析。可识别的参考消息错误只影响参考快照，不清空订单簿或中断订单簿连接；未预期异常保留调用链并由现有重连机制处理。

### Lighter

在现有 Lighter WebSocket 连接中同时订阅：

- `order_book/{market_id}`
- `market_stats/{market_id}`

`market_stats` 更新 `index_price`、`mark_price`、`current_funding_rate`、上一期 `funding_rate` 和交易所时间。参考频道错误不改变订单簿 nonce、ready 状态或订单簿内容。

### 初始化和恢复

- `load_market()` 完成市场识别后，通过公开 REST 获取一次参考快照。
- WebSocket 是正常运行时的主数据源。
- 任一参考快照超过 `reference.stale_sec` 未更新时，启动 REST 恢复。
- 恢复期间每 `reference.rest_recovery_sec` 秒请求一次。
- 收到新的有效 WebSocket 参考消息后停止 REST 轮询。
- REST 请求不进入策略评估或下单调用链。
- 启动时参考接口不可用、运行中参考数据过期或 REST 恢复失败都只记录状态化告警，不阻止现有交易逻辑。

默认配置：

```yaml
reference:
  rest_recovery_sec: 15
  stale_sec: 60
  residual_alert_bps: 20
  residual_persist_sec: 30
```

配置继续执行严格 schema 校验。时间和阈值必须为有限非负数，其中恢复周期和过期时间必须大于零。

## 四、基差、残差与资金费计算

### 参考基差

只在 Entropy oracle 和对冲腿 index 同时有效时计算：

```text
reference_basis_bps
= (entropy_oracle_px / hedge_index_px - 1) * 10000
```

缺少任一输入时结果为空。mark price 仅记录，不作为 oracle/index 的替代值。

### 有符号可执行溢价

从 Entropy 相对对冲腿的角度表示：

```text
sell_entropy:
  signed_executable_premium_bps
  = (entropy_bid / hedge_ask - 1) * 10000

buy_entropy:
  signed_executable_premium_bps
  = (entropy_ask / hedge_bid - 1) * 10000
```

第二个值在 Entropy 便宜时为负数。

### 残差

```text
signed_residual_bps
= signed_executable_premium_bps - reference_basis_bps

sell_entropy:
  residual_edge_bps = signed_residual_bps

buy_entropy:
  residual_edge_bps = -signed_residual_bps
```

`residual_edge_bps` 为正表示该方向的订单簿差异超过参考基差。它是观测值，不参与本阶段的交易决策。

### 每小时净资金费

采用“正资金费由多头支付空头”的统一方向：

```text
sell_entropy:
  net_funding_bps_per_hour
  = entropy_funding_current_bps_per_hour
    - hedge_funding_current_bps_per_hour

buy_entropy:
  net_funding_bps_per_hour
  = hedge_funding_current_bps_per_hour
    - entropy_funding_current_bps_per_hour
```

缺少任一腿资金费时结果为空。不在 CSV 中把该小时费率乘以假设持仓小时数，后续分析器按用户选择的 1–6 小时窗口投影。

## 五、CSV schema

### `signals.csv`

每个方向快照新增：

- `entropy_oracle_px`
- `entropy_mark_px`
- `entropy_funding_current_bps_per_hour`
- `entropy_funding_last_bps_per_hour`
- `entropy_funding_last_ts_ms`
- `entropy_reference_age_ms`
- `hedge_index_px`
- `hedge_mark_px`
- `hedge_funding_current_bps_per_hour`
- `hedge_funding_last_bps_per_hour`
- `hedge_funding_last_ts_ms`
- `hedge_reference_age_ms`
- `reference_update_skew_ms`
- `reference_basis_bps`
- `signed_executable_premium_bps`
- `signed_residual_bps`
- `residual_edge_bps`
- `net_funding_bps_per_hour`

现有 `top_edge_bps` 保留，避免改变已有信号生命周期含义。

### `minutes.csv`

每分钟记录参考值的收盘快照：

- 两边 oracle/index/mark
- 两边标准化的当前和上一期资金费率
- 两边参考数据年龄与更新时间偏差
- `reference_basis_close_bps`
- `funding_diff_close_bps_per_hour`，固定定义为 Entropy 当前资金费减去 hedge 当前资金费

对每秒计算出的有符号残差聚合：

- `residual_open_bps`
- `residual_high_bps`
- `residual_low_bps`
- `residual_close_bps`
- `residual_mean_bps`
- `residual_std_bps`

只有同时具备有效参考输入的样本进入残差统计。整分钟没有有效残差时相关字段为空，并保留原有订单簿分钟统计。

### 兼容和迁移

首次使用新版本启动时，现有 schema 检测会把旧文件保留为下一个未占用的 `.old`、`.old.1` 等名称，然后创建带新表头的文件。不会原地转换、合并或覆盖旧数据。

`tools/analyze.py` 保持兼容旧 `minutes.csv`。发现新字段时额外输出参考基差、残差和资金费分布，并支持直接读取 `.csv.gz`。旧文件缺少新列时继续输出当前溢价分析，不伪造参考结果。

## 六、状态化告警

参考观测使用独立状态机，不复用交易信号状态：

- `abs(signed_residual_bps) >= reference.residual_alert_bps` 连续保持 `reference.residual_persist_sec` 后记录一次开始告警。
- 回到阈值内时记录一次恢复。
- 任一所需参考数据超过 `reference.stale_sec` 时记录一次过期告警。
- 有效 WebSocket 或 REST 数据恢复时记录一次恢复。
- 状态未变化时不重复输出。

默认参数为 20 bps、30 秒持续、60 秒过期。告警只写现有日志系统，不新增外部通知依赖，不改变开仓或平仓判断。

## 七、`signals.csv` 每日轮转与压缩

按 UTC 自然日轮转，只轮转信号明细文件：

1. 日期变化时刷新并关闭当前 `signals.csv`。
2. 将其重命名为带 UTC 日期的文件。默认路径生成 `signals-YYYYMMDD.csv`；自定义路径 `dir/name.csv` 生成 `dir/name-YYYYMMDD.csv`。日期取该文件最后已写入记录的 UTC 日期，目标存在时使用 `.1`、`.2` 等未占用名称。
3. 压缩到同目录临时 `.gz` 文件。
4. 完成写入、刷新和关闭后校验 gzip 可完整读取，再用原子改名发布最终 `.gz`。
5. 仅在压缩成功后删除对应未压缩归档。
6. 压缩失败时保留未压缩归档，记录异常，并继续创建具有完整表头的新 `signals.csv`。

不自动删除历史 `.csv` 或 `.csv.gz`。进程正常关闭时只刷新并关闭当前文件，不因关闭而强制轮转。

默认开启每日轮转；配置位于 `recorder`：

```yaml
recorder:
  signal_rotate_daily: true
```

## 八、错误隔离

- 参考频道解析只捕获明确预期的字段/数值错误，记录交易所、频道和必要字段上下文；不输出凭据或完整敏感消息。
- 可识别的参考错误保留最后有效快照，不影响订单簿 ready 状态。
- 未预期异常不得吞掉，继续由任务监督和现有重连/停机机制感知。
- REST 恢复保留 HTTP 状态和交易所上下文，采用既定 15 秒间隔，不做紧密重试。
- CSV 序列化、刷新和轮转错误沿用 fail-fast 原则；压缩阶段失败是可恢复归档错误，必须保留原始数据并让当前新文件继续记录。

## 九、测试与验证

### 额度

- 多档 ask 导致平均买价上升时，`buy_notional` 不超过上限。
- 大幅正溢价使卖出腿金额更大时，`sell_notional` 不超过上限。
- 大幅负溢价的反向交易同样约束两腿。
- `take_fraction`、`q_max`、`size_step`、`min_base` 和 `min_notional` 组合边界。
- 精度取整后最终计划满足两腿额度不变量。

### 参考数据

- Hyperliquid `activeAssetCtx` 的订阅、市场过滤、字段解析和资金费转换。
- Lighter `market_stats` 的订阅、market id 过滤、字段解析和资金费转换。
- REST 初始化、过期后恢复、WebSocket 恢复后停止轮询。
- 错误、非有限、非正价格和乱序消息不覆盖最后有效快照。
- 参考消息错误不清空或改变订单簿。

### 计算与告警

- 两个交易方向的参考基差、有符号溢价、残差和资金费符号。
- 缺少任一必要输入时派生字段为空。
- 20 bps 阈值、30 秒持续、恢复、60 秒过期和去重。
- 参考状态缺失或过期不改变同一订单簿输入下的原交易计划结果。

### CSV 与分析器

- 新表头和字段顺序稳定。
- 分钟残差只聚合有效参考样本。
- 旧 schema 归档到未占用 `.old` 名称。
- UTC 跨日轮转、文件重名、gzip 成功、gzip 失败保留原始文件和正常关闭。
- `tools/analyze.py` 兼容旧 CSV、新 CSV 和 `.csv.gz`。

### 完成验证

- 运行完整 pytest 测试套件。
- 对修改的 Python 文件运行编译检查。
- 网络协议测试使用固定的官方示例帧和模拟 HTTP 响应，默认测试不依赖公网。
- 检查工作区差异，确认没有阈值、交易方向、手续费、仓位或下单流程的无关变化。

## 十、验收标准

1. 任意成功返回的套利计划，两条腿的计划名义金额均不超过 `max_order_notional_usd`。
2. 现有订单簿输入和有效交易配置下，除额度缩小外，策略方向和阈值行为保持不变。
3. 两边参考数据优先来自现有 WebSocket，REST 仅负责初始化和失效恢复。
4. 参考数据缺失、过期或解析失败不会阻止交易，也不会污染最后有效值。
5. 新 CSV 能区分订单簿价差、oracle/index 结构性基差、残差和每小时净资金费。
6. 残差与参考数据告警不会重复刷屏，也不会改变交易行为。
7. `signals.csv` 按 UTC 每日轮转并在安全校验后压缩，任何压缩失败都不丢失原始数据。
8. 旧数据保持可恢复，旧分析流程继续可用。
