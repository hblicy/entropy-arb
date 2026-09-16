# 动态 Residual 策略与批次风控设计

**状态：** 已确认

**日期：** 2026-09-14

**范围：** Entropy `ANTH-USDC` 与 Robinhood Chain Lighter `ANTHROPIC`，单币种、单对冲交易所、固定额度

## 1. 背景与目标

现有引擎使用固定原始价差开仓。43.1 小时实测数据表明，原始价差与 Entropy oracle / Lighter index 参考基差的相关性为 0.926；当前 `midline_bps=0`、上下阈值各 4 bps 时，约 98% 的秒级样本会触发 `buy_entropy`。这类信号主要来自两个市场参考价格体系的结构性偏差，而不是短期可回归错价。

本次目标是：

1. 以扣除参考基差后的 signed residual 生成动态开仓信号。
2. 引入单活动批次状态，明确区分开仓、同向加仓、正常平仓、软退出和硬退出。
3. 用完整往返的收敛空间、手续费和滑点预算评估机会。
4. reference 不完整、不新鲜或不同步时禁止新增风险。
5. 让批次状态跨进程重启恢复，无法可靠恢复时 fail closed。
6. `--record-only` 运行与实盘相同的影子策略并输出低频、可分析的批次事件。

## 2. 非目标

- 不在本阶段增加第二个对冲交易所或多交易所路由。
- 不改变现有双腿并发下单、未知订单确认、净敞口修复和交易所限频恢复流程。
- 不用 mark price 替代缺失的 Entropy oracle 或 Lighter index。
- 资金费继续只记录和告警，不作为开仓阻断条件。
- 不把影子计划成交当作真实成交，也不用影子结果训练真实滑点模型。
- 不自动启用实盘；完成实现后仍先运行影子模式。
- 不修改现有 `minutes.csv` 和 `signals.csv` 表头。

## 3. 策略组件

新增 `entropy_arb/strategy.py`，保持交易所无关，并提供三个边界清晰的组件。

### 3.1 `ResidualModel`

模型维护按 UTC 分钟编号的 mid residual 样本。启动时从配置的 `minutes.csv` 读取最近窗口，随后在内存中每分钟更新一次。

输入样本必须满足：

- CSV 身份字段与当前 Entropy symbol、dex、hedge symbol 和 venue 完全匹配；
- `residual_close_bps` 为有限数；
- 两边 reference age 不超过开仓 reference 年龄上限；
- `reference_update_skew_ms` 不超过开仓更新时间差上限；
- 时间戳不在当前时间之后，且位于主窗口覆盖的真实时间范围内。

相同分钟后到的样本覆盖该分钟旧值；乱序的更早分钟可以进入窗口，但不能让模型版本倒退。模型每完成一个新的分钟收盘样本后增加版本号。

输出 `ModelSnapshot`：模型版本、样本数、中位数、10%/25%/75%/90% 分位数、IQR、状态和生成时间。分位数使用线性插值，确保离线回放和实时计算确定一致。

### 3.2 `PositionCampaign`

每个进程最多一个活动套利批次。批次字段包括：

- campaign ID、模式（`shadow` 或 `live`）、交易对身份；
- 方向、状态和首次开仓 UTC 时间；
- 两腿当前匹配 base 数量及加权成交价格；
- 冻结的模型版本、中枢、入场边界和退出目标；
- 已发生手续费、已实现收益和最近一次状态变化时间。

状态流为：

```text
FLAT -> OPEN -> SOFT_EXIT -> HARD_EXIT -> FLAT
```

订单提交和未知结果仍由现有引擎恢复状态管理；批次只记录已经明确的实际成交。批次未归零前禁止反向开仓。

### 3.3 `StrategyDecision`

策略输出单一决策值：`OPEN`、`ADD`、`CLOSE`、`FORCED_CLOSE` 或 `SKIP`。决策携带方向、原因、模型快照、实时 residual、预计收敛空间、手续费、滑点预算、数量和名义金额。执行引擎只消费决策，不在下单路径内重新实现统计规则。

## 4. 动态模型

默认配置为：

```yaml
strategy:
  mode: residual_dynamic
  live_enabled: false
  window_minutes: 180
  min_samples: 120
  lower_quantile: 0.10
  upper_quantile: 0.90
  regime_window_minutes: 60
  regime_recovery_minutes: 15
  exit_band_fraction: 0.25
  min_exit_band_bps: 0.5
  min_expected_profit_bps: 2.0
  soft_hold_minutes: 60
  hard_hold_minutes: 360
  entry_reference_max_age_sec: 15
  entry_reference_max_skew_sec: 15
  state_file: logs/campaign-state.json
  event_csv: logs/strategy-events.csv
```

