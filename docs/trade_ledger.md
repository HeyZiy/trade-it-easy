# 成交台账（trade_ledger）规格

> 状态：**已实现**。本文是台账的设计决策与实现规格；
> 改动本文所列决策时，必须同一提交更新本文档（AGENTS.md 文档同步纪律）。

## 一、问题陈述（设计动机）

若持仓/资金/下单继续以妙想模拟仓为事实来源：建仓要在妙想 App 手动跟、
卖出走模拟市价单、A1 敞口读模拟余额——而实盘是"人看日报手动下单"，
模拟仓就是一个**需要人工保持同步的平行假账户**：忘记跟买一次，
次日的卖出判定、整手取减、残腿清单全部对着假数算。此外：

- 策略的全部记忆（`position_exit_state.json`）孤身在服务器，无备份；
- 判定语境从不沉淀（`reports/` gitignored），"积累之后做复盘"没有数据地基；
- 仓库 public，资金数字与实盘动议都不该上 GitHub。

## 二、方案

持仓/资金唯一事实来源 = 服务器端 **append-only 成交台账**：

- 每笔成交（cron 自动记的影子成交 + 手动记的底仓）追加一行 JSONL；
- 持仓/现金/敞口全部是**流水推导视图**，不存可变状态表；
- 判定核（`sell_rules`/`rebalancer`/`ExitLedger`）签名零改动——台账推导结果按
  持仓 dict 同形供料，替换的只是入口脚本的取数段；
- 妙想只承担每日选股名单一个消费者；
- **唯一名义基数写死代码（100 万）**：流水、报告、视图的一切绝对数都是名义股数/
  名义额，呈现层全百分比；真实资金数字在系统里不存在，% →钱的乘法留在用户下单时；
- 数据不上 git：台账 + `position_exit_state.json` 只存服务器；读优先架构——服务器
  上所有任务读本机权威活数据零同步问题，本地经 rsync 手动拉副本，仅服务复盘与备份。

### seam

唯一新接口面：`trade_ledger` 模块，两个动词——

```
append_trade(record)                     # 写侧：cron 成交确认 / book.py 手动建仓
derive(trades, as_of, prices) -> View    # 读侧：持仓 dict 同形供料 + entry_map + 敞口(invested, equity)
```

## 三、用户故事

1. 作为策略运行者，我希望卖出判定对着台账而不是妙想算，以便不再需要记得去模拟仓跟仓。
2. 作为策略运行者，我希望日报输出当前持仓与权重，以便实盘直接照日报下单。
3. 作为策略运行者，我希望建仓名单确认后自动记影子成交，以便日常零手工维持账本。
4. 作为实盘跟单者，我希望实盘现有底仓能手动记成建仓流水，以便策略从第一天就管理真实已有的票。
5. 作为策略运行者，我希望 T+1、整手、残腿语义由台账推导保留，以便判定结果仍贴合 A 股可执行性。
6. 作为复盘者，我希望每笔流水带理由码串，以便日后统计"哪类信号赚钱"而不依赖 gitignored 日报。
7. 作为复盘者，我希望收益率/权重/回撤在固定名义基数下跨时间可比，以便中途加减金不污染策略成绩单。
8. 作为仓库主人，我希望 GitHub（public）上不存在任何真实资金数字与实盘动议，以便隐私问题终结。
9. 作为用户，我希望本地一条 `book.py view` 看到名义持仓/权重/peak/现金，以便不登服务器也能查账。
10. 作为用户，我希望本地副本陈旧时视图直接显示"流水截止日"，以便知道自己看的是不是旧账。
11. 作为开发者，我希望判定核零签名改动，以便既有单测（FakeClient→台账 dict）近乎原样迁移。

## 四、实现决策

### 4.1 模块与归属

- 包 `src/trade_ledger/`（跨策略共享且有交易语义 → 共享概念包，目录规则）；
  单文件 `ledger.py` 承载 append/derive 两动词即可，视图渲染归 CLI。
- 流水文件 `data/trade_ledger.jsonl`（gitignored，与 `data/*.json` 同目录不同命：
  唯一不入 git 的数据不设防，因为压根不进 git）。
