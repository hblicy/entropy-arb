# Record-only 信号可观测性设计

**状态：** 已确认

**日期：** 2026-09-01

## 目标

在不改变实盘触发、下单和风险规则的前提下，为 `--record-only` 增加信号级 CSV，确认美股开盘附近的短暂价差究竟是可持续机会，还是两边盘口更新时间不同步造成的假信号。

## 范围

- 保留现有 `logs/minutes.csv` 分钟聚合文件。
- 新增 `logs/signals.csv`，仅在 `--record-only` 下启用。
- 信号首次超过当前配置的净阈值和两边吃单费时立即记录 `start`。
- 信号持续期间每秒记录一次 `sample`。
- 信号消失、盘口失效或程序关闭时记录 `end`。
- 不增加盘口同步门槛，不改变 `_scan()`，不发送订单。

## 方案

在现有 `entropy_arb/recorder.py` 中增加独立的 `SignalRecorder`。它直接读取两边 `OrderBook` 和已经完成市场解析的 venue 元数据，使用与实盘相同的 `plan_arb()` 计算阈值、可成交深度和计划金额，但不调用任何 venue 下单接口。

`Engine` 在 `record_only=True` 时启动该记录器。行情 feed 继续通过现有更新事件唤醒记录器，使信号开始和结束尽快被观察；信号激活后，记录器即使没有新盘口事件也会每秒写一次样本。实盘模式不启动该任务，避免新增下单路径开销和行为变化。

## 信号生命周期

两个方向独立维护状态：

- `sell_entropy`：买对冲腿、卖 Entropy；净门槛为 `midline_bps + upper_bps`。
- `buy_entropy`：买 Entropy、卖对冲腿；净门槛为 `lower_bps - midline_bps`。

顶层可成交 edge 超过净门槛加两边吃单费时，信号进入激活状态。事件编号由方向、开始毫秒时间、运行级 UUID 和方向内序号组成，保证单次运行及跨运行追加文件中的生命周期不会互相合并。

- 未激活到激活：写 `start`，持续时间为 0。
- 继续激活且距上次落盘至少 1 秒：写 `sample`。
- 激活到未激活：写 `end`，带总持续时间和结束原因。
- 关闭时仍激活：写 `end`，结束原因为 `shutdown`。

若顶层 edge 已超过门槛，但深度、最小数量或最小名义金额不允许形成订单计划，信号仍被记录，并在 `plan_status` 中保存 `plan_arb()` 的明确原因。

## CSV 字段

`logs/signals.csv` 每行包含：

- `ts_ms`、`time_utc`、`symbol`、`entropy_dex`、`hedge_venue`
- `event_id`、`event`、`direction`、`elapsed_ms`、`end_reason`
- `entropy_bid`、`entropy_ask`、`hedge_bid`、`hedge_ask`
- `entropy_book_age_ms`、`hedge_book_age_ms`、`book_update_skew_ms`
- `top_edge_bps`、`net_threshold_bps`、`total_fee_bps`
- `plan_status`、`qty`、`buy_limit`、`sell_limit`
- `planned_notional_usd`、`crossable_notional_usd`
- `buy_depth_slippage_bps`、`sell_depth_slippage_bps`
- `leg_slippage_limit_bps`、`expected_edge_usd`

信号生命周期是否过期与实盘一致，使用各自 `alive_ts`；输出的盘口年龄和更新时间差仍使用 `last_update_ts`，用于观察真实价格更新时间和两边错位程度。深度滑点分别衡量计划最差买价相对买一、计划最差卖价相对卖一的偏离。

## 配置

在 `recorder` 下增加：

```yaml
recorder:
  enabled: true
  csv: logs/minutes.csv
  signal_csv: logs/signals.csv
```

`signal_csv` 有默认值，现有配置无需修改即可继续运行。配置仍执行严格未知键校验。
仅在 `--record-only` 下，分钟 CSV、信号 CSV 与日志文件必须解析为三个不同文件；
校验会识别规范化路径、符号链接和已存在的硬链接。实盘不会因未使用的
`signal_csv` 与其他路径相同而拒绝启动。

## 错误处理

- 旧信号文件表头与新 schema 不一致时旋转为未占用的 `.old`、`.old.1` 等归档，避免不同 schema 混写或覆盖已有归档。
- 文件创建、写入或 flush 失败时记录完整异常、设置停止事件并让任务失败；不得继续运行并假装数据已落盘。
- 预期的无盘口、盘口失效和计划不足通过 `end_reason` 或 `plan_status` 表达，不作为异常吞掉。

## 测试

- 信号首次超过门槛写 `start`。
- 激活未满 1 秒不重复写，满 1 秒写 `sample`。
- 信号消失和关闭分别写正确的 `end`。
- 两个方向的状态和事件编号互不干扰。
- 盘口年龄、更新时间差、深度滑点、可成交金额和预期收益计算正确。
- 顶层 edge 合格但计划金额不足时仍写信号及明确 `plan_status`。
- CSV 追加只保留一个表头，旧 schema 会旋转。
- `Engine` 仅在 `--record-only` 启动信号记录器，实盘任务集合保持不变。
- 完整测试在警告视为错误的模式下通过，所有 Python 文件可编译。

## 非目标

- 本次不根据盘口年龄或更新时间差阻止交易。
- 本次不加入美股交易时段过滤。
- 本次不改变静态中枢、阈值、固定额度或滑点配置。
- 本次不自动判断某个信号可否实盘；结论由新增数据分析得出。
