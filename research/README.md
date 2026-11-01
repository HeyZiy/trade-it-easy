# 索引

## tools

完整入口见 [研究方法与数据工具](tools/README.md)。

| 目录                                                | 用途                                        |
|-----------------------------------------------------|---------------------------------------------|
| [market\_regimes](tools/market_regimes/README.md)   | 指数月/周趋势状态与收益区间归因             |
| [factor\_analysis](tools/factor_analysis/README.md) | 公共因子计算、研究流程、报告及可选 SDK 扩展 |
| [etf\_data](tools/etf_data/README.md)               | ETF 本地行情缓存、复权信号价与数据质量检查  |
| [fundamental\_analysis](tools/fundamental_analysis/README.md) | 基本面九类指标的时序计算 |
| [technical\_analysis](tools/technical_analysis/README.md) | 技术指标时序计算及显式行情准备 |
| [announcement\_analysis](tools/announcement_analysis/README.md) | 公告筛选、原文下载与文本提取 |

factor\_analysis 提供可调用函数和聚宽代码生成器；具体股票池、时间规则与参数留在 studies。趋势研究中的 tools 依赖该研究的缓存与参数，仍跟随研究目录。

## studies

| 目录                                                                 | 研究对象                            | 当前说明                                                                                  |
|----------------------------------------------------------------------|-------------------------------------|-------------------------------------------------------------------------------------------|
| [roe\_quality](studies/roe_quality/README.md)                        | ROE / 质量池历史研究与 v1～v3 对照  | 已收线；保留池质量、容量及执行诊断证据；现行 v5 见 quality\_momentum                      |
| [quality\_momentum](studies/quality_momentum/README.md)              | 质量池相对强弱轮动；现行v5 | 冻结现有规则，复用已有消融；小额实盘准备，实验及旧版本归档 |
| [value_revenue](studies/value_revenue/README.md)                     | 价值因子EP × 营收同比加速           | 第一轮独立研究；EP有收益关联，营收加速增量偏弱，交互缺乏充分支持                          |
| [expectations](studies/expectations/README.md)                       | 低预期 × 已披露盈利维持             | 低预期＋历史盈利维持全期核查完成，未显示稳定增量；60日控制系数HAC t=−0.37                 |
| [trend](studies/trend/README.md)                                     | 个股趋势策略                        | 已收线（v3证伪）；保留版本链、尸检与失败证据                                              |
| [industry\_momentum](studies/industry_momentum/README.md)            | 行业 ETF 动量轮动                   | 研究基线 lM_v5（最简合理版）；lM_v3_3_2 为生产规则出处；已收线实验已断舍离                  |
| [etf\_rotation](studies/etf_rotation/README.md)                      | ETF跨资产趋势与相对强弱轮动         | 独立维护15只基线与17只能源扩充；行业材料见industry_momentum                               |
| [broad\_index\_strategies](studies/timing/README.md) | 宽基ETF长期配置的持有体验与择时     | 一级比较12只ETF；二级固定参数单跑恒生科技，20/10及ATR通道改善当前样本体验，结论按标的区分 |
