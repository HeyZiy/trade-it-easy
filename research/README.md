# 研究索引

研究按用途分为可复用工具和具体研究。

## 可复用工具：tools

| 目录                                                | 用途                  |
| ------------------------------------------------- | ------------------- |
| [market\_regimes](tools/market_regimes/README.md) | 指数月/周趋势状态与收益区间归因    |
| [single\_factor](tools/single_factor/README.md)   | 因子 IC、分组、时间对齐与中性化诊断 |

single\_factor 只保留可复用的因子诊断引擎（`single_factor_test.py` 与代码生成器）；趋势研究中的 tools 依赖该研究的缓存与参数，仍跟随研究目录。

## 具体研究：studies

| 目录                                                          | 研究对象                          | 当前说明                                            |
| ----------------------------------------------------------- | ----------------------------- | ----------------------------------------------- |
| [roe\_quality](studies/roe_quality/README.md)               | 质量类选股（ROE 轮动 + 质量轮动 + 质量池系统）  | 打分线已收线；质量池 A 过线并完成个人化容量实验（README\_pool.md）         |
| [value_revenue](studies/value_revenue/README.md) | 价值因子EP × 营收同比加速 | 第一轮独立研究；EP有收益关联，营收加速增量偏弱，交互缺乏充分支持 |
| [expectations](studies/expectations/README.md) | 低预期 × 已披露盈利维持 | 低预期＋历史盈利维持全期核查完成，未显示稳定增量；60日控制系数HAC t=−0.37 |
| [trend](studies/trend/)                                     | 个股趋势策略                        | 保留原版本、辅助脚本及数据                                   |
| [industry\_momentum](studies/industry_momentum/)            | 行业 ETF 动量轮动                   | 保留原版本；生产策略说明见 ../strategy/industry\_momentum.md |
| [etf\_rotation\_reference](studies/etf_rotation_reference/) | 原 research/new\.py 的 ETF 策略参考 | 来源代码保留，尚未整理为明确研究问题                              |

## 研究如何形成积累

研究问题可以包含多个猜想，实验用来缩小不确定性。证据不足不等于猜想已被证伪；数据审计和执行诊断也不必包装成收益猜想。

新研究说明优先记录：

1. 问题：想弄清什么？
2. 实验：怎么做，改了什么？
3. 结论：证实 / 证伪 / 证据不足，附关键数字（等有再补）。

每次运行记录脚本版本、参数、资金、日期、数据口径及结果。聚宽实验继续保留可直接粘贴的完整单文件；

本地从仓库根运行市场状态工具：`python -m research.tools.market_regimes --help`。
