# ETF 择时与长期持有体验

逐只 ETF 比较固定择时规则与买入持有的年化收益、最大回撤、最长水下时间与平均仓位。

## 按标的查看

启动交易工作台后，从「ETF 择时研究」进入 `/research/timing`。每只 ETF 有独立页面，已有报告复用研究结果的原始日期，各报告组各自与同窗口的长拿配对。

工作台在 `data/timing_reports/<代码>.json` 保存网页生成的报告，从 [`data/timing_reports/archive/`](../../../data/timing_reports/archive/) 读取已归档的回测 CSV。两个路径均相对于工作台的数据根目录。「重新生成」尝试在线更新，取数失败时保留已有报告。

研究解释见[研究结论](FINDINGS.md)，数据及成交口径见[共享账户引擎](common/backtest.py)。

## 复现研究

从仓库根目录运行，回测结果写入 `data/timing_reports/archive/`。行情与 PE 输入仍由共享研究缓存提供。

| 需要生成的数据 | 运行入口 |
| --- | --- |
| 行情与 PE 缓存 | `python -m research.studies.timing.fetch_data` |
| 配置标的的代表规则 | `python -m research.studies.timing.scan` |
| 恒生科技的固定参数组 | `python -m research.studies.timing.513180.513180` |
| 唐奇安参数族 | `python -m research.studies.timing.common.donchian` |
| ATR 通道参数族 | `python -m research.studies.timing.common.atr_channel` |
| 网格中枢频率对照 | `python -m research.studies.timing.experiments.grid_reanchor` |
| 创业板回撤诊断 | `python -m research.studies.timing.159915.159915` |
| Cycle 单 ETF 择时对照 | `python -m research.studies.timing.experiments.cycle_timing` |

[标的配置](assets.py)定义批量复现的覆盖范围。
