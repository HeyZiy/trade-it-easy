# AmazingData 数据能力

本目录统一项目运行代码的 AmazingData 会话及公共查询。研究工具保留指标计算、公告筛选、PDF 转换和报告生成。

## 结构与使用

| 模块 | 职责与主要接口 |
|---|---|
| `client.py` | 默认 `client` 管理认证、重试及 BaseData/InfoData/MarketData 对象 |
| `basics.py` | 交易日历、股票/ETF/可转债代码列表、历史代码表、股票基础信息 |
| `market.py` | 分批原始 K 线、价格矩阵、近期及执行快照、单次复权因子及 ETF 后复权行情 |
| `fundamentals.py` | 财务、股本、证券状态、解禁事件，以及历史流通/总市值 |
| `industries.py` | 行业基础表、成员变更记录、指定日期的行业映射与哑变量 |
| `announcements.py` | 股票、基金、可转债的公告列表与原文下载 |
| `_utils.py` | 日期、代码规范化、SDK 返回结构与缓存目录规则 |

同级 `amazingdata_fetcher.py` 的 `AmazingDataFetcher(BaseFetcher)` 适配项目多数据源框架，提供能力声明、优先级、标准日线、实时报价与指数入口。它调用本目录的会话和行情能力。数据能力模块不依赖该适配类。

```python
from data_provider.fetchers.amazingdata import basics, market, fundamentals

codes = basics.get_stock_list()         # SDK 格式，如 000001.SZ
close = market.get_close_price(codes[:10], 20260101, 20261030)
float_value = fundamentals.get_float_market_value(codes[:10], 20260101, 20261030)
```

基础信息的 `get_code_list/get_hist_code_list` 及行情的 `get_raw_kline/get_kline/get_close_price` 接受或返回带交易所后缀的 SDK 代码。`fetch_code_list/fetch_kline` 保留策略使用的 6 位代码接口。指数入口明确区分 `000001.SH` 和股票 `000001.SZ`。

`get_raw_kline` 返回代码到原始表的字典；`get_kline` 返回日期×代码的矩阵，多字段列为 `(field, code)`。这些价格均未复权，原有策略复权由各自调用路径处理。`fetch_etf_hfq_bars` 延续原来的事件乘数累积复权规则。

高级 SDK 查询可以使用 `client.get_base_data()`、`client.get_info_data()`、`client.get_market_data()`；公共查询有复用需求时，加入对应能力模块。

## 统一实时报价

`AmazingDataFetcher.get_realtime_quote(code)` 使用 `market.get_latest_snapshot` 查询沪深股票与 ETF 的当日近期快照。统一实时源默认顺序为 `amazingdata,tencent,akshare_sina,efinance,akshare_em`；显式设置 `REALTIME_SOURCE_PRIORITY` 时以该配置为准。没有完整 AD/TGW 凭证时跳过 AD。

快照通过 `query_snapshot` 查询北京时间最近五分钟，午休以 11:30、收盘后以 15:00 为窗口终点；只接受窗口内且不晚于请求时间的快照，按交易所 `trade_time`（缺列时使用日期时间索引）选最新一笔。该接口提供查询时可获得的近期快照，不是实时订阅；当日无数据、数据过旧、查询异常或最新价无效时，统一路由继续尝试其他来源。

报价使用 `last`，不拿 `close` 或更早一笔替代无效最新价。昨收有效时计算涨跌额、涨跌幅和振幅。文档未明确快照成交量单位，`volume` 保持缺失。缺失估值等字段仍由统一实时路由按原规则补充，补充源不覆盖 AD 价格。网页刷新通过该接口更新展示估值，报价不写入账户流水或收盘收益曲线。

## 配置与会话

项目根目录的 `.env` 在客户端加载时读取。优先使用整组 `TGW_USERNAME/TGW_PASSWORD/TGW_HOST/TGW_PORT`；完全没有 `TGW_*` 配置时，兼容旧工具的完整 `AD_USERNAME/AD_PASSWORD/AD_HOST/AD_PORT`。两组字段不混用。

导入模块及构造客户端不联网。首次请求建立会话，默认客户端在同一进程内共享连接；连接初始化失败会按配置重试，最终失败后本进程后续请求抛出 `DataFetchError`。策略暂停或多源回退仍由上层决定。研究工具各自启动进程时，各进程有自己的会话。

持久缓存使用 `AMAZING_DATA_DIR`；相对路径按项目根目录解析，默认 `data/AmazingData_local_data/`。需要 SDK 强制落盘的行业和历史代码表在线查询使用独立临时子目录，成功或异常均清理；共享缓存不参与清理。公告原文可用工具的 `--local-path` 单独指定存储位置。

## 历史数据口径

- `fetch_code_list(asof=...)` 只查询指定日期的历史名单。
- 财务字段与公告日保留给策略装配；各接口的日期含义分别处理，维持原有查询窗口与字段裁剪。
- 历史市值使用未复权收盘价×当时股本×10000，按股本变动日对齐。有公告日时同时限制可用时间；缺失或非正股本保留 NaN，流通股本不以总股本替代。SDK 未提供公告日或历史修订版本时，不据此宣称完整的历史可见性。
- 行业原始成员表保留纳入/剔除日期；研究用行业矩阵要求显式 `asof`。未知归属保留缺失，归属冲突不返回。

## 官方实现的归属

| 官方来源 | 项目归属 |
|---|---|
| 因子分析 `DataProvider` 的日历、代码、K 线及基准接口 | `basics`、`market` |
| 因子分析的市值、行业映射和哑变量 | `fundamentals`、`industries`；修正历史对齐与子集缓存问题 |
| 公告工具 `DataProvider` 的登录、SDK 对象与代码表 | `client`、`basics`；运行工具中的重复 provider 已移除 |
| 项目原 `amazingdata_info.py` 的批量接口 | 按职责进入 `basics`、`market`、`fundamentals`、`industries` |
| 技术面、基本面工具的单独登录 | 改为共享 `client`，分析与展示留在原工具 |

官方 MCP 另外覆盖期货、期权、两融、龙虎榜、大宗交易、ETF 申赎和份额、指数权重、国债收益率及可转债条款等。本目录尚未为这些能力逐项建立查询接口；扩展时可查阅 [官方开发手册](../../../docs/ad_api/AmazingData开发手册.md) 和 [MCP 使用说明](../../../docs/ad_api/MCP服务器说明文档.md)。

本项目不保留独立的 MCP 服务脚本。公共数据模块使用 Python 数据结构并保持独立于 FastMCP；MCP 注册、返回值序列化和服务启动属于单独的服务适配职责。
