# Entropy 多对冲交易所套利架构设计

**状态：** 已确认

**日期：** 2026-08-27

**范围：** Entropy 固定为主腿、单币种单进程、每笔选择一个最优对冲交易所

## 1. 目标

以当前开源版本为基线，把固定的 `Entropy + 一个 hedge` 双交易所机器人改造成可插拔的多对冲交易所套利系统：

- Entropy 始终是主腿，同时连接多个对冲交易所。
- 每次机会只选择一个综合净收益最优的对冲交易所，不拆单到多个交易所。
- 单个进程只运行一个交易品种；多品种通过多进程部署隔离。
- 新增交易所只需要新增适配器、凭证配置和适配器契约测试，不修改策略、路由和风控核心。
- 开平仓阈值由 1–6 小时短线数据动态生成。
- 滑点保护根据真实成交动态更新，同时保留明确的硬上限。
- 所有关键决策、订单状态、仓位和异常都可追踪、可恢复。
- 在 Linux VPS 上通过实测选择部署区域并降低本机关键路径延迟。

## 2. 非目标

第一阶段不包含以下能力：

- 任意两个非 Entropy 交易所之间的套利。
- 单进程同时交易多个币种。
- 一次交易把对冲腿拆到多个交易所。
- 多机房或多进程协同下单。
- 资金费率直接阻止开仓；第一阶段只记录和告警。
- 根据模拟成交运行的实时模拟盘。允许 `observe-only` 观察模式和确定性的历史订单簿回放，但不伪造成交。

## 3. 当前基线

当前代码已经具备以下可复用能力：

- Hyperliquid 和 Lighter 的真实订单簿订阅。
- 双腿并发吃单、成交确认、部分成交后的净敞口修复。
- 订单簿逐档计算、手续费感知的套利规模计算。
- 行情新鲜度、下单频率、持仓上限、故障暂停和仓位对账。
- 分钟行情记录、阈值分析工具和终端仪表盘。

当前核心限制是配置、引擎、记录器和仪表盘都围绕 `self.entropy + self.hedge` 建模。交易所选择和交易所实现也直接进入 `engine.py`，无法在不修改核心逻辑的情况下增加新交易所。

设计检查时，本机测试结果为 `27 passed, 2 failed`：

- `config.example.yaml` 在 Windows 默认 GBK 编码下读取失败，配置文件需要显式使用 UTF-8。
- 仪表盘测试受终端宽度影响，`sell_entropy` 被截断为 `sell_e…`，测试渲染宽度需要固定。

这两个基线问题必须在架构迁移前修复，确保后续重构有可信的回归基准。

## 4. 已确认的设计决策

1. Entropy 固定为主腿。
2. 单币种单进程。
3. 每笔只选择一个最优对冲交易所。
4. 动态价差采用分钟级短线模型，主窗口可配置为 1–6 小时。
5. 默认使用 3 小时主窗口和 1 小时异常检测窗口。
6. 资金费率只记录和告警，不阻止开仓。
7. 持仓软时限为 30 分钟，硬时限为 2 小时。
8. 动态滑点使用最近一小时数据；不足时最多向前扩展到最近 50 笔成交。
9. 全局额度和每个交易所额度都采用配置文件中的固定值。
10. 目标生产环境是 Linux VPS。

## 5. 总体架构

```text
Entropy 行情 ─┐
              ├─ MarketState ─ OpportunityEngine ─ VenueRouter
多个对冲所 ───┘                                  │
                                                ▼
RiskManager ───────────────────────────── ExecutionCoordinator
                                                │
                       ┌────────────────────────┼───────────────┐
                       ▼                        ▼               ▼
                 EntropyAdapter          LighterAdapter    XYZAdapter

所有模块 ──审计事件──> StorageWriter ──> SQLite WAL
```

### 5.1 模块职责

