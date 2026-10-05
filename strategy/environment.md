# 环境识别层（environment）

> **模块身份**：跨策略的环境识别指标库，不是一套交易策略，也不是任一策略的开仓门控。
> 当前仅一轴：市场门控（gate\_state）。
> 策略地图与各消费者职责见 [overview.md](./overview.md)。

> **分层原则**：环境层只产**标签与判定事实**，不持有 policy。
> 环境指标的存废标准 = 对目标策略的**分离度**：能否有效区分该策略的强势期与非强势期；
> 各策略接入某轴前，须先跑"该轴 × 该策略"归因。
> **统一环境快照**：环境判定任务 `env_report.py` 每交易日 14:35 产出
> `data/environment.json`，消费方读快照，不各自取数判定
> （趋势双任务 / ETF 周报 / 卫星轮动均经 `load_environment` 读类型化视图）。

***

## 一、轴与实现

| 轴             | 回答的问题                  | 实现                                                                      | 节拍               | 输出                                                             |
| ------------- | ---------------------- | ----------------------------------------------------------------------- | ---------------- | -------------------------------------------------------------- |
| gate_state（市场门控）  | 指数趋势结构是什么（标签）          | `src/market_state/market_gate.py`（AmazingData 指数单源，快照补当日 bar）           | 日频，14:35 快照判定        | `data/environment.json` gate_state 节                               |

**统一快照**：`env_report.py`（每交易日 \~14:35，消费任务前）落盘
（`src/market_state/environment.py` 唯一 owner：schema + 类型化视图 + 过期判定）；
只产标签不产 policy。定时任务已启用（`deploy/crontab.server`，14:35）；
`gate_state` 判定时点统一在 14:35，消费任务（ETF 周一 14:40 / 卖出 14:45 / 卫星 14:50 / 买入分析 15:10）读同一份判定。

***

## 二、判定规则（现状 spec）

### gate_state：国证A指（399317）均线结构 5 级

判定只依赖国证A指（399317）日线的均线结构，优先级 trending\_down > trending\_up > sideways > weak\_up > chaos；
sideways 判定阈值为收盘偏离 MA20 < 1.5%。标签语义单点在 `market_gate._gate_state_label`；标签一律过去式措辞，不读作行情预测。
**响应动作（CAN\_OPEN / 收紧档 / 清仓）是各策略的 policy**（趋势回踩 = `buy_pipeline.CAN_OPEN_STATES`；卫星轮动无门控），
见 [trend\_strategy.md](./trend_strategy.md)「市场状态分级与响应动作」。

***

## 三、运行

- 环境快照（每日）：`python env_report.py`（`--force` 跳过交易日检查）；消费方经
  `src/market_state/environment.load_environment` 读类型化视图；
  "verdict 能不能消费"（缺失/不可用/过期）谓词单点 `gate_verdict_issue`，
  读到不可用 verdict 后 fail-closed 还是 fail-soft 是各消费方 policy：
  买入分析 fail-closed（不开新仓，与兜底 chaos 等价）、卖出任务 fail-soft（门控仅展示）、
  **ETF 核心再平衡不消费**（不读快照、无执行前置、无保护性默认）、
  卫星轮动不消费。

**消费边界**：环境标签只作"是否开新仓"的一票（趋势链），
**不得**进入任何组合级仓位动作（`src/etf/rebalancer.py` 与市场状态完全无关）。