- CLI `book.py` 在仓库根（与 `pullback_sell.py` 等入口脚本同级）：
  `book.py open <code> <成本价> <权重%>`（手动建仓，换算名义整手股数）、
  `book.py view [--account X]`（名义+百分比渲染，兼对账自检：视图内校验 cash≥0、
  持仓与 peak 文件一致）。
- 读侧现价装配单点 `src/trade_ledger/prices.py`（`snapshot_with_prices`）：
  derive 本体保持零 I/O，消费 market_value 的入口（A1 敞口 / 核心再平衡 /
  卫星轮动）统一从这里取定价完成的快照；只需 count/entry_map 的消费方
  （卖侧判定，逐票本就过 feeds 拿全量日线）直接用 derive。

### 4.2 流水记录 schema（一行一 JSON dict）

| 字段 | 语义 |
|---|---|
| `date` | 成交日（YYYY-MM-DD，交易日） |
| `code` / `name` | 经 `canonical_stock_code()` 归一 |
| `side` | `buy` / `sell` |
| `qty` | **名义股数，必为 100 整数倍**（append 时 floor/round_lot 收敛） |
| `price` | 名义成交价（影子成交=判定时点现价；手动=用户输入近似） |
| `account` | `core` / `satellite` / `quality_pool` —— **归因标签**，不参与分账（见 4.5 单池口径）；derive 时按末次买入带到持仓上，供核心/卫星归属拆分 |
| `reasons` | 理由码字符串数组（如 `["pullback_ma10","sector_ok","B1_ext"]`），复盘地基 |
| `source` | `cron` / `book` |
| `manual` | bool：实盘底仓补记（入场日语义=该日，不追溯真实买入历史） |

- 现金不落流水：现金 = 名义期初 − Σ买 + Σ卖。**单池口径**：
  现实妙想本来就是一个账户装全部策略，A1 敞口/卫星 10% 预算/核心权重全都按这个
  混合口径算——台账整本单池推导，保持行为零漂移；`account` 标签服务复盘归因与
  持仓归属拆分（rebalancer 按标签分核心/卫星，不再用代码名单反推），
  `view` 可按标签过滤展示。
- 无手续费模型、无调入调出条目（见"范围外"）。字段演进走"加列容忍旧行"。

### 4.3 读侧推导（derive）

- 持仓 dict 与妙想同形：`count`（Σ买−Σ卖）、`avail_count`（**date < as_of 的净持仓**，
  即 T+1）、`cost_price`（移动加权平均）、`market_value`（count×现价）——
  `rebalancer.compare`、`execute_batch` 的卖出≤持仓校验、质量池 `assembly` 的
  持仓计数，现读的字段一个不缺。同形之外只多一个 `account`（末次买入的标签，
  缺省 `core`），供核心/卫星归属拆分。
- `entry_map`：每只最新 `buy` 流水的 date（`book.py view` 的入场日列）。
- 敞口：`invested = Σ market_value`，`equity = 名义期初全账户合计 + 浮盈`——
  `pullback_analysis._fetch_portfolio_exposure` 读此值（A1 档位判定本来就是比率，
  名义口径与真实口径结论一致）。现价取数失败时维持现有 fail-open `(0.0, 0.0)`。

### 4.4 写点（逐入口）

| 入口 | 写点 |
|---|---|
| `pullback_sell.py` | 持仓/入场日由 `derive(trades, 今日, 现价)` 供料 |
| `sell_pipeline.execute_sells` | 命中的卖出信号 `append_trade`（价=判定时点现价）；四态结果（已记账/记账失败/不足一手/试运行） |
| `pullback_analysis.py` | 名单中**通过买侧全部门槛**的票自动 `append_trade(buy)`（`buy_pipeline.record_shadow_buys`）：gate+A1/A2 裁决放行、信号按评分从高到低依次记账（无评分门槛）、名义股数 = 仓位上限 ÷ 信号日收盘（floor_lot 整手，不足一手不记）、同票当日幂等；只出建议的（禁开仓日/--stocks 指定名单调试）不记 |
| `etf_observe.py::_compute_allocation` | 持仓/余额读 derive（`rebalancer.compare` 签名不动） |
| `etf_observe.py::_execute_batch` | 调仓批次走 `trade_ledger.execute_batch`（account=core）：入口只装配指令与渲染，执行持仓/现金由批次 module 从指定台账读取 |
| `industry_momentum.py::run/_execute` | 持仓/余额 derive 供料；轮动批次走 `execute_batch`（account=satellite），通用安全校验随批次接口收口；卫星 10% 预算为策略自有 policy，留在入口前置校验 |
| `src/mx/executor.py` | 纯计算模块：`round_lot`/`floor_lot` 整手唯一口径，判定核继续消费；妙想现役仅剩 `MXService` 选股/资讯服务（`MX_APIKEY` 从 .env 保留） |

