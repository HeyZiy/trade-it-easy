# 研究索引

研究按用途分为可复用工具和具体研究。

## 可复用工具：tools

| 目录                                                | 用途                  |
| ------------------------------------------------- | ------------------- |
| [market\_regimes](tools/market_regimes/README.md) | 指数月/周趋势状态与收益区间归因    |
| [single\_factor](tools/single_factor/README.md)   | 因子 IC、分组、时间对齐与中性化诊断 |
| [etf\_data](tools/etf_data/README.md) | ETF 本地行情缓存、复权信号价与数据质量检查 |

single\_factor 只保留可复用的因子诊断引擎（`single_factor_test.py` 与代码生成器）；趋势研究中的 tools 依赖该研究的缓存与参数，仍跟随研究目录。

## 具体研究：studies

| 目录                                                          | 研究对象                          | 当前说明                                            |
| ----------------------------------------------------------- | ----------------------------- | ----------------------------------------------- |
| [roe\_quality](studies/roe_quality/README.md)               | ROE / 质量池历史研究与 v1～v3 对照 | 已收线；保留池质量、容量及执行诊断证据；现行 v5 见 quality\_momentum |
| [quality\_momentum](studies/quality_momentum/README.md) | 经营支撑与价格兑现；现行质量动量 v5 | 日频动量全期收益与回撤胜过10组随机，优势集中于早期；第六节记录结果及证据边界 |
| [value_revenue](studies/value_revenue/README.md) | 价值因子EP × 营收同比加速 | 第一轮独立研究；EP有收益关联，营收加速增量偏弱，交互缺乏充分支持 |
| [expectations](studies/expectations/README.md) | 低预期 × 已披露盈利维持 | 低预期＋历史盈利维持全期核查完成，未显示稳定增量；60日控制系数HAC t=−0.37 |
| [trend](studies/trend/README.md)                                     | 个股趋势策略                        | 已收线（v3证伪）；保留版本链、尸检与失败证据 |
| [industry\_momentum](studies/industry_momentum/README.md)            | 行业 ETF 动量轮动                   | 现行研究基线 v3\_3\_2 / v3\_3\_4；证据与去重对照见 README；生产规则见 ../strategy/industry\_momentum.md |
| [etf\_rotation](studies/etf_rotation/README.md) | ETF跨资产趋势与相对强弱轮动 | 原15只与37只混合池研究；收益假设、交易规则、已有对照及后续主线见README |
| [broad\_index\_strategies](studies/broad_index_strategies/README.md) | 宽基ETF长期配置的持有体验与择时 | 一级比较12只ETF；二级固定参数单跑恒生科技，20/10及ATR通道改善当前样本体验，结论按标的区分 |

## 研究如何形成积累

研究问题可以包含多个猜想，实验用来缩小不确定性。证据不足不等于猜想已被证伪；数据审计和执行诊断也不必包装成收益猜想。

新研究说明优先记录：

1. 问题：想弄清什么？
2. 实验：怎么做，改了什么？
3. 结论：证实 / 证伪 / 证据不足，附关键数字（等有再补）。

每次运行记录脚本版本、参数、资金、日期、数据口径及结果。聚宽实验继续保留可直接粘贴的完整单文件；

本地从仓库根运行市场状态工具：`python -m research.tools.market_regimes --help`。
