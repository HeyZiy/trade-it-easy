# 研究方法与数据工具

`tools` 提供跨研究复用的函数；具体假设、股票池、信息时点、收益区间、参数和结论放在 `research/studies`。调用计算函数前，由研究脚本准备符合自身时间口径的数据。

| 模块 | 研究用途 | 主要入口 |
|---|---|---|
| [fundamental_analysis](fundamental_analysis/README.md) | 财务、估值、股本及经营指标 | `analyze_fundamentals`、`get_ttm`、`calc_*` |
| [technical_analysis](technical_analysis/README.md) | OHLCV 技术指标时序 | `analyze_technical`、`TechnicalIndicators` |
| [factor_analysis](factor_analysis/README.md) | IC、分组收益、组合因子及诊断报告 | `rank_ic`、`analyze_factor`、`analyze_factors` |
| [announcement_analysis](announcement_analysis/README.md) | 公告筛选、原文下载、PDF 文本提取 | `filter_announcements`、`search_announcements`、`PdfConverter` |
| [market_regimes](market_regimes/README.md) | 市场趋势状态与区间收益归因 | 按该模块的数据与状态口径调用 |
| [etf_data](etf_data/README.md) | ETF 本地行情缓存、复权及数据审计 | `data.py`、`fetch.py` |
| [qdii_premium](qdii_premium/fetch.py) | QDII 净值、价格与双口径溢价缓存 | `python -m research.tools.qdii_premium.fetch --help` |

基本面、技术面、因子和公告模块以 Python 函数调用为主。需要获取数据时显式调用数据接口；计算返回完整时序或结构化结果，保存和展示由调用方决定。数据获取、认证和缓存的公共能力归 [data_provider](../../data_provider/fetchers/amazingdata/README.md)。

这些方法分别回答「数据是否可信」「指标是什么」「指标与后续收益有什么关系」「收益发生在什么环境」。IC、分组收益和状态归因仍属于研究诊断，完整账户收益、费用、现金约束及执行规则需在具体研究中验证。
