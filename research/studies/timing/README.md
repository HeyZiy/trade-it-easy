# ETF 择时与长期持有体验

逐只 ETF 比较固定择时规则与买入持有的年化收益、最大回撤、最长水下时间与平均仓位。
规则集与网页择时大表一致：26 条规则行（含依赖 PE 的估值行与依赖成交量的放量突破），
按族展开，参数事先固定，不做寻优。

## 方法与口径

- 标的：9 只 ETF（沪深300、中证500、创业板、中证1000、恒生、标普500、纳指、恒生科技、中证红利）。
- 数据：东财后复权 OHLC；成交量取东财日线，仅用于「放量突破」。
- 窗口：第 272 个有效收盘起，至 2026-09-30。272 是最慢规则「高波动回避」的预热长度
  （RV20 的 20 个日收益 + 252 日分位 - 1）；全部标的、全部规则同起点同窗口。
- 成本：单边 0.06%（佣金 0.01% + 滑点 0.05%），按 100 股取整，现金零利息。
- 执行：收盘确认信号，下一交易日开盘成交；T+1，网格跳空按开盘价改善成交。
- 估值：跟踪指数 PE 全历史分位（乐咕 ttm），分位 ≤20% 满仓、≥80% 清仓，区间维持；
  使用早于交易日的 PE，无前视。
- 网格：以窗口首日开盘价为中枢，几何步长 3%，上下各 10 档，资金 20 等份，期初买 10 份。
- 多起点：每月用相同初始资金新建账户，固定 1/3/5 年持有期；窗口高度重叠，不是独立样本。

## 规则族

| 族 | 规则 |
| --- | --- |
| 基准 | 长拿、半仓长拿、三成长拿 |
| 绝对估值 | PE分位（20/80） |
| 超跌反转 | 5日收益反转 |
| 网格 | 每月重定、每季重定、不重定 |
| 均线交叉 | 双均线 5/20、20/60、60/120 |
| 均线择时 | MA20+ATR×1.5、MA60+ATR×1.5、MA200单线、MA200连续2日确认 |
| 通道突破 | 唐奇安20/10、55/20、55/20+MA200过滤、唐奇安+吊灯 |
| 量价突破 | 放量突破、波动扩张突破 |
| 时序动量 | MOM63、MOM126、MOM252、MOM252-21 |
| 波动状态 | 高波动回避 |

无 PE 缓存的标的只运行价格规则；无成交量时少「放量突破」一行。

## 按标的查看

启动交易工作台后，从「ETF 择时研究」进入 `/research/timing`。每只 ETF 有独立页面，已有报告复用研究结果的原始日期，各报告组各自与同窗口的长拿配对。

工作台在 `data/timing_reports/<代码>.json` 保存网页生成的报告，从 [`data/timing_reports/archive/`](../../../data/timing_reports/archive/) 读取已归档的回测 CSV。两个路径均相对于工作台的数据根目录。「重新生成」尝试在线更新，取数失败时保留已有报告。

研究解释见[研究结论](FINDINGS.md)，数据及成交口径见[共享账户引擎](common/backtest.py)。

## 复现研究

从仓库根目录运行，回测结果写入 `data/timing_reports/archive/`。行情与 PE 输入仍由共享研究缓存提供。

| 需要生成的数据 | 运行入口 |
| --- | --- |
| 行情与 PE 缓存 | `python -m research.studies.timing.fetch_data` |
| 9 只 ETF 的 26 条规则行与 1/3/5 年多起点 | `python -m research.studies.timing.scan` |
| 唐奇安参数族 | `python -m research.studies.timing.common.donchian` |
| ATR 通道参数族 | `python -m research.studies.timing.common.atr_channel` |
| RSRS 族 | `python -m research.studies.timing.common.rsrs` |
| 网格中枢频率对照 | `python -m research.studies.timing.experiments.grid_reanchor` |
| 前置结构诊断（Hurst/方差比/自相关） | `python -m research.studies.timing.experiments.regime_diagnosis` |
| 创业板回撤诊断 | `python -m research.studies.timing.159915.159915` |
| 唐奇安/ATR/RSRS 主组跨标的汇总 | `python -m research.studies.timing.experiments.compare_timing` |

[标的配置](assets.py)定义批量复现的覆盖范围。