- `VenueAdapter`：封装交易所行情、市场规格、账户、仓位、资金费率、下单和成交确认。
- `MarketState`：保存各交易所带接收时间、交易所时间和序列号的标准化订单簿。
- `StrategyModel`：计算动态中枢、上下分位带、异常状态和持仓退出目标。
- `OpportunityEngine`：为每个 `Entropy ↔ 对冲所`、每个方向生成可执行候选。
- `VenueRouter`：过滤不可用候选，并选择预期净收益最高的一家对冲所。
- `RiskManager`：管理固定额度、容量预留、持仓时限、异常滑点、熔断和恢复状态。
- `ExecutionCoordinator`：执行双腿订单，解析成交结果，处理部分成交和未知状态。
- `StorageWriter`：在关键路径之外持久化行情、决策、订单、成交和风险事件。
- `Engine`：只编排生命周期和模块通信，不包含交易所特有判断或策略公式。

### 5.2 核心不变量

- 全局同一时间最多有一笔套利执行中，因为所有机会共用 Entropy 主腿。
- 两腿可以并发发送，但下一次路由必须等当前执行结果明确或进入恢复状态。
- 每个对冲所的仓位批次独立记录，不能用其他交易所的反向仓位掩盖风险。
- 任何订单状态未知时都不得盲目重发。
- Entropy 状态未知时，整个策略禁止新增仓位。
- 配置、状态恢复或关键审计写入异常时，系统明确失败并停止新增仓位。

## 6. 统一交易所适配器

适配器使用统一的标准化数据结构，并至少提供以下能力：

```python
class VenueAdapter(Protocol):
    venue_id: str
    role: Literal["anchor", "hedge"]
    capabilities: VenueCapabilities

    async def load_market(self, symbol: str) -> MarketSpec: ...
    async def start(self, sink: MarketEventSink) -> None: ...
    async def stop(self) -> None: ...
    async def fetch_account(self) -> AccountSnapshot: ...
    async def fetch_position(self) -> PositionSnapshot: ...
    async def fetch_funding(self) -> FundingSnapshot: ...
    async def place_taker(self, request: OrderRequest) -> OrderAck: ...
    async def resolve_order(self, client_order_id: str) -> OrderOutcome: ...
```

`MarketSpec` 统一表达价格精度、数量精度、最小基础数量、最小名义金额和交易状态。`OrderOutcome` 必须区分已成交、部分成交、明确拒绝、明确未成交和状态未知，不能用空成交量混淆未知状态。

适配器通过代码中的显式注册表创建：

```python
ADAPTER_FACTORIES = {
    "hyperliquid": HyperliquidAdapter,
    "lighter": LighterAdapter,
}
```

配置文件不能提供任意 Python 导入路径。新增交易所需要显式加入注册表并通过适配器契约测试。

## 7. 行情与动态价差模型

### 7.1 标准化价差

对每个对冲所 `H` 计算：

```text
mid_premium_bps = (Entropy_mid / H_mid - 1) × 10,000
rich_exec_bps   = (Entropy_bid / H_ask - 1) × 10,000
cheap_exec_bps  = (Entropy_ask / H_bid - 1) × 10,000
```

- Entropy 偏贵时，卖 Entropy、买 H，使用 `rich_exec_bps`。
- Entropy 偏便宜时，买 Entropy、卖 H，使用 `cheap_exec_bps`。

中枢和分位带使用分钟 `mid_premium_bps`，实际开仓和规模始终使用实时可成交盘口。

### 7.2 主窗口

- 默认主窗口：180 个有效分钟，可配置范围为 60–360 分钟。
- 默认最低样本：120 个有效分钟。
- 中枢：窗口内 `mid_premium_bps` 的中位数。
- 上边界：窗口内 `mid_premium_bps` 的 95% 分位数。
- 下边界：窗口内 `mid_premium_bps` 的 5% 分位数。
- 上下边界分别计算，不假设分布对称或服从正态分布。
- 模型每分钟更新一次；实时 tick 只用于验证和执行，不重新拟合模型。

样本不足时状态为 `MODEL_NOT_READY`，明确禁止新增仓位，但允许减仓和平仓。

### 7.3 一小时异常检测

最近 60 个有效分钟用于检测：

