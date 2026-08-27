# Live Readiness Hardening Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复当前双腿机器人已复现的实盘执行、关机、对账、行情序列、HIP-3 下单、配置和依赖可复现性问题。

**Architecture:** 保持现有适配器和引擎边界，只在产生错误状态的源头修正语义。订单结果继续由 `OrderResult` 表达；引擎集中判定一次双腿执行是否成功；未知状态只触发对账，不重发。

**Tech Stack:** Python 3、asyncio、aiohttp、pytest、PyYAML、Hyperliquid/Lighter SDK。

---

### Task 1: 双腿执行结果、关机排空与对账调度

**Files:** `tests/test_engine.py`, `entropy_arb/engine.py`

1. 写测试复现部分成交被计为成功、关机超时后取消在途任务、保护期导致对账请求丢失。
2. 单独运行这些测试，确认因现有行为而失败。
3. 实现严格的双腿成功条件、不可取消的执行排空循环、保护期后的自动对账重调度。
4. 单独运行 `tests/test_engine.py`，再运行完整测试。
5. 提交：`修复：加固执行完成与关机对账流程`。

### Task 2: Lighter nonce 连续性

**Files:** `tests/test_feeds.py`, `entropy_arb/feeds.py`

1. 写测试证明 `begin_nonce` 不等于上一条 `nonce` 时必须重新订阅，完全相等时才能接收。
2. 运行测试确认失败。
3. 把连续性判断改为严格相等。
4. 运行 feed 测试和完整测试。
5. 提交：`修复：严格校验 Lighter 订单流序号`。

### Task 3: HIP-3 下单兼容

**Files:** `tests/test_venue_contract.py`, `entropy_arb/venue_hl.py`

1. 写测试断言 HIP-3 请求不包含 `cloid`，未知响应不会按客户端 ID 查询或重发。
2. 运行测试确认失败。
3. 删除 HIP-3 cloid 生成和轮询路径，保留未知结果供引擎对账。
4. 运行适配器测试和完整测试。
5. 提交：`修复：兼容 Hyperliquid HIP-3 下单`。

### Task 4: 配置边界和实盘依赖

**Files:** `tests/test_config.py`, `tests/test_requirements.py`, `entropy_arb/config.py`, `requirements-live.txt`

1. 参数化写入非法金额、频率、比例和超时测试，并写依赖必须精确固定的文本测试。
2. 运行测试确认失败。
3. 在配置加载完成前统一拒绝非法边界；把实盘 SDK 固定到确定版本/提交。
4. 运行配置、依赖和完整测试。
5. 提交：`修复：校验实盘配置并固定依赖版本`。

### Task 5: 文档和最终验证

**Files:** `README.md`, `README.zh-CN.md`

1. 更新 HIP-3 未发送 cloid、未知结果对账、关机等待在途执行和上线检查说明。
2. 运行 `python -m pytest -q -W error`。
3. 运行 `python -m compileall -q main.py entropy_arb tools tests`。
4. 运行 `git diff --check`、敏感文件检查和最终差异审查。
5. 提交：`文档：补充实盘加固行为说明`。

