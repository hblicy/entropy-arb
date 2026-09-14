# 动态策略最终审计加固设计

## 目标

修复最终审计确认的 pending execution 审计不确定、混合 signals 覆盖范围不透明、损坏 pending 腿标识不严格、无扩展名运行路径派生异常，以及策略事件文件启动检查内存放大的问题。完成后，同一次实盘 execution 无论正常完成还是重启恢复，都产生内容一致、时间语义明确且可幂等去重的策略事件。

## 范围与约束

- pending execution 持久化 schema 从 v2 升级到 v3；campaign schema、signal CSV schema 和 strategy event CSV schema 不变。
- 不改变交易信号、手续费计算、下单方向、仓位上限、reduce-only 或交易所适配器接口。
- 已存在的 v2 pending 文件不自动迁移。启动时保留原文件并 fail-closed，错误明确要求核对两边订单和真实仓位。
- 没有 pending 文件或仅有 campaign 文件的升级不受影响。
- 回放继续只做 top-of-book 近似，不生成实际成交 PnL。

## 1. Pending schema v3

### 1.1 不可变审计上下文

新增严格校验的 `PendingAuditContext`，作为 `PendingExecutionState` 的字段持久化。它保存发单前已经确定、重启后不能从当前行情重建的字段：

- `reason`；
- `signed_residual_bps`、`reference_basis_bps`；
- `convergence_bps`、`round_trip_fee_bps`；
- 买卖腿滑点预算；
- `planned_notional_usd`、`projected_net_bps`、`projected_net_usd`；
- `estimated_campaign_pnl_usd`；
- 决策时两腿参考数据龄、更新时间差和方向化净资金费。

模型快照、entry/exit 边界、数量、预期成交价、手续费率和 campaign_before 已存在于 pending，继续由原字段提供。审计上下文中的可选数值只能是有限数或 null；字符串字段必须符合策略可产生的基本约束。

正常和恢复路径都不再从当前盘口或当前 reference 对象补写决策时字段，而是共同从 pending 与审计上下文构造最终事件。

### 1.2 终态时间

`PendingExecutionState` 增加可空 `settled_at`：

- 发单前首次写入 pending 时为 null；
- 两腿都取得终态结果后，以本进程观察到“双腿终态齐备”的 wall-clock 时间写入；
- 必须先持久化终态双腿和 `settled_at`，之后才允许更新 campaign 或写策略事件；
- 已经有 `settled_at` 时不得在重试中覆盖，保证恢复事件稳定；
- `campaign_changed/campaign_closed/execution_settled` 的 `ts` 使用 `settled_at`；
- CLOSE/FORCED_CLOSE 的 `hold_seconds` 使用 `settled_at - campaign_before.opened_at`。

交易所未提供可信成交时间，因此该字段定义为“引擎首次确认双腿终态的时间”，不冒充交易所成交时间。若进程在获得终态后、持久化前崩溃，重启首次重新确认时确定该值；一旦落盘，后续恢复保持一致。

### 1.3 版本与失败策略

`SCHEMA_VERSION` 升为 3，loader 只接受 v3。检测到 v2 或其他版本时抛 `PendingExecutionStateError`，消息包含文件路径、实际版本和人工核对提示；文件不移动、不删除、不覆盖。

双腿必须满足：

- venue key 集合严格等于 `{"entropy", "hedge"}`；
- 一买一卖且 venue 不重复；
- 双腿成交量不超过请求数量加允许的数值容差；
- OPEN 的腿方向必须与 `direction` 一致；ADD 与 campaign 方向一致；CLOSE/FORCED_CLOSE 与 campaign 方向相反。

任何不满足条件的持久状态在 campaign 变更前 fail-closed。

## 2. 统一策略事件构造

新增内部事件构造器，以 pending 为唯一持久事实来源，输入仅包括已经验证的 pending 和 campaign_after。它负责：