批次 interface 为 `execute_batch(orders, account=..., trade_date=..., path=...)`，
不接收调用方的持仓/现金快照，也不按 account 分账。批次日期不得早于台账最新
流水日期，避免借用未来持仓或资金；`append_trade` 的手动旧日期补记能力保留。

整手、代码及四位成交价归一后，按代码累计全部卖量，并预检买入总额是否超过
台账现金与计划卖出回款；任一安全预检失败，返回 abort，整批零追加。通过后先
卖后买，每笔写入前重新读取实际台账持仓与现金。卖出记账失败不贡献预计回款；
运行中现金不足的原买单标记 failed，股数不自动缩减，继续检查其余指令。缩量、
策略持仓名额和费用预检归各策略；通用批次保持持仓总量校验，T+1 可卖量由策略核使用。
一条指令只产生一条执行结论，报告金额与实际四位记账价一致。

### 4.5 起点口径

- 起点 = 实盘现有底仓经 `book.py open` 手动补记流水（`manual` 字段入场日语义=录入日，
  不追溯真实买入历史）。
- `position_exit_state.json` 的 peak/held_days 从录入日重新积累（可接受，
  台账流水日后同样收敛）。
- 名义期初 100 万（单池，代码常量），与判定/预算比例常量同区维护，改动须同步本文。
  战术子账户是趋势策略在单池里的记账命名空间，不按资金规模拆账——
  买侧单票限额是策略固定风控参数、绝对额口径（`trend_strategy.md` 仓位规则），单池 100 万不稀释它。

### 4.6 传输与部署

- crontab 任务集合不变；`pullback_sell.py` 须在 14:45 前拿到现价——沿用现取数 seam。
- 本地快捷命令（README/AGENTS.md 记录）：
  `rsync -av --include 'trade_ledger.jsonl' --include 'position_exit_state.json' ...`
  手动触发；本地视图数据带"流水截止日"陈旧度提示。
- 服务器无需任何新凭据（不上 git、不连云）。

## 五、测试决策

- 好测试=只测外部行为：derive 是纯函数（流水列表 + as_of + prices → 视图），
  不测内部 dict 结构；append 测"写一行再 derive 读回"闭环。
- 重点用例：T+1 边界（当日买不可卖）、整手收敛（残腿<100）、移动加权成本、
  跨归因标签单池（account 不隔离持仓/现金）、手动底仓 manual 字段的入场日语义、
  空仓日 derive 恒等空列表。
- 判定核等价性：既有 `test_sell_pipeline.py` 的 plain-dict 假持仓模式原样保留
  （它们本来就不碰 client），新增 fixture 用 `tmp_path` 的 jsonl——与
  `ExitLedger(path=tmp/…)` 先例同款。
- 批次测试先建立真实流水，再经 execute_batch 验证无新增流水、累计超卖、卖出
  失败后的实际现金、过期调用方视图、四位价舍入、执行结论数量和执行后 derive 自洽。

## 六、范围外

- 真实账户跟踪、真实成交回填、实盘/影子偏差对账；
- 手续费/印花税/分红除权模型；
- 调入调出（资金流事件）——名义账本永久无资金流；
- 日报 md 归档回流、多设备同步、云数据库、git 回流（仓库 public，流水=公开实盘动议）；
- 妙想下单兼容层/双轨过渡。