每分钟的建模值为：

```text
mid_residual_bps
= (Entropy_mid / Hedge_mid - 1) * 10000
  - reference_basis_bps
```

主窗口使用最近 180 个真实分钟内的有效样本，而不是简单取文件最后 180 行。少于 120 个有效分钟时状态为 `MODEL_NOT_READY`，禁止开仓和加仓，但不阻止已有批次减仓。

最近 60 分钟用于 regime 检测。以下任一条件成立时状态为 `REGIME_UNSTABLE`：

1. 短窗口中位数与主窗口中位数的绝对差大于主窗口 IQR；
2. 短窗口 IQR 大于主窗口 IQR 的两倍；
3. 连续 5 个真实分钟没有有效 residual。

若主窗口 IQR 为零，则任何非零中位数偏移或短窗口 IQR 都视为不稳定。进入不稳定状态后，需要连续 15 个新的有效分钟均通过检测才恢复 `READY`。

## 5. 实时 residual 与 reference 门禁

```text
reference_basis_bps
= (Entropy_oracle / Hedge_index - 1) * 10000

sell_entropy signed residual
= (Entropy_bid / Hedge_ask - 1) * 10000 - reference_basis_bps

buy_entropy signed residual
= (Entropy_ask / Hedge_bid - 1) * 10000 - reference_basis_bps
```

开仓和加仓要求：

- Entropy oracle、Lighter index 和四个最优盘口价格均为有效有限正数；
- 两边 reference age 均不超过 15 秒；
- reference 更新时间差不超过 15 秒；
- 两边订单簿通过现有 freshness、ready、限频、故障和下单预算检查；
- 模型状态为 `READY`。

reference 的 60 秒 stale 告警和 REST 恢复配置保持不变。15 秒门禁只控制新增风险，不改变恢复任务频率。

reference 不满足门禁时：空仓禁止开仓；持仓禁止加仓；正常 residual 回归条件不可计算。达到 6 小时后，强制退出只依赖新鲜订单簿，不要求 reference 可用。

## 6. 开仓、加仓和收益估计

空仓时：

- `sell_entropy` 的可执行 signed residual 大于等于模型 90% 分位数时形成候选；
- `buy_entropy` 的可执行 signed residual 小于等于模型 10% 分位数时形成候选。

同方向活动批次只在实时 residual 仍达到该批次冻结的入场边界时允许加仓。后续模型变化不能降低该批次的加仓标准。`SOFT_EXIT` 或 `HARD_EXIT` 状态禁止加仓。

开仓时冻结：中位数、命中的入场边界、模型版本和退出目标。入场带宽与退出带为：

```text
entry_band_bps = abs(entry_boundary_bps - frozen_midline_bps)
exit_band_bps  = max(min_exit_band_bps,
                     entry_band_bps * exit_band_fraction)
```

完整往返预计收敛空间：

```text
sell_entropy = open_signed_residual - (midline + exit_band)
buy_entropy  = (midline - exit_band) - open_signed_residual
```

完整往返手续费明确按开仓和平仓各两腿计算：

```text
round_trip_fee_bps = 2 * (entropy_taker_fee_bps + hedge_taker_fee_bps)
```

资金费只作为附加观测字段写入事件，不从开仓收益门槛中扣除，也不阻止开仓。

每个边际盘口档位都必须满足：

```text
projected_net_bps
= convergence_bps
  - round_trip_fee_bps
  - observed_open_depth_slippage_bps
  - reserved_close_slippage_bps
>= min_expected_profit_bps
```

只要下一边际档不满足，计划数量就在前一档停止。最终数量继续受到共同 size step、双方最小数量、双方最小名义、`take_fraction`、单笔 `$500` 上限和双方各 `$1000` 持仓上限约束。

开仓和加仓继续使用 `execution.premium_persist_sec` 过滤瞬时信号。`residual_dynamic` 实盘要求该值大于零，示例配置使用 3 秒。成交后使用现有 `execution.cooldown_sec`，示例配置使用 60 秒。

## 7. 平仓生命周期

平仓始终优先于开仓或加仓。

### 7.1 正常退出

- `sell_entropy` 批次使用反向买入 Entropy 的可执行 signed residual；该值小于等于冻结的 `midline + exit_band` 时允许平仓。
- `buy_entropy` 批次使用反向卖出 Entropy 的可执行 signed residual；该值大于等于冻结的 `midline - exit_band` 时允许平仓。

正常退出遵守动态滑点预算和盘口新鲜度，但不要求满足新的开仓最低利润。

### 7.2 软退出

首次开仓满 60 分钟后进入 `SOFT_EXIT`：禁止加仓。只要按当前反向盘口估算的完整批次净收益不小于零，就允许提前减仓，不再等待冻结退出带完全命中。

