# entropy-arb

**[English documentation / 英文文档 → README.md](README.md)**

开源双交易所永续合约套利机器人。其中一条腿永远是 **Entropy**（Hyperliquid 上的
`io` builder dex）；另一条腿（对冲腿）三选一：

| `--hedge` | 交易所 | 计价货币 | 吃单费 | 协议 |
|---|---|---|---|---|
| `lighter` | Lighter 主网 | USDC | 0 bps | zkLighter ws（增量订单簿，异步结算） |
| `lighter-rh` | Lighter Robinhood 链 | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book，IOC 同步结算 |

> **推荐链接** —— 通过以下链接注册即可支持本项目：
> - Entropy — Tier 4 推荐，100% 返佣：<https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood 链：<https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz（Hyperliquid）：<https://app.hyperliquid.xyz/join/QUANTGUY>

当同一品种在一边贵、另一边便宜时，机器人同时在贵的一边卖出、便宜的一边买入
（均为吃单），持有 delta 中性仓位，等溢价回归后反向平仓。所有交易决策使用的
价格都来自**将要实际成交的那个交易所的真实订单簿**——Hyperliquid 的盘口来自
官方 websocket（`wss://api.hyperliquid.xyz/ws`），Lighter 的盘口来自 Lighter
官方 websocket。

机器人运行期间（即使没有密钥、没有开策略）会自动把两边盘口记录成**分钟级
CSV 数据**，配套的分析工具可以直接把这些数据变成策略所需的三个核心参数。

## 信号逻辑

整个信号就是 `config.yaml` 里三个数字，由你根据采集的数据自己设定：

```
premium_bps =（Entropy 价格 / 对冲腿价格 − 1）× 10 000

                          ┌──────────────  卖出 Entropy + 买入对冲腿
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   溢价的长期中枢
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  买入 Entropy + 卖出对冲腿
```

- `midline_bps` —— 溢价的常态水平。跨所溢价几乎从不以零为中心（预言机不同、
  计价货币不同、新上市溢价等），零中心的带只会朝一个方向开仓、打满仓位上限、
  永远无法平仓。请实际测量溢价所在的位置，然后填入。
- `upper_bps` / `lower_bps` —— 中枢上下两侧的入场带宽。

两个方向的门槛都作用于**可实际成交的价格**（Entropy 买一 对 对冲腿卖一，
反之亦然），并且是**扣除双边吃单手续费之后的净门槛**——引擎会在阈值之上
另行叠加手续费。因此一次完整往返扣费后**净赚 ≥ upper + lower bps**，这是
结构上保证的。

有一点必须理解：当 `midline_bps: 5` 时，买入 Entropy 的门槛是
`lower − midline`，可能为**负数**。这是有意为之——如果 Entropy 长期贵 5 bps，
那么在溢价为 0 时买入它，相对其自身均衡水平就是便宜了 5 bps，这笔交易正是
此前在 `midline + upper` 处卖出的获利平仓。这同时意味着**中枢填错就是亏钱
策略**：若真实溢价中枢是 0 而你填了 5，机器人会整天以公允价买入 Entropy。
先测量、再交易——数据采集器和分析工具就是为此而生。

## 快速开始

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 数据采集只需要这些

cp config.example.yaml config.yaml       # 策略配置（阈值、规模、风控）
cp .env.example .env                     # 密钥——交易必填
```

交易哪个市场**不在**配置文件中——每次启动时用命令行参数显式指定：
`--symbol` 选择 Entropy 品种，`--hedge` 选择对冲交易所（三选一：
`lighter`、`lighter-rh`、`tradexyz`）。如果同一标的在对冲交易所使用
不同名称，再传 `--hedge-symbol`；不传时默认与 `--symbol` 相同。

`--record-only` 永远不会发单。使用 `strategy.mode: residual_dynamic` 时，
它还会按计划价格推进一套隔离的影子批次；这些假设成交只用于验证，不是实际
成交或真实盈亏。旧 `fixed_premium` 模式仍然只采集数据。

**第一步：先采集数据**（不需要任何密钥）：

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

例如，Entropy 的 `ANTH` 与 Robinhood Lighter 的 `ANTHROPIC` 使用：

```bash
python3 main.py --record-only --symbol ANTH --hedge lighter-rh \
  --hedge-symbol ANTHROPIC --no-dashboard
