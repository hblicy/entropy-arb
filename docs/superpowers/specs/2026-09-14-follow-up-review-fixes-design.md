# 动态残差策略复审修复设计

## 目标

修复复审确认的模型恢复、跨市场状态隔离、输出路径校验、历史回放时间轴、启动恢复审计和策略事件 CSV 尾行问题，使动态残差策略在单进程及多市场并行场景下保持可恢复、可追溯，并且不再把覆盖不完整的历史回放描述为完整结果。

## 范围

本轮只处理以下已确认问题：

1. 同一分钟的替换样本错误推进 regime 恢复计数；
2. warm start 接受未结束分钟和重复分钟；
3. 不同市场默认共用 campaign、pending 和 strategy event 文件；
4. 输出路径预检没有使用最终的 shadow、pending 和市场隔离路径；
5. raw signal 对回放时间轴产生选择性截断；
6. pending execution 启动恢复后缺少可幂等的 campaign 审计事件；
7. 策略事件 CSV 崩溃残留半行后直接追加。

不改变交易信号公式、手续费公式、下单方向、仓位上限、reduce-only 行为和交易所适配器接口。

## 1. ResidualModel 分钟语义

`ResidualModel.observe()` 将区分三类输入：

- `minute > latest_minute`：这是新的时间推进，可以更新 regime 状态和连续恢复分钟数；
- `minute == latest_minute`：只替换该分钟样本并增加模型版本，不推进连续恢复分钟数；
- `minute < latest_minute`：只更新历史窗口值，不重放或改变当前 regime 状态。

`warm_start_residual_model()` 先按分钟合并合法记录，文件中最后一条记录获胜，然后按分钟递增顺序送入模型。它只接受 `minute <= now_minute - 1` 的已结束分钟；当前分钟和未来分钟计入 `rejected_time`。返回的 `accepted` 表示最终载入的唯一分钟数，而不是原始合法行数。

回归测试必须证明：同一分钟连续替换 15 次不能解除 `REGIME_UNSTABLE`，15 个新的有效分钟可以解除；重复历史行只计一个样本；当前分钟不会进入 warm start。

## 2. 市场隔离运行时路径

新增单一职责的运行时路径模块。它根据 `MarketIdentity` 的四个字段生成市场标签：

```text
<可读且文件名安全的字段片段>-<规范身份 SHA-256 前 10 位>
```

可读部分对每个字段限制长度，只保留字母、数字、点、下划线和短横线；其他字符替换为短横线。哈希使用未清洗的完整身份，因此不同身份不会因可读部分清洗而碰撞。

配置路径是基础路径，不再是最终写入路径。最终路径规则为：

- live campaign：在基础文件扩展名前加入市场标签；
- live pending：从市场隔离后的 campaign 路径派生 `.pending`；
- shadow campaign：从市场隔离后的 campaign 路径派生 `.shadow`；
- live strategy event：在基础文件扩展名前加入市场标签；
- shadow strategy event：从市场隔离后的 event 路径派生 `.shadow`。

例如基础路径 `logs/campaign-state.json` 可派生为：

```text
logs/campaign-state.io-ANTH--lighter-rh-ANTHROPIC-a1b2c3d4e5.json
logs/campaign-state.io-ANTH--lighter-rh-ANTHROPIC-a1b2c3d4e5.pending.json
logs/campaign-state.io-ANTH--lighter-rh-ANTHROPIC-a1b2c3d4e5.shadow.json
```

路径预检必须使用最终实际写入的路径，并继续执行空路径、同文件、父子路径、hardlink 和可写性检查。record-only 检查 minute CSV、signal CSV、shadow campaign 和 shadow event；live 检查 trades CSV、可选 minute CSV、live campaign、pending 和 live event。基础路径本身不加入冲突检查，因为它不再被写入。

### 旧状态迁移门禁

引擎初始化动态策略时检查旧版未隔离 campaign 路径及其 `.shadow` 或 `.pending` 派生路径。如果旧路径存在且对应的新路径不同，则拒绝启动，错误信息同时给出旧路径和新路径，并要求操作员核对持仓后手工移动。程序不得自动移动、删除、覆盖或忽略旧状态文件。

旧的 strategy event CSV 不参与恢复，不阻止启动；新的运行写入市场隔离后的文件。文档必须说明升级前有未平仓 campaign 时需要先完成状态迁移。

## 3. 连续回放快照

不新增第二个大型 CSV，也不改变 `SIGNAL_HEADER`。`SignalRecorder` 在原有 `start/sample/end` 生命周期记录之外，每 `sample_sec` 写入一条 `event=snapshot` 的中性市场快照：