- 短窗口中位数与主窗口中枢的偏移是否超过主窗口四分位距。
- 短窗口四分位距是否超过主窗口四分位距的两倍。
- 连续无效或缺失分钟是否达到 5 分钟。

满足任一条件时进入 `REGIME_UNSTABLE`：禁止新增仓位、保留减仓能力并产生风险事件。状态连续 15 个有效分钟恢复正常后，才重新允许开仓。

### 7.4 开仓条件

候选必须同时满足统计条件和经济条件：

```text
Entropy 偏贵：rich_exec_bps >= 上边界
Entropy 偏低：cheap_exec_bps <= 下边界

expected_net_profit_usd
= sell_proceeds
- buy_cost
- taker_fees
- dynamic_slippage_reserve
```

`expected_net_profit_usd` 必须大于按成交名义计算的 `strategy.min_expected_profit_bps`；默认值为 2 bps。订单簿逐档计算只纳入仍满足上述条件的边际深度。

### 7.5 持仓批次与退出

每个对冲交易所最多维护一个活动 `PositionCampaign`。同方向后续成交合并进该批次，并更新加权平均成本；批次未归零前不能反向开仓。

状态机为：

```text
FLAT → OPENING → OPEN → CLOSING → FLAT
                    │
                    ├─ 30 分钟 → SOFT_EXPIRED
                    └─ 2 小时  → HARD_EXPIRED
```

开仓时冻结该批次的中枢、上下边界、退出带、成本估计和模型版本。退出带定义为对应入场带宽的 25%，但不得小于 0.5 bps：

```text
exit_band_bps = max(0.5, 0.25 × entry_band_bps)
```

- 卖 Entropy/买对冲所的批次：当反向可成交价差回到冻结中枢上方 `exit_band_bps` 以内时允许平仓。
- 买 Entropy/卖对冲所的批次：当反向可成交价差回到冻结中枢下方 `exit_band_bps` 以内时允许平仓。
- 平仓仍必须逐档检查手续费和滑点，但不要求达到新的开仓最低利润。
- 动态中枢后续漂移只能触发告警和风险状态，不能重写已有批次的退出目标。

30 分钟软时限触发后禁止该批次继续加仓，并提高其平仓路由优先级。2 小时硬时限触发后，在交易所级硬滑点上限内分批减仓；如果无法安全退出，保持停止新增仓位并要求人工处理，不无限扩大保护价。

## 8. 多交易所候选与路由

每次有效行情更新后，`OpportunityEngine` 为所有健康对冲所计算两个方向的候选。候选先经过以下硬过滤：

- Entropy 和对冲所行情均新鲜且序列连续。
- 行情、账户和成交回报通道均处于可交易状态。
- 交易所未被限频、暂停、隔离或标记为恢复中。
- 全局额度、Entropy 额度和该对冲所固定额度充足。
- 订单满足双方最小数量、最小名义和精度要求。
- 发单预算和保证金充足。
- 统计偏离与最低预期净利润均满足要求。

候选按 `expected_net_profit_usd` 从高到低排序。在同等净收益下，依次优先：

1. 预计成交滑点更小；
2. 下单到成交 p95 延迟更低；
3. 剩余交易所额度比例更高；
4. `venue_id` 字典序，用于保证测试和回放结果确定。

路由器只选择一个对冲所。选择结果包含行情版本、模型版本、风险快照和决策 ID。发单前必须在同一执行锁内重新验证行情版本、预期净收益和额度，并原子预留本次额度。候选失效时放弃本次决策并重新扫描，不追价，也不直接无条件切换第二名。

所有未选候选都记录明确原因，例如 `LOWER_NET_PROFIT`、`CAP_EXHAUSTED`、`STALE_BOOK`、`VENUE_PAUSED` 或 `MODEL_NOT_READY`。

## 9. 动态滑点

动态滑点按“交易所实例 + 买卖方向”分别建模，使用实际成交均价与决策时订单簿预期均价之间的不利偏差。