```

在 VPS 或 `screen` 中建议显式保存控制台日志：

```bash
python -u main.py \
  --record-only \
  --symbol ANTH \
  --hedge lighter-rh \
  --hedge-symbol ANTHROPIC \
  --no-dashboard \
  2>&1 | tee -a logs/engine.log
```

至少运行几个小时（最好一整天——溢价存在日内规律）。分钟聚合写入
`logs/minutes.csv`；仅在 `--record-only` 下，连续最优盘口观测写入
`logs/signals.csv`。没有固定价差信号时约每秒写一条中性的 `snapshot`；信号
活跃时改为写 `start`、每秒 `sample`，并在信号消失、盘口过期或程序关闭时写
`end`。这些数据只用于观察，不会阻止开仓或改变实盘策略；可用
`recorder.signal_csv` 修改明细路径。两个文件的每行都包含两条腿各自的原生
symbol、Entropy DEX 和对冲交易所。

参考价格和资金费复用现有行情 WebSocket 采集，启动时通过 REST 初始化，参考
WebSocket 过期后再用 REST 定时恢复。Hyperliquid 与 Lighter 的资金费统一为
`bps/hour`。`fixed_premium` 模式下，参考异常和残差告警仍只记录、只告警；
`residual_dynamic` 模式增加风险时必须具备新鲜且更新时间差合格的参考数据，
硬退出则不依赖参考数据。
信号行会追加两腿的参考价格、资金费、数据龄，以及按方向计算的有符号可成交
溢价、残差、残差 edge 和净资金费；参考值缺失时留空，但不会丢弃原信号。

每个“交易标的 + 交易所组合”应使用独立的 `recorder.csv`。分析器兼容使用旧
`symbol` 身份字段或完全不含市场字段的历史文件，但检测到一个文件中混有多个
已标识市场时会直接拒绝分析，不会给出存在风险的合并阈值。旧 schema 或末行
不完整、无效时，原文件会保留到下一个未占用的 `.old`、`.old.1` 等归档，再写入
干净的新文件。`--record-only` 启动时会
立即打开两个采集文件；创建或写入失败会报错并停止进程。如果分钟行已经交给
CSV writer 后 `flush()` 才报告结果不确定的 I/O 错误，采集器不会盲目重写同一
分钟聚合；这能避免重复行，但无法在 flush 失败时保证该行一定落盘。
开启 `recorder.signal_rotate_daily: true` 后，跨入新的 UTC 日期并写入第一行时，
`signals.csv` 会轮转，例如 9 月 10 日归档为 `signals-20260910.csv.gz`；重名时
依次使用 `.gz.1`、`.gz.2`。程序会完整校验 gzip 后才删除原始归档；压缩失败
则保留带日期的原始 CSV，并继续写新的 `signals.csv`。

**第二步：分析数据、设定阈值：**

```bash
python3 tools/analyze.py --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
python3 tools/analyze.py --csv logs/minutes-20260910.csv.gz \
  --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
python3 tools/analyze.py --csv logs/minutes.csv \
  --strategy-csv logs/strategy-events.io--ANTH--lighter-rh--ANTHROPIC-ae4e3987a8.shadow.csv \
  --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
