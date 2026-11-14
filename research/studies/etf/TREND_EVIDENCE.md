# ETF 趋势轮动的研究依据

本研究检验的收益来源是 ETF 自身的中期趋势在买入后延续。绝对趋势决定是否参与，ETF 之间的相对强弱用于排优先级，仓位和标的相关性决定承担多少风险。各项规则的增量需要通过相同账户口径的本地回测估计。

| 假设 | 原始研究与适用范围 |
|---|---|
| 中期趋势可能延续 | Moskowitz、Ooi 与 Pedersen（2012）在 58 个股指、汇率、商品和债券期货品种上观察到 1 至 12 个月的收益延续，详细分析了 12 个月回看、1 个月持有的多空组合。它为测试中期趋势提供动机。本地实验的多头 ETF、持有期与费用均有区别，具体窗口仍待验证。[Time Series Momentum，原文](https://docs.lhpedersen.com/TimeSeriesMomentum.pdf) |
| 趋势期限有市场依赖 | Shi 与 Zhou（2017）在中国主要股票指数上同时观察到短期趋势延续与较长周期反转，结果对回看和持有期敏感。其指数样本与本地动态 ETF 池存在差异，窗口应做相邻期限与分段比较。[Time Series Momentum and Contrarian Effects in the Chinese Stock Market，原文](https://arxiv.org/abs/1702.07374) |
| 同向敞口和高波动可能放大回撤 | Baltas 与 Kosowski（2015）在期货趋势组合中检验相关性调整，发现高相关期降低杠杆有助于改善风险调整后表现，同时会增加换手。Moreira 与 Muir（2017）在若干权益因子及外汇套利上发现，高波动时降低敞口可改善样本夏普。对本地 ETF，相关性过滤和波动率减仓分别作为候选，效果需同时比较收益、回撤、仓位与换手。[Baltas 与 Kosowski，原文](https://spiral.imperial.ac.uk/entities/publication/358e362e-3968-492b-9a24-cb579828e358) · [Moreira 与 Muir，原文](https://www.nber.org/system/files/working_papers/w22208/w22208.pdf) |
| 急涨后的入场质量可能变差 | Nagel（2012）在股票和行业组合上发现短期反转收益随流动性环境变化，市场承压时更明显。它提供了检验急涨过滤的动机。ETF 过滤阈值、放弃趋势延续的机会成本，以及滤掉的是噪声还是有效突破，都由本地对照衡量。[Evaporating Liquidity，原文](https://www.nber.org/system/files/working_papers/w17653/w17653.pdf) |
| 成交摩擦会改变短周期策略评价 | Frazzini、Israel 与 Moskowitz 使用发达市场股票机构成交数据估计交易成本，展示了不同收益来源对成本和换手的敏感性。本地日线实验以统一费率比较，另用成本和延迟成交检验敏感性。实际可成交性需要核对报价、涨跌停和成交量。[Trading Costs of Asset Pricing Anomalies，作者工作论文](https://www.aqr.com/insights/research/working-paper/trading-costs-of-asset-pricing-anomalies) |

基线、单项改动和组合规则使用相同数据、复权、撮合和费用。分段收益以各段起点为基准，最大回撤在区间内重新计算。已用于解释回撤的历史仍属于探索样本，后段留出检验只能评价本次固定规则在该段的表现。后续聚宽复核应核对历史成分、决策时点和完整成交记录。