- 优先使用最近一小时的成交样本。
- 一小时少于 20 笔时向前扩展，但最多使用最近 50 笔。
- 使用不利滑点的 95% 分位数，加配置化安全边际。
- 结果限制在交易所配置的 `min_bps` 和 `hard_max_bps` 之间。
- 少于 10 笔历史成交时使用显式配置的 `bootstrap_bps`，并标记为 `BOOTSTRAP`。

动态滑点只收紧或放宽保护价和可成交规模，不能越过 `hard_max_bps`。盘口在决策后变化导致预期净利润不足时直接放弃，不扩大滑点追价。

异常滑点按顺序触发：

1. 单次超过动态预算：记录异常并重新计算统计。
2. 最近 10 笔中 3 笔超过预算：该所订单规模减半。
3. 最近 10 笔中 5 笔超过预算或任一笔超过硬上限：暂停该所新增仓位 15 分钟。
4. 暂停期结束后重新预热和探测；未恢复则继续暂停。

## 10. 风控与额度

额度使用配置中的固定值，不根据实时余额自动扩大：

- `risk.global_gross_position_usd`：所有活动批次总名义上限。
- `risk.anchor_max_position_usd`：Entropy 绝对持仓上限。
- `venues.<id>.max_position_usd`：每个对冲所独立上限。
- `venues.<id>.max_order_notional_usd`：单笔上限。
- `venues.<id>.max_orders_per_min`：滑动 60 秒下单预算。

风险检查同时使用当前真实仓位、未决订单最大可能成交量和已预留额度。额度在发单前预留，订单明确结束后按实际成交释放或转换为持仓占用，防止并发超配。

对冲所故障时，只隔离该所的新增交易；其已有批次继续独立跟踪和告警。只有在 Entropy 状态明确、实际净敞口已修复或仍在全局上限内时，其他对冲所才可以继续运行。Entropy 行情、订单或仓位状态不明确时，全局停止新增交易。

## 11. 双腿执行与故障处理

`ExecutionCoordinator` 在全局执行锁内完成以下流程：

1. 重新验证行情版本、模型状态、净利润和额度。
2. 生成统一决策 ID 和两腿客户端订单 ID。
3. 向后台写入器提交包含两腿客户端订单 ID 的 `ORDER_INTENT`，并等待 SQLite 事务提交回执。
4. 并发发送两腿 IOC/受保护市价单。
5. 等待两腿明确结果或进入未知状态。
6. 更新真实成交、额度和持仓批次。
7. 必要时执行 `reduce-only` 修复。
8. 释放执行锁并重新扫描最新行情。

故障矩阵：

| Entropy 腿 | 对冲腿 | 处理 |
|---|---|---|
| 等量成交 | 等量成交 | 正常完成 |
| 已知成交 | 已知少成交/未成交 | 对已知净敞口执行 `reduce-only` 修复 |
| 已知未成交 | 已知成交 | 在对冲所减少已成交仓位 |
| 状态未知 | 任意状态 | 全局停止新增交易，查询订单与真实仓位 |
| 状态明确 | 对冲腿未知 | 隔离该所并对账；Entropy 状态及净敞口确认后，其他交易所方可继续 |
| 对账仍不一致 | 任意状态 | 进入 `RECOVERY_REQUIRED`，保持停机并高优先级告警 |

未知订单不能以相同或新客户端订单 ID 盲目重试。恢复流程必须先查询订单状态，再查询真实仓位，并把结果与本地审计记录对齐。

## 12. 资金费率

每个适配器按交易所能力采集：

- 当前资金费率；
- 下一结算时间；
- 预测或下一期资金费率（交易所提供时）；
- 数据来源时间和新鲜度。

资金费率写入候选、持仓批次和收益归因，但第一阶段不改变开仓结果。以下情况产生告警：

- 任一腿资金费率数据超过两个采集周期未更新；
- 按当前仓位估算的下一期净资金费成本超过该批次预期价差收益的 50%；
- 下一次资金费结算发生在硬持仓时限之前，且方向为净支出。

这些告警只提示和展示，不自动阻止开仓。

## 13. 配置结构

配置继续严格校验未知键和类型。交易品种进入配置，单个进程只允许一个 `runtime.symbol`：