```

它只分析 `logs/minutes.csv`，输出溢价分布、各档带宽的历史触发频率，
以及可直接粘贴进 `config.yaml` 的 `thresholds:` 配置块。同一市场、同一分钟的
重启片段会先合并再做样本数过滤，因此每分钟只计一次；它不会分析
`logs/signals.csv`，也不会把同一分钟文件中的多个已标识市场混合计算。
新参考列存在有效值时，分析器还会输出分钟 close 的参考基差、有符号残差和
每小时资金费差分布。传入 `--strategy-csv` 后，还会汇总已完成/未平批次、
持仓时限、强制退出、模型可用率、拒绝原因及影子/实盘结果；普通 `.csv` 与
`.csv.gz` 使用完全相同的分析逻辑。

考虑实盘前，先回放轮转后的原始信号文件。它是只读的 **top-of-book
approximation**（最优盘口近似），不会把假设成交冒充为实际盈亏。当前版本
生成的文件包含连续 `snapshot`；只有门槛触发生命周期的旧文件仍可读取，但结果
会明确标记为 `threshold-censored legacy`，不能当成完整时间轴。回放还会输出
请求截止时间和实际数据覆盖截止时间；覆盖不完整时会告警，不会用最后一笔盘口
外推到请求截止时间：

```bash
python3 tools/replay_strategy.py \
  --minutes logs/minutes.csv \
  --signals logs/signals-20260912.csv.gz \
            logs/signals-20260913.csv.gz \
            logs/signals.csv \
  --config config.yaml
