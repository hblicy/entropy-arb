# Final Live Safety Fixes Design

## Goal

修复最终全分支审查确认的六个缺陷：非 READY 模型阻断减仓、深度收益近似、固定策略双腿仓位上限、残差修复价格取整、Hyperliquid signer 跨进程 nonce 冲突，以及 campaign 异常整数错误类型。

## Chosen design

### 1. Pending close snapshots

保持 pending schema v3 和字段不变。`OPEN`/`ADD` 继续要求 `frozen_model` 为 READY；`CLOSE`/`FORCED_CLOSE` 接受合法的 `MODEL_NOT_READY` 或 `REGIME_UNSTABLE` 快照。非 READY 且无样本时允许五个分位字段全部为 null；有样本时仍要求全部为有限数且有序。这样保留决策时模型状态，不用错误地用 campaign 的旧 READY 模型冒充当前状态。

备选方案是平仓时持久化 campaign 的 frozen model，改动更小但会丢失真实决策上下文；另一方案是升级 schema v4 拆分两个模型，迁移成本超出本次修复范围。

### 2. Exact marginal convergence

深度 walker 在每个 ask/bid 边际组合上重新计算 direction-specific residual 与 convergence。独立滑点预算仍作为每腿价格边界；收益门槛直接使用边际 convergence 减往返手续费和预留平仓滑点，不再用两个 bps 的线性和近似比值变化。最终取整后按最终限价再次计算返回值。

### 3. Per-leg position caps

固定策略把两腿剩余额度都换算成 base quantity：买腿允许数量为 `cap / buy_px - position`，卖腿允许数量为 `cap / sell_px + position`。取较小数量后再按买腿价格换回 planner 使用的买入名义上限。

### 4. Residual hedge rounding

SELL 的最低保护价向上取整，BUY 的最高保护价向下取整。取整后若订单不满足最小名义则不发送，保持 recovery，不放宽滑点边界。

### 5. Signer-scoped process locks

每个 venue 暴露实际签名主体的 lock id；Hyperliquid 必须使用 `wallet.address`，不能使用 query/subaccount address。Engine 对去重、排序后的每个 account id 获取独立文件锁；部分获取失败时逆序释放已取得的锁，正常清理也逆序释放。市场级 campaign/pending/event 路径继续保持隔离。

组合锁只按账户对生成一个摘要不能解决“两个进程只共享一个 signer”的重叠，因此不采用。跨主机文件锁无法互斥，运行手册继续要求不同主机使用不同 API wallet。

### 6. Campaign error normalization

数值转换捕获 `OverflowError` 并转换为 campaign 自己的可识别异常；loader 再附加 `invalid campaign state` 上下文。正常有限数值行为不变。

## Verification

每项先增加可复现失败测试，再做最小实现并运行对应测试。最后运行全量 pytest、compileall、`git diff --check` 和工作树检查。当前环境另有一个与本次范围无关的 uptime 相关测试脆弱性，将单独报告，不混入本次生产修复。