```yaml
runtime:
  symbol: SNDK

venues:
  entropy:
    adapter: hyperliquid
    role: anchor
    enabled: true
    dex: io
    credential_env_prefix: ENTROPY
    taker_fee_bps: 0.0
    max_position_usd: 3000
    max_order_notional_usd: 300
    max_orders_per_min: 120
    slippage:
      bootstrap_bps: 20
      min_bps: 2
      hard_max_bps: 50

  lighter_mainnet:
    adapter: lighter
    role: hedge
    enabled: true
    profile: mainnet
    credential_env_prefix: LIGHTER_MAINNET
    taker_fee_bps: 0.0
    max_position_usd: 1000
    max_order_notional_usd: 200
    max_orders_per_min: 30
    slippage:
      bootstrap_bps: 20
      min_bps: 2
      hard_max_bps: 50

strategy:
  main_window_minutes: 180
  anomaly_window_minutes: 60
  min_valid_minutes: 120
  lower_quantile: 0.05
  upper_quantile: 0.95
  min_expected_profit_bps: 2.0
  soft_holding_limit_minutes: 30
  hard_holding_limit_minutes: 120

risk:
  global_gross_position_usd: 3000
  anchor_max_position_usd: 3000

execution:
  settle_timeout_sec: 10
  dynamic_slippage_safety_bps: 1
  reconcile_sec: 15

funding:
  poll_sec: 60
  stale_after_sec: 180
  expected_profit_alert_ratio: 0.5

observability:
  sqlite: logs/entropy-arb.sqlite3
  log_file: logs/engine.log
  queue_size: 10000
```

环境变量按交易所实例前缀隔离。例如 `LIGHTER_MAINNET_ACCOUNT_INDEX` 和 `LIGHTER_RH_ACCOUNT_INDEX` 不共享。适配器定义该类型需要哪些环境变量；缺少实盘凭证时启动直接失败。

## 14. 状态持久化与恢复

SQLite 使用 WAL 模式并由单独后台写入器批量写入。核心表包括：

- `minute_bars`：交易所分钟盘口、价差和数据质量。
- `model_snapshots`：动态中枢、分位带、异常状态和模型版本。
- `opportunities`：全部候选、成本、评分、选择结果和拒绝原因。
- `campaigns`：持仓批次、冻结模型、加权成本、期限和状态。
- `orders`：客户端订单 ID、交易所订单 ID、请求、确认和最终状态。
- `fills`：成交数量、均价、手续费和接收时间。
- `funding_rates`：资金费率、结算时间和来源时间。
- `risk_events`：断线、限频、滑点异常、超时、熔断和人工恢复状态。

关键审计事件进入有界队列。订单意图属于写前日志：后台写入器完成 SQLite 事务后向执行器返回提交回执，执行器收到回执后才能调用交易所下单接口。队列满、提交超时或后台写入器失败时必须产生明确错误并停止新增仓位，不能静默丢弃或绕过写前日志。订单结果和风险状态同样要求提交回执；普通行情只按分钟聚合后异步写入，避免高频 tick 落盘拖慢策略。

重启流程：

1. 读取所有非终态订单和未结束持仓批次。
2. 连接交易所并获取真实未结订单、仓位、余额和市场规格。
3. 按客户端订单 ID、交易所订单 ID 和真实仓位进行对齐。
4. 完全一致时恢复持仓批次和期限计时。
5. 不一致时进入 `RECOVERY_REQUIRED`，禁止开仓并要求人工确认。

现有 `logs/minutes.csv` 提供一次性只读导入工具，导入时显式记录来源文件和导入时间。

## 15. Linux VPS 与延迟

第一阶段使用单 VPS、单进程。所有延迟节点同时记录 UTC 时间和 monotonic 时间：

```text
行情接收 → 订单簿更新 → 候选计算 → 路由完成
→ 两腿开始发送 → 交易所确认 → 成交确认 → 仓位对账
```

部署要求：