- 确定 `execution-<execution_id>`；
- 选择 `campaign_changed`、`campaign_closed` 或 `execution_settled`；
- 从双腿终态计算数量和实际成交价；
- 从 campaign_before、手续费和成交价计算 realized PnL；
- 使用 frozen model、边界和 `PendingAuditContext` 填充分析字段；
- 使用 `settled_at` 填充事件时间和持仓时长。

正常执行、进程内 unknown-order 恢复、启动恢复全部调用该构造器。StrategyEventRecorder 继续按确定性 decision_id 抑制同一活动事件文件内的重复记录。

## 3. 回放覆盖范围

`ReplayResult` 增加：

- `coverage_start_ts`：实际用于连续回放的第一条 snapshot 时间；没有 snapshot 时为 null；
- `censored_prefix`：输入中是否存在早于 `coverage_start_ts` 的旧 lifecycle 行。

规则如下：

- 完全没有 snapshot：保持 `threshold-censored legacy`，`timeline_complete=false`；
- 第一条 snapshot 之前有 lifecycle：只从第一条 snapshot 开始计算策略指标，approximation 标记为 mixed/censored-prefix，`timeline_complete=false`；
- 从输入第一条有效记录开始就是 snapshot，且 snapshot/lifecycle cadence 无超限间隔并覆盖到 requested end：才允许 `timeline_complete=true`；
- CLI 总是输出 coverage start、coverage end 和前缀截断状态。

回放不会使用第一条 snapshot 之前的选择性 lifecycle 行计算 campaign 指标，也不会用最后盘口外推缺失时间。

## 4. 无扩展名运行路径

marker 派生必须依据原始配置路径的 suffix，而不能再次解释已经追加市场标签的文件名：

- `state` 派生为 `state.<market-tag>`、`state.<market-tag>.pending.json` 和 `state.<market-tag>.shadow`；
- `events` 派生为 `events.<market-tag>` 和 `events.<market-tag>.shadow`；
- 有扩展名路径继续保持 `state.<market-tag>.pending.json`、`events.<market-tag>.shadow.csv`。

路径仍由完整 MarketIdentity 哈希隔离，旧状态门禁和最终输出碰撞检查使用修正后的实际路径。

## 5. 策略事件文件启动检查

`StrategyEventRecorder` 改为流式读取：

- 先以二进制检查文件以换行结束；
- 再用严格 CSV reader 逐行校验，不建立完整 rows 列表；
- 内存仅保存非空 decision_id 集合；
- 损坏时仍用 `os.replace` 整文件归档，不截断、不拼接；
- 通过 logger warning 输出损坏文件和归档目标；归档失败继续向调用方抛错。

归档后的新活动文件是新的权威审计流。旧归档可能含合法前缀，离线合并多个归档时应按 decision_id 去重；运行时不会因为损坏归档中的旧 ID 而抑制新活动文件的恢复事件。

## 6. 测试与验收

按 TDD 分别覆盖：

1. 正常终态、进程内延迟终态和启动恢复得到相同 audit payload 与稳定 settled_at；
2. CLOSE 恢复使用 settled_at 计算 hold 和 realized PnL；
3. v2 pending、未知 venue、方向错误和超量成交全部 fail-closed，原文件不变；
4. 混合 lifecycle/snapshot 输出 coverage start、censored prefix 且不得标记完整；
5. 纯 snapshot 连续区间仍可标记完整；
6. 无扩展名 state/event 的 marker 顺序正确；
7. 大型 strategy event CSV 检查不调用 `read_bytes()` 或 `list(reader)`，损坏归档有 warning；
8. 完整 pytest、compileall、git diff --check 和最终只读复审通过。

## 7. 部署影响

合并并更新服务器后，先继续 `--record-only` 验证新 snapshot 和 market-scoped shadow 文件。实盘启动前检查是否存在 market-scoped `.pending.json`：如果它是 schema v2，不得直接删除或改版本，必须核对两边订单历史和真实仓位后人工处理。只有无未决 journal、campaign 与真实仓位一致、完整测试通过时，才进入单独的实盘启用决策。
