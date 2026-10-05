# 市场状态收益诊断

将已有研究的逐日收益曲线，按市场指数已经结束的月线/周线状态归因。
适用于 `roe_quality`、`trend_bt`、`industry_momentum` 等研究；导入模块不联网、不写文件。

## 默认窗口的含义

10月均线用于较慢的趋势背景，20周均线约覆盖五个月，用于中期变化。
10月均线有 [Faber趋势过滤研究](https://www.cambriainvestments.com/wp-content/uploads/2018/01/A-Quantitative-Approach-to-Tactical-Asset-Allocation.pdf)
的先例，该论文用它代表接近200日的趋势尺度。月末收盘的10点均值与每日收盘的200点均值并不相等。
20周是这里为中期诊断选取的起始窗口，这个10月/20周组合并非已验证或优化的参数。
月/周信号来自同一价格序列，彼此相关。

若检查参数稳定性，先固定一个小范围，例如月窗口8/10/12、周窗口16/20/24，
检查状态收益排序是否稳定，并保留后续时期验证。不要从网格中挑历史收益最高的组合。
模块提供窗口配置，但不会自动优化或筛选参数。

## 命令行

从仓库根目录运行，默认拉国证A指(`sz399317`，与生产环境层同指数)公开日线，缓存按指数代码共享。
默认不剔除年份，CSV基准的名称默认为“CSV基准”；状态指数和CSV基准可以不同。

```powershell
.\.conda\python.exe -m research.tools.market_regimes --results research/studies/trend/result_v1.csv research/studies/trend/result_v3.csv --benchmark-name 沪深300 --out research/studies/trend/reports/market_regimes
```

行业动量现有CSV的表头已经损坏，显式指定从0开始的列位置即可读取日期/策略/基准：

```powershell
.\.conda\python.exe -m research.tools.market_regimes --results research/studies/industry_momentum/result_l2_etfself.csv --format cumulative-percent --columns '#0' '#2' '#1' --out research/studies/industry_momentum/reports/market_regimes
```

离线运行提供指数CSV（`date,close`），用 `--index-name` 指定报告名称。
指数需要覆盖收益曲线的每个交易日，并额外包含均线预热期和首日之前的收盘。
`--index-symbol` 可改新浪指数代码；`--month-window`、`--week-window` 配置均线；
`--annualization-days` 默认252；`--exclude-years 2021` 是可选收益集中度诊断。
缓存不足时用 `--refresh`，不会静默缩短样本。多个CSV可以有不同的起止日期。

输出 `report.html`、`metadata.json`，以及每条曲线的 `daily.csv`、`episodes.csv`、
`summary.csv`、`year_states.csv`。同名CSV会加父目录名前缀避免混淆。
`pit_pool_retest_result.csv` 一类指标汇总表不是逐日曲线，不能用于本模块。

## 收益输入格式

`auto`只按约定列名识别，不通过数值大小推测单位。基准列均可省略，省略时图表不会制造一条现金基准。

| 格式 | 日期 / 策略 / 可选基准列 | 单位 |
|---|---|---|
| `cumulative-percent` | `时间 / 策略收益 / 基准收益` | 累计百分数，10表示10%，也接受`10%` |
| `cumulative-return` | `date / strategy_cumulative / benchmark_cumulative` | 累计小数，0.1表示10% |
| `nav` | `date / strategy_nav / benchmark_nav` | 净值或资金；用initial参数给定首日前初始值 |
| `daily-return` | `date / strategy_return / benchmark_return` | 单日小数，0.01表示1% |

自定义列用 `--format` 配合 `--columns 日期列 策略列 [基准列]`，可以使用列名或`#N`位置。
净值默认初始值为1；资金输入如10万元用 `--initial-nav 100000`。
策略和基准可用 `--initial-benchmark-nav` 分别指定初始值。CSV需已处理入出金；
本模块不会从资金变化中推测投资收益。首行是首日结束值，首日收益也计入统计。

## Python复用

```python
from pathlib import Path
from research.tools.market_regimes import RegimeConfig, analyze_returns, read_index, read_returns

curve = read_returns(Path("research/studies/trend/result_v1.csv"))
index = read_index(Path("research/tools/market_regimes/_cache/sz399317.csv"))
result = analyze_returns(curve, index, RegimeConfig(month_window=10, week_window=20))
print(result.summary)
```

其他研究已在内存中有DataFrame时，传入约定列名的数据即可；自定义列和单位先用
`normalize_returns(frame, CurveSpec(...))` 转换。返回 `RegimeAnalysis` 的四张表，
计算过程中不修改输入、不取行情、不写文件。网络和报告只在CLI/显式输出调用中发生。

## 状态与统计口径

月强/弱比较上一已结束日历月的收盘与月均线；周强/弱比较上一已结束周的收盘与周均线。
价格等于均线归入“强”。每个交易日的收盘收益，只用当日之前已经结束的周期，
不把当月/当周最终价格回填到此前日期。周按周五结束，整周休市不生成虚拟K线；
月末遇休市时保守等到日历月结束才更新。均线按实际周/月K线数量计算。

累计百分数先转换净值，再用相邻净值比还原日收益。
各状态的对数收益贡献相加须复原原曲线的总收益；状态年化按该状态内交易日折算，
是条件统计，不是执行状态过滤后的择时年化。最大回撤在每个连续段内计算，不拼接
不连续状态的收益曲线来声称可投资回撤；首尾段可能被样本边界截断。
年份表是诊断切片，连续区间的结果也不是各段独立起跑的回测。

状态指数目前使用价格指数，不包含股息再投资；策略自身CSV基准按原导出保留。
默认的国证A指覆盖全市场（剔ST），适合描述A股整体背景；机会集是大中盘的研究可选
中证800，其他市场或资产研究应选择相应状态指数。