- Linux 使用 `chrony` 同步时钟；时钟偏差超过 100 ms 时禁止开仓。
- 验证兼容性后启用 `uvloop`。
- 每个交易所使用独立 HTTP 连接池和 WebSocket 生命周期。
- HTTP/TLS 连接预热、DNS 缓存、WebSocket 心跳和订单通道保活。
- 启动后完成行情、账户、成交回报和下单链路预热才进入可交易状态。
- 生产环境关闭 Rich 仪表盘。
- 文件日志使用 `QueueHandler/QueueListener` 或等价后台写入机制。
- 关键路径不得执行同步文件写入、同步 DNS 查询或阻塞式网络请求。

VPS 区域通过探针实测选择，指标包括各交易所 WebSocket 更新间隔、断线率、订单请求 RTT、下单到确认、下单到成交以及 p50/p95/p99。目标是最小化 Entropy 与主要对冲所的综合执行延迟，而不是只优化一家交易所的 ping。

本机代码路径的验收目标是：不含外部网络时间时，行情进入到路由完成的 p95 小于 10 ms；路由完成、写前日志提交到订单发送调用启动的 p95 小于 10 ms。安全所需的写前日志不能为追求延迟而绕过。外部网络延迟只做观测和区域对比，不伪造固定 SLA。

## 16. 代码模块规划

```text
entropy_arb/
  config.py                 严格配置契约和环境变量解析
  models.py                 标准行情、订单、成交、候选、批次数据结构
  market.py                 多交易所行情状态和新鲜度
  strategy.py               动态模型、异常检测和持仓状态机
  router.py                 候选过滤、排序和额度预留
  risk.py                   全局/交易所额度、期限和熔断
  execution.py              双腿执行、订单解析、补救和恢复
  storage.py                SQLite 后台写入和恢复读取
  engine.py                 生命周期和模块编排
  venues/
    __init__.py
    base.py                 VenueAdapter 协议与能力声明
    registry.py             显式适配器工厂注册
    hyperliquid.py          Entropy 和 trade.xyz 实现
    lighter.py              Lighter 主网和 Robinhood 实现
```

现有 `book.py` 保留订单簿和逐档深度计算职责。现有 `feeds.py`、`venue_hl.py` 和 `venue_lighter.py` 的逻辑分阶段迁移进对应适配器；迁移完成前不删除旧实现。

## 17. 测试设计

### 17.1 基线与单元测试

- 配置文件显式 UTF-8 读取和严格键校验。
- 订单簿快照、增量、乱序、重复和断线恢复。
- 动态中位数、上下分位数、最小样本和模型冻结。
- 离群尖峰、中枢突变、波动骤增和连续数据缺口。
- 持仓软时限、硬时限、禁止反向翻仓和批次归零。
- 手续费、动态滑点、最小利润、逐档深度和固定额度。
- 路由排序、确定性平局处理和所有拒绝原因。

### 17.2 适配器契约测试

每个适配器使用固定的交易所消息 fixture 验证：

- 市场规格标准化。
- 行情订阅和新鲜度。
- 下单请求精度和方向。
- 已成交、部分成交、拒绝、未成交、超时和未知状态映射。
- 余额、仓位和资金费率标准化。
- 重连后序列和订单回报恢复。

### 17.3 故障注入

- Entropy 成交、对冲腿拒绝。
- Entropy 拒绝、对冲腿成交。
- 两腿不同数量的部分成交。
- 一腿超时后最终成交。
- 重复或乱序成交回报。
- 下单返回未知后进程重启。
- SQLite 写入器失败或队列满。
- 行情断线、账户通道断线、限频和异常滑点。

### 17.4 回放与灰度

- 使用历史分钟数据验证动态模型状态和阈值。
- 使用更高频订单簿数据确定性回放候选、路由和持仓状态机。
- `observe-only` 同时记录固定阈值与动态阈值的决策差异，不发送订单。
- 小额实盘按交易所逐一启用，再启用多交易所路由。
- 每次扩大额度前检查成交成功率、实际滑点、单腿失败率、持仓时间和恢复事件。

## 18. 分阶段迁移