### 7.3 硬退出

首次开仓满 360 分钟后进入 `HARD_EXIT`。硬退出不要求盈利，也不要求 reference 可用，但必须满足：

- 两边订单簿新鲜且 ready；
- 两腿使用相同 base 数量；
- 每腿保护价不超过 20 bps 硬上限；
- 不产生反向净仓位。

深度不足时，只提交硬上限内双方都可成交的共同数量。剩余仓位保持 `HARD_EXIT`，禁止新增风险并按状态变化和限频周期持续告警，不扩大滑点无限追价。

批次归零后转为 `FLAT`，再经过现有 `cooldown_sec` 才能开新批次，避免立即反向翻仓。

## 8. 动态滑点

默认配置为：

```yaml
slippage:
  bootstrap_bps: 5.0
  min_bps: 1.0
  safety_bps: 1.0
  hard_max_bps: 20.0
  max_edge_fraction: 0.25
  min_live_samples: 10
```

滑点样本按交易所实例和买卖方向分别维护，定义为实际成交均价相对决策时订单簿预计均价的不利偏差，盈利方向改善按零计入不利样本。

- 少于 10 笔真实成交时使用 5 bps bootstrap。
- 至少 10 笔后优先使用最近一小时样本的 p95；一小时不足 20 笔时向前扩展，但最多使用最近 50 笔。
- p95 加 1 bps 安全边际，再限制在 1 至 20 bps。
- 影子成交不进入这组样本。

每腿最终预算还受机会收益约束：

```text
edge_budget_per_leg
= (convergence_bps - round_trip_fee_bps
   - min_expected_profit_bps) * max_edge_fraction

leg_budget_bps
= min(statistical_budget_bps,
      edge_budget_per_leg,
      hard_max_bps)
```

默认 `max_edge_fraction=0.25`，为完整往返四条成交腿平均预留收益。若 `leg_budget_bps < min_bps`，候选被拒绝。计划的每条开仓腿深度滑点必须不超过该腿预算；预计平仓的两条腿各按同一预算保留。发单前在执行锁内重新计算，失效时放弃，不扩大保护价。

真实成交异常按最近 10 笔滚动判断：

- 3 笔超过各自决策预算：该交易所后续新增仓位单笔额度减半；
- 5 笔超过预算，或任一笔超过 20 bps：暂停该交易所新增仓位 15 分钟；
- 暂停不阻止减仓；到期后重新以当前统计预算评估。

正常和软退出使用动态预算。硬退出可以直接使用 20 bps，但不能超过硬上限。

## 9. 状态持久化与重启

实盘状态路径为配置的 `strategy.state_file`；影子模式自动在文件名后增加 `.shadow`，默认得到 `logs/campaign-state.shadow.json`。两种模式绝不共用状态。

状态写入使用同目录临时文件、flush、fsync 和原子替换。状态 schema 带版本号；未知版本、无效 JSON、非有限数值、交易对不匹配均为明确错误，不能猜测或静默重置。

实盘启动先读取状态，再通过现有严格仓位查询读取真实仓位：

1. 无活动状态且两边真实仓位在容差内为零：正常启动。
2. 有活动状态，且真实仓位方向、匹配数量与状态在共同 size step / 净敞口容差内一致：恢复批次并继续计算持仓时限。
3. 其他组合：进入恢复暂停并停止新增订单，输出明确差异，要求人工确认；不得从仓位倒推冻结模型。

状态只记录已确认成交。若进程在订单结果未知时退出，沿用现有订单确认和仓位对账恢复；恢复完成后再原子更新批次。状态写入失败进入现有 fail-closed 恢复路径，错误保留完整调用链。

影子模式假设计划数量按计划价格立即成交，以推进独立影子批次。该假设在所有事件中标记 `shadow`，不生成真实订单、不修改 venue position、不进入真实滑点样本。

## 10. 事件与分析

新增低频 `strategy-events.csv`，字段至少包括：

- UTC 时间、模式、事件类型、decision ID、campaign ID；
- pair 身份、方向、批次状态和原因；
- 模型版本、样本数、状态、中枢、上下分位数和 IQR；
- 实时 signed residual、冻结边界、退出目标和预计收敛空间；
- 完整往返手续费、各腿滑点预算、预计净收益；
- 数量、名义金额、reference age / skew；
- 实际或影子成交价格、批次持仓时间、已实现结果。

每分钟写一个模型快照；模型状态变化、开仓、加仓、平仓、强制退出和批次变化立即写。重复 `SKIP` 只在原因变化或下一分钟写一次，避免每秒刷相同行。