```

### 动态残差从影子到实盘的闸门

`strategy.mode: residual_dynamic` 启用滚动有符号残差模型；默认的
`strategy.live_enabled: false` 是独立的第二道实盘开关。只有以下三个条件同时
满足，动态策略才可能发送真实订单：

1. `strategy.mode` 为 `residual_dynamic`；
2. `strategy.live_enabled` 为 `true`；
3. 启动命令中没有 `--record-only`。

`strategy.state_file` 和 `strategy.event_csv` 是基础路径。引擎会先追加确定性的
市场标签，再追加模式标记，因此不同 symbol、交易所及实盘/影子不会共用策略
状态。以 ANTH 为例，实际实盘文件是
`logs/campaign-state.io--ANTH--lighter-rh--ANTHROPIC-ae4e3987a8.json`、对应的
`.pending.json` 日志，以及
`logs/strategy-events.io--ANTH--lighter-rh--ANTHROPIC-ae4e3987a8.csv`；
`--record-only` 使用对应的 `.shadow.json` 和 `.shadow.csv`。

升级后，如果检测到 `logs/campaign-state.json`、
`logs/campaign-state.pending.json` 或 `logs/campaign-state.shadow.json` 等旧版未隔离
状态，引擎会拒绝启动。必须先核对两边交易所真实仓位，确认该文件所属的市场和
模式，备份后再人工移动到启动错误提示的新路径；不要盲目改名，也不要把旧状态
用于另一市场。

动态实盘启动时会先
读取两边真实仓位，仅当保存批次的交易对、方向和匹配数量均一致时才恢复。
状态缺失但仓位非零、状态损坏或两边不一致时会暂停并要求人工恢复，不会从仓位
猜测冻结模型。每次动态实盘发单还会在任一腿开始前写入市场隔离的
`.pending.json` 日志。重启后，引擎只会自动查询已持久化订单引用的
未决腿，批次变化只应用一次，并且仅在重新读取两边交易所仓位且一致后删除日志。
缺少订单引用、交易审计未完成或状态互相矛盾时仍会禁止交易并要求人工核对；
引擎不会猜测结果或重发原订单。
测试或回放完成也不代表已获授权把 `live_enabled` 改为 `true`。

**第三步：实盘** —— 填写 `.env`，安装签名 SDK，仓位上限从刚好满足
交易所最小名义的水平开始：

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

运行时和签名 SDK 的直接依赖都已固定版本，Lighter 也固定到了具体 Git 提交。
升级依赖必须主动修改版本，并在部署新环境前重新跑完整测试和仅采集检查。

不带 `--record-only` 会连接真实账户。固定策略在两边行情就绪且越过带宽时
可能发单；动态策略还必须同时通过独立实盘开关、模型就绪、持续性检查、参考
数据检查和批次状态对账。

实盘还会按当前交易对和两边账户身份持有操作系统进程锁。同一组合的第二个进程
会在行情和策略任务启动前退出。进程退出后锁由操作系统自动释放；仍可能有进程
运行时不要删除或绕过锁文件。`--record-only` 不获取此锁。

**仪表盘。** 在终端运行时会显示实时 Rich 仪表盘：两边盘口（含数据龄/点差）、
持仓与上限、账户权益与本次会话盈亏、两个方向的可成交溢价对比完整门槛
（已含手续费与库存加价，● 表示已武装）、数据采集进度、最近成交，以及日志
尾部（完整日志写入 `logging.file`，默认 `logs/engine.log`）。`--record-only`
模式同样可用。加 `--cn` 参数可使仪表盘全部以中文显示。`--no-dashboard`
可切换为纯日志输出（nohup/systemd 等非终端环境会自动退回纯日志），也可
设置 `logging.dashboard: false`。

## 数据采集与分析

采集器在所有模式下自动运行（`recorder.enabled: true`）：每秒采样一次两边
的真实盘口，每分钟写一行：

| 列 | 含义 |
|---|---|
| `minute_ts`, `time_utc` | 分钟起点（epoch 秒 / ISO UTC） |
| `entropy_symbol`, `entropy_dex`, `hedge_symbol`, `hedge_venue` | 两条腿的原生市场身份；一个文件应只包含一个交易对 |
| `entropy_bid/ask`, `hedge_bid/ask` | 该分钟最后一次有效盘口 |
| `premium_open/high/low/close/mean/std_bps` | Entropy 相对对冲腿的中间价溢价 |
| `sell_edge_mean/max_bps` | 卖出 Entropy 方向的可成交溢价（Entropy 买一 / 对冲腿卖一 − 1） |
| `buy_edge_mean/max_bps` | 买入 Entropy 方向的可成交溢价（对冲腿买一 / Entropy 卖一 − 1） |
| `*_oracle_px`, `*_index_px`, `*_mark_px` | 最新可用的标准化参考价格；交易所不提供的字段留空 |
| `*_funding_current/last_bps_per_hour`, `*_funding_last_ts_ms` | 标准化当前/上一期资金费及交易所时间戳 |
| `*_reference_age_ms`, `reference_update_skew_ms` | 两腿参考数据的单调时钟数据龄与接收偏差 |
| `reference_basis_close_bps`, `funding_diff_close_bps_per_hour` | Entropy oracle / 对冲腿 index 基差；Entropy 当前资金费减对冲腿当前资金费 |
| `residual_open/high/low/close/mean/std_bps` | 中间价溢价减参考基差，只统计两项必要参考值齐全的样本 |
| `samples` | 该分钟约 60 秒中两边盘口同时有效的秒数 |

采集的 edge 为费前口径。请分别用 `--entropy-fee-bps` 和
`--hedge-fee-bps` 传入两边吃单费；分析工具会按实盘相同的买卖价格比公式
扣费后再统计触发频率。例如 Entropy + Lighter 使用 `0.9` 和 `0.0`，
Entropy + `tradexyz` 使用 `0.9` 和 `1.0`。旧脚本仍可使用合计值
`--fees-bps`，但它只是近似计算；两个精确费率参数必须同时提供。
费率可能因账户或交易所调整，上线前应核对实际费率。`--hours 24` 可只分析
最近数据；溢价中枢会漂移，请定期重新分析并更新 `config.yaml`。

## 配置说明

策略在 `config.yaml`（严格校验——未知键名、非有限数字及不安全的金额、频率、
比例和超时边界都会在启动时直接报错），密钥在 `.env`。
交易市场由命令行指定（`--symbol`、可选的 `--hedge-symbol`、`--hedge`）。完整的双语注释参考：
[config.example.yaml](config.example.yaml)。核心项：

| 键 | 含义 | 默认值 |
|---|---|---|
| `thresholds.midline_bps` | 溢价中枢（必须实测！） | — |
| `thresholds.upper_bps` / `lower_bps` | 入场带宽（> 0） | — |
| `entropy.dex` | Entropy 在 Hyperliquid 上的 dex 名 | `io` |
| `*.taker_fee_bps` | 各所吃单费 | Entropy 0.9；Lighter 0.0；tradexyz 对冲腿 1.0 |
| `*.max_position_usd` | 各所持仓上限 | 1000 |
| `*.max_orders_per_min` | 各所每分钟下单预算（滑动 60 秒） | 120；Lighter 对冲腿 30 |
| `sizing.take_fraction` | 吃掉可套利深度的比例 | 0.5 |
| `sizing.max_order_notional_usd` | 每次切片两条腿各自实际计划名义金额的硬上限 | 500 |
| `inventory.scale_bps` / `floor_frac` | 库存阶梯（仓位超过上限的 `floor_frac` 后额外加价） | 10 / 0.5 |
| `execution.premium_persist_sec` | 信号需持续多久才触发 | 0.3 |
| `execution.*` | 滑点保护、超时、对账周期等 | 见配置文件 |
| `recorder.*` | 分钟数据；只读模式信号生命周期路径 | 开启，`logs/minutes.csv`；`logs/signals.csv` |
| `recorder.signal_rotate_daily` | 按 UTC 日轮转并校验压缩信号明细 | true |
| `reference.rest_recovery_sec` / `stale_sec` | REST 恢复周期 / 参考数据过期阈值 | 15 / 60 |
| `reference.residual_alert_bps` / `residual_persist_sec` | 状态化观察告警的残差阈值 / 持续时间 | 20 / 30 |
| `strategy.mode` / `strategy.live_enabled` | 固定带或滚动残差策略；独立动态实盘闸门 | `residual_dynamic` / false |
| `strategy.state_file` / `strategy.event_csv` | 基础路径；运行时追加市场哈希及实盘/影子标记 | `logs/campaign-state.json`；`logs/strategy-events.csv` |
| `slippage.*` | 真实成交 p95 预算、硬上限及开仓降级控制 | 见配置文件 |
| `logging.dashboard` / `logging.file` | 终端仪表盘；开启时日志写入文件 | 开启，`logs/engine.log` |

## 密钥配置（`.env`，仅实盘需要）

- **Entropy / tradexyz（Hyperliquid）** —— 在
  <https://app.hyperliquid.xyz/API> 创建 API（agent）钱包。`HL_PRIVATE_KEY`
  填 **agent 钱包私钥**，`HL_ACCOUNT_ADDRESS` 填主账户地址。当
  `--hedge tradexyz` 时两条腿默认共用该账户（内部自动共享 nonce 序列）；
  如需分开，设置 `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`。注意给
  所交易的各 dex 分别充入保证金。
- **Lighter** —— `LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、
  `LIGHTER_API_PRIVATE_KEY`，必须注册在与启动参数 `--hedge` **相同的部署**上
  （主网与 Robinhood 链是两套独立的账户和密钥——参见
  [lighter-python](https://github.com/elliottech/lighter-python)）。同一账户上同时运行的
  每个进程必须使用独立的 API key index/私钥；共用 key 也会共用 nonce 序列，
  不受支持。

## 执行机制

- 两条腿**同时发出吃单**：Lighter 用带均价保护的市价单，在鉴权 websocket
  上异步确认成交；Hyperliquid HIP-3 用 IOC 限价单同步结算。HIP-3 当前不兼容
  `cloid`，因此请求不会携带它；超时或 5xx 会保持为明确的“结果未知”。由于没有
  订单引用，引擎会停机并要求人工核验仓位/恢复，不会自动提交修复单，也绝不会
  盲目重发原订单。Lighter 的提交与成交确认分别拥有一个完整的
  `settle_timeout_sec` 窗口；提交超时也按“结果未知”处理，因为订单可能已经到达
  交易所。
- **持续性闸门**（`premium_persist_sec`）：信号先"武装"，持续存在才触发，
  过滤单 tick 的假信号。
- **库存阶梯**：仓位超过上限的 `floor_frac` 后，同方向加仓需要线性递增的
  额外溢价，满仓时最高加 `scale_bps`。
- **净敞口对冲**：两腿成交不对等时立即用 reduce-only 单（带滑点保护）
  削减敞口，并每 `reconcile_sec` 与链上仓位对账。
- **故障隔离**：被限频的交易所短暂暂停；交易所不可达（如例行维护）时暂停
  交易并每 `venue_probe_sec` 探测直至恢复；连续 `max_consecutive_errors`
  次执行异常则整体停机。
- **崩溃证据与单实例**：动态实盘在发单前写入不含密钥的未决执行日志；重启
  后只自动恢复带可靠订单引用且审计完整的记录，并在双腿仓位刷新一致前保持
  关闭交易。其余情况要求人工核对。相同账户和交易对的第二个实盘进程会被
  操作系统锁拒绝，仅采集进程不受影响。
- **安全关机**：收到停止信号后不再产生新机会，但会等待所有已经提交的双腿
  执行得到结果后才关闭交易所连接。等待过久会写入 critical 日志，不会由程序
  主动取消在途下单任务。初始化失败时也会关闭此前已创建的全部任务和交易所；
  任一受监督后台任务报错或意外提前退出，都会触发停机，并在清理完成后让进程
  以非零状态退出。
- **无订单影子**：`--record-only` 不会提交订单。动态影子成交只使用计划价格，
  不能当作实盘结果；不带 `--record-only` 时，只要实盘闸门允许就可能动用真金白银。

## 目录结构

```
main.py                  入口（--record-only，默认即实盘）
entropy_arb/config.py    YAML + .env 配置契约与校验
entropy_arb/book.py      订单簿 + 含手续费的套利规模计算
entropy_arb/models.py    标准化订单结果数据结构
entropy_arb/feeds.py     官方 HL ws + zkLighter ws 行情
entropy_arb/venue_hl.py  Hyperliquid dex 适配器（Entropy、tradexyz）
entropy_arb/venue_lighter.py  zkLighter 适配器（主网、Robinhood 链）
entropy_arb/venues/base.py  统一交易所适配器协议
entropy_arb/venues/registry.py  显式适配器工厂注册表
entropy_arb/engine.py    双交易所策略主循环
entropy_arb/dashboard.py Rich 终端仪表盘
entropy_arb/recorder.py  分钟级盘口 + 只读信号生命周期采集
entropy_arb/strategy.py  滚动残差模型与纯策略决策
entropy_arb/campaign.py  单批次持久状态与启动对账
entropy_arb/recovery_state.py  未决执行持久日志
entropy_arb/live_lock.py 实盘账户/交易对跨进程锁
tools/analyze.py         分钟阈值 + 可选批次汇总
tools/replay_strategy.py 只读最优盘口策略回放
tests/                   python3 -m pytest tests/
```

当前命令行仍然只运行两条腿。交易所创建已经统一经过适配器协议和显式注册表；
这是后续按阶段实现多对冲交易所架构的兼容基础，完整设计见
`docs/superpowers/specs/2026-08-27-multi-hedge-arbitrage-design.md`。

## 已知风险

- **中枢填错就是亏钱策略。** 溢价中枢会漂移，请定期重新测量并保持
  `config.yaml` 与市场同步。
- **USDG 基差**（`lighter-rh`）：对冲腿以 USDG 计价，持续溢价中有
  一部分是稳定币本身的基差；midline 吸收其水平，但 USDG 的*变动*是真实盈亏。
- **资金费**：两个交易所有独立费率。两种策略都会统一单位、记录并告警，
  但资金费不阻止开仓，也不从阈值中扣除；仓位上限请保持保守。
- **薄盘口**：Entropy 深度可能很小；`take_fraction` 与名义上限控制单笔规模，
  但部分成交后对冲腿的滑点是真实存在的。
- **交易时段**：股票类永续（如 SNDK）盘后各所预言机行为不同，建议加宽带宽
  或避开盘后。
- **单腿风险**：一条腿成交后另一条可能失败。通常会自动对冲并对账；但无订单
  引用的 Hyperliquid 超时/5xx 会故意停机等待人工恢复，因此必须持续监控。

风险自负。本软件直接操作真实资金，本文档不构成任何投资建议。请从最小的
仓位上限开始。任何实盘前都应再次运行 `--record-only`，检查新增 reference
字段和 `logs/engine.log`；这些检查也不代表实盘无风险。

## 开源协议

[MIT](LICENSE)
