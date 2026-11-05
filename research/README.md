# 索引

## tools

完整入口见 [研究方法与数据工具](tools/README.md)。

| 目录                                                            | 用途                                        |
|-----------------------------------------------------------------|---------------------------------------------|
| [market\_regimes](tools/market_regimes/README.md)               | 指数月/周趋势状态与收益区间归因             |
| [factor\_analysis](tools/factor_analysis/README.md)             | 公共因子计算、研究流程、报告及可选 SDK 扩展 |
| [etf\_data](tools/etf_data/README.md)                           | ETF 本地行情缓存、复权信号价与数据质量检查  |
| [fundamental\_analysis](tools/fundamental_analysis/README.md)   | 基本面九类指标的时序计算                    |
| [technical\_analysis](tools/technical_analysis/README.md)       | 技术指标时序计算及显式行情准备              |
| [announcement\_analysis](tools/announcement_analysis/README.md) | 公告筛选、原文下载与文本提取                |

factor\_analysis 提供可调用函数和聚宽代码生成器；具体股票池、时间规则与参数留在 studies。趋势研究中的 tools 依赖该研究的缓存与参数，仍跟随研究目录。

## studies

| 目录                                                    | 研究对象                          | 当前说明                                                                                  |
|---------------------------------------------------------|-----------------------------------|-------------------------------------------------------------------------------------------|
| [quality\_momentum](studies/quality_momentum/README.md) | 质量池相对强弱轮动；现行v5        | 冻结现有规则，复用已有消融；小额实盘准备，实验及旧版本归档                                |
| [value\_revenue](studies/value_revenue/README.md)       | 价值因子EP × 营收同比加速         | 第一轮独立研究；EP有收益关联，营收加速增量偏弱，交互缺乏充分支持                          |
| [expectations](studies/expectations/README.md)          | 低预期 × 已披露盈利维持           | 低预期＋历史盈利维持全期核查完成，未显示稳定增量；60日控制系数HAC t=−0.37                 |
| [etf](studies/etf/README.md)                            | 全 ETF 跨资产动量轮动（3 只等权） | 独立新研究，IM\_v1 首轮修订                                                               |
| [rotation](studies/rotation/README.md)                  | ETF跨资产趋势与相对强弱轮动       | 独立维护15只基线与17只能源扩充；行业规则见strategy/industry\_momentum.md                  |
| [timing](studies/timing/README.md)    | ETF长期配置的持有体验与择时   | 一级比较12只ETF；二级固定参数单跑恒生科技，20/10及ATR通道改善当前样本体验，结论按标的区分 |