- `direction` 为空；
- `event_id` 在一次运行中唯一；
- 记录四个最优价、盘口年龄、参考价、参考年龄和更新时间差；
- `crossable_notional_usd` 使用四个最优档可用名义金额的最小值，作为双方向共用的保守 top-of-book 容量；
- snapshot 不计入 raw buy/sell coverage，也不改变原有 signal lifecycle 状态。

recorder 的等待超时同时考虑下一次 snapshot 和活跃 lifecycle sample，因此即使 fixed-premium 信号未触发，也会持续生成回放时间轴。每日 gzip 轮转行为保持不变。

回放器兼容旧文件：

- 有 snapshot 的区间按连续 top-of-book 快照回放；
- 完全没有 snapshot 的旧文件标记为 `threshold-censored legacy top-of-book approximation`；
- `now_ts` 只定义允许读取的最大时间，实际回放截止时间是最后一条可用记录；
- 当 `now_ts` 晚于最后一条 snapshot 时，输出明确的覆盖截止时间和 incomplete 警告，不使用最后盘口推测缺失时段；
- campaign、hold、soft/hard close 指标均标注为截至实际覆盖终点。

这不会把 top-of-book 近似升级成实际成交回测；README 中仍保留“不报告实际 PnL”的限制说明，并补充旧数据的阈值截断限制。

## 4. Pending execution 恢复事件幂等

不修改 pending state 的持久化 schema。动态实盘执行为每次 execution 使用确定性的审计 ID，例如 `execution-<execution_id>`，正常完成路径和启动恢复路径使用同一 ID。

`StrategyEventRecorder` 启动时读取现有完整记录的 `decision_id`，并拒绝在同一文件中再次写入相同非空 ID。这样可覆盖以下崩溃窗口：

1. campaign 已保存、事件未写：重启后补写事件；
2. 事件已写、`campaign_applied` 未保存：重启后恢复 campaign，重复事件被抑制；
3. `campaign_applied` 已保存、pending 尚未清除：重启校验 durable campaign 后，重复事件被抑制。

启动恢复根据 pending 的 intent、冻结模型、双腿终态成交和 campaign_before 重建审计事件。OPEN/ADD 写 `campaign_changed`，完全平仓写 `campaign_closed`，并沿用正常路径的成交价、数量、持仓时长和可计算 realized PnL 字段。

## 5. 策略事件 CSV 尾行保护

`StrategyEventRecorder` 打开非空文件时，同时验证表头和最后一条 CSV 记录：

- 字段数必须等于 `STRATEGY_EVENT_HEADER`；
- `ts_ms` 必须是非负有限数；
- `event` 和 `decision_id` 必须满足记录器可写出的基本约束。

如果表头或尾行无效，使用现有归档命名规则把整个原文件移动到 `.old`/递增归档路径，然后新建带正确表头的文件。不得截断或尝试拼接半行。归档失败必须向调用方抛错，不能继续写入。

## 6. 错误处理与日志

- 旧状态迁移、路径冲突和损坏状态属于启动错误，必须包含具体路径并阻止对应模式启动；
- 回放覆盖不足不是交易运行错误，工具正常退出但在结果中明确标记 incomplete；
- CSV 归档或新文件创建失败必须保留异常上下文并使启动失败；
- 不增加宽泛异常吞噬或静默兼容路径。

## 7. 测试与验收

每个问题按 TDD 执行：先增加最小回归测试并确认其因当前缺陷失败，再做最小实现使其通过。至少覆盖：

- 同分钟替换不推进 regime recovery；
- warm start 去重及排除当前分钟；
- 不同 MarketIdentity 生成不同路径，shadow/live 和 pending 路径互异；
- 最终派生路径与现有输出碰撞时配置拒绝；
- 旧 campaign/pending/shadow 文件触发迁移门禁；
- 无 fixed-premium 信号时仍定时记录 snapshot；
- 新 snapshot 回放与旧 censored 回放产生不同覆盖标识；
- `now_ts` 晚于最后快照时报告实际截止时间；
- 正常执行和启动恢复只记录一次确定性 campaign 事件；
- 残缺 strategy event 尾行被完整归档。

最终验收命令包括完整 `pytest`、`compileall`、`git diff --check`，以及对最终 diff 的独立只读代码审查。实盘启用仍需用户在修复合并、服务器更新、旧状态迁移和新一轮 record-only 验证完成后单独决定。