`tools/analyze.py` 增加可选策略事件输入，汇总：模型可用率、regime 状态、候选拒绝原因、开仓和完整闭环数量、方向、持仓时间分布、1 小时和 6 小时完成率、预计与实际/影子净收益、超时退出和未平批次。旧 `minutes.csv`、旧 `signals.csv` 和不带策略事件参数的调用保持兼容。

## 11. 配置兼容与安全开关

- 旧配置没有 `strategy` 时使用 `fixed_premium`，维持现有行为。
- `fixed_premium` 继续使用原有 `thresholds`。
- `residual_dynamic` 必须显式设置，并读取本设计新增字段。
- `residual_dynamic` 下 `live_enabled=false` 时，未带 `--record-only` 的启动请求直接报错，不发送订单。
- 只有同时配置 `mode=residual_dynamic`、`live_enabled=true` 且未带 `--record-only` 才进入动态策略实盘。
- `residual_dynamic` 实盘要求 `premium_persist_sec > 0`。
- `thresholds` 在 residual 模式中仅保留给旧模式和对照分析，不参与实际决策。
- 所有新增数值配置执行有限数、范围和交叉约束校验；软时限必须小于硬时限，最小滑点不能大于硬上限，分位数顺序必须有效，最低样本不能大于主窗口。

## 12. 错误处理

- 可预期的策略拒绝返回稳定原因码，不抛异常、不刷堆栈。
- 配置错误、状态文件损坏、状态写入失败和内部不变量破坏必须让调用方感知并保留上下文。
- reference 门禁失败只阻止新增风险；不会清空最后有效 reference，也不会中断行情连接。
- 模型加载遇到不匹配或无效历史行时跳过并计数；文件本身无法读取或表头不兼容时明确失败，不伪造就绪状态。
- 影子事件写入失败沿用 record-only fail-fast 原则，避免继续运行却丢失验证证据。

## 13. 测试与验证

### 13.1 模型

- 分钟去重、乱序、时间窗口、身份过滤、reference 门禁和未来时间过滤；
- 中位数、四分位数、10%/90% 分位数和确定性插值；
- 120 样本 ready、样本不足、三类 regime 异常和连续 15 分钟恢复；
- 启动历史预热与实时分钟更新一致。

### 13.2 决策与计划

- 两个方向的 residual 符号、分位开仓和冻结边界；
- 完整往返双倍手续费、四腿滑点预留和 2 bps 最低利润；
- 多档深度只纳入仍满足收益与滑点预算的边际数量；
- 同向加仓、软退出后禁止加仓、禁止反向翻仓；
- 正常退出、非负收益软退出、无 reference 硬退出和部分硬退出。

### 13.3 滑点

- bootstrap、最近一小时、最多 50 笔、p95、安全边际和 1 至 20 bps 限制；
- 收益份额上限与预算不足拒绝；
- 3/10 缩量、5/10 暂停、单笔超过硬上限暂停和暂停后恢复；
- 影子成交不污染真实样本。

### 13.4 状态与集成

- 原子状态往返、schema 版本、损坏文件、身份不匹配；
- 空仓启动、匹配批次恢复、真实仓位无状态和数量不一致 fail closed；
- 影子与实盘状态隔离；
- 影子模式完整开平仓周期零下单；
- 现有未知订单恢复、净敞口修复、单笔和仓位额度测试继续通过。

### 13.5 完成验证

- 运行完整 pytest；
- 对所有修改的 Python 文件运行编译检查；
- 使用已采集的 43.1 小时数据回放，确认不会再出现原始价差导致的约 98% 单向开仓；
- 检查影子闭环数、持仓时间、6 小时超时、滑点拒绝和未平批次；
- 检查 git diff，确认没有多交易所、资金费阻断、依赖升级或无关重构。

## 14. 验收标准

1. residual 模式只依据 reference-adjusted residual 和冻结批次规则新增风险。
2. reference 缺失、过期或更新时间差超限时，开仓和加仓均 fail closed。
3. 模型不足 120 个有效分钟或 regime 不稳定时不新增仓位。
4. 一个进程最多一个活动批次，未归零前不能反向开仓。
5. 1 小时进入软退出，6 小时进入硬退出，重启不重置计时。
6. 每条腿不超过固定单笔额度，双方持仓不超过各自固定上限。
7. 动态滑点不超过机会收益份额或 20 bps 硬上限。
8. 状态与真实仓位不一致时不发送新订单。
9. `--record-only` 使用相同决策逻辑、零下单并产生可分析的影子闭环事件。
10. 旧配置和 `fixed_premium` 行为兼容，现有测试保持通过。
11. 完整测试和历史回放通过后仍先影子运行，不自动认定可上实盘。