### 阶段 0：恢复可信基线

- 新建开发分支。
- 修复 UTF-8 配置读取和确定性仪表盘测试宽度。
- 固化当前双交易所信号、深度和执行的回归测试。
- 验收：全量测试通过，业务行为没有改变。

### 阶段 1：抽取统一数据结构和适配器接口

- 引入 `models.py`、`venues/base.py` 和 `venues/registry.py`。
- 用包装方式接入现有 Hyperliquid/Lighter 实现。
- 仍然只运行一个对冲所。
- 验收：原有命令和双交易所行为保持一致。

### 阶段 2：多对冲所行情与统一状态

- 同时启动多个对冲所适配器。
- 引入 `MarketState`、统一健康状态和标准化分钟记录。
- 增加 `observe-only`，只记录候选不下单。
- 验收：任一对冲所断线不影响其他行情采集。

### 阶段 3：固定阈值多交易所路由

- 引入 `OpportunityEngine`、`VenueRouter` 和双层固定额度。
- 暂时继续使用当前固定阈值，隔离路由变量。
- 小额实盘验证最优交易所选择和单所故障隔离。
- 验收：每次只执行一个对冲所，路由结果可解释且可回放。

### 阶段 4：持仓批次、审计和恢复

- 引入 `PositionCampaign`、SQLite WAL、订单生命周期和重启恢复。
- 启用 30 分钟软时限和 2 小时硬时限。
- 验收：故障注入和重启测试全部通过，未知状态不会重发订单。

### 阶段 5：动态价差影子运行

- 引入 3 小时主模型、1 小时异常检测和冻结退出目标。
- 与固定阈值并行记录至少一周，不改变真实下单。
- 验收：模型无未来数据泄漏，样本不足和 regime 异常时明确禁止开仓。

### 阶段 6：启用动态阈值

- 从最小交易所额度开始启用动态开平仓。
- 逐级检查触发频率、净收益、最大不利偏移和持仓期限。
- 验收：动态模型的实盘决策、模型快照和实际结果能够完整关联。

### 阶段 7：动态滑点和资金费率告警

- 启用成交滑点分位数、启动值、缩量和暂停规则。
- 接入各所资金费率记录、收益归因和告警。
- 验收：异常滑点能按规则降级，资金费率缺失不会被静默忽略。

### 阶段 8：Linux VPS 延迟优化

- 部署区域探针并比较候选机房。
- 启用异步日志、独立连接池、连接预热、时钟检查和 `uvloop`。
- 验收：本机关键路径达到 p95 目标，所有外部延迟均有分位指标。

## 19. 总体验收标准

- 当前支持的 Entropy、Lighter 主网、Lighter Robinhood 和 trade.xyz 全部通过统一适配器契约。
- 添加一个测试用交易所适配器时，不需要修改策略、路由、风险和执行模块。
- 多个对冲所同时在线时，每次交易只选择一个对冲所，并能解释未选原因。
- 模型不足、行情异常、Entropy 状态未知、关键存储失败时不会新增仓位。
- 所有订单都能从决策 ID 追踪到行情版本、模型版本、风险快照、订单和成交。
- 每个对冲所的持仓批次、期限和真实仓位独立可见。
- 单腿失败、部分成交、订单未知和进程重启均有自动化测试。
- 全量测试在目标 Linux 环境通过；当前 Windows 开发环境的编码和渲染测试也保持通过。
- 小额灰度完成前不扩大仓位额度。

## 20. 主要风险

- 动态中枢可能在结构性价差变化时追随错误 regime，因此已有持仓必须冻结退出目标。
- 多交易所增加了订单状态组合和恢复复杂度，因此先统一适配器，再增加路由，最后启用动态策略。
- 单 VPS 无法同时靠近所有交易所，因此区域选择必须基于综合执行数据。
- 资金费率第一阶段不阻止开仓，极端费率仍可能侵蚀收益；必须保留清晰告警和收益归因。
- 硬持仓时限不等于无限滑点强平。无法在硬滑点上限内退出时，系统会停机告警并等待人工处理。
