# ETF 本地研究数据

从仓库根目录运行，使用项目解释器 `.conda/python.exe`。
工具只负责行情、复权、缓存和质量标记；选池、去重、评分和交易规则放在各研究中。

## 获取与更新

```powershell
.conda/python.exe -m research.tools.etf_data.fetch_ad --end-date 2026-09-30
```

默认缓存位于 `research/data/etf_daily_ad/`。原始行情和复权因子来自 AmazingData，
字段及复权口径见 [AD 缓存说明](../../data/etf_daily_ad/README.md)。已有合格缓存覆盖目标日时跳过下载。
下载失败会记录失败代码并以非零状态退出。`--codes 512690 159516` 仅更新指定品种的行情，
`--refresh` 强制重取，`--rebuild` 仅重建本地派生文件。`--cache-dir` 可指定独立快照目录。
构建期间不要同时读取同一缓存目录。

## 东财 hfq 与乐咕 PE

`fetch_em.py` 提供东财 `fund_etf_hist_em` 后复权日线与乐咕指数 PE 的逐标的取数原语，
写入 `research/data/etf_hfq_em/` 与 `research/data/index_pe_lg/`，`write_manifests()` 刷新元信息。
抓哪些标的、估值路由由研究入口编排：`python research/studies/timing/fetch_data.py`。

## 腾讯备用快照

腾讯快照使用独立目录：

```powershell
.conda/python.exe -m research.tools.etf_data.fetch_tencent --codes 515880 159611 --start-date 2015-10-01 --end-date 2026-09-30 --cache-dir research/studies/etf_timing/_cache/pool37_tencent_20260930 --direct --allow-day-response
```

`fetch_tencent.py` 分段获取腾讯公开接口的原始OHLC与后复权请求结果，保存每段日期、端点、响应字段及文件SHA256，支持断点重取。备用 `newfqkline` 返回截至终点的最多640条，只有确定覆盖请求起点时才裁去多余早期记录，未覆盖则拒绝截断数据。`--history-floor-file` 只利用已有首根行情日期跳过上市前空请求，不参与选池。

部分ETF的 `hfq` 请求仍返回 `day` 字段；默认拒绝，显式启用 `--allow-day-response` 时保留并记录 `hfq_day_response_rows`，不将其伪装为明确的 `hfqday` 响应。该标记及供应商后复权字段仍需与平台或权益事件验证，不能仅凭请求参数宣称复权已完全正确。缓存命中也遵守该开关。

此接口没有提供成交额，`amount` 保留NaN，不能用于成交额过滤。成交量由手换算为份；`quality=ok` 只表示数值、OHLC范围及活动量检查通过，不表示已验证平台复权等价。腾讯快照应保存在独立目录。

2026-10-19已获取ETF轮动37只池、73466条日线，全部末日为2026-09-30。核验确认腾讯部分 `hfq` 序列存在加法偏移，非除权日的价格比例也不等同于原始价；该快照仅保留作原始价来源，标记 `usable_for_returns=false`，不能直接用其 `hfq_close` 算收益或评分。

### 乘法复权与聚宽核验

`event_adjustment.py` 读取明确的分红/折算记录构建价格，不按价格跳变猜事件：现金分红以除息前收盘计算乘法因子，公开份额折算登记日在下一交易日作用于交易价格。SDK授权区间可获取精确折算因子时，保留公开比例并使用SDK精确价格乘数与生效日，避免比例舍入和日期错位。

```powershell
.conda/python.exe -m research.tools.etf_data.event_adjustment --source-cache research/studies/etf_timing/_cache/pool37_tencent_20260930 --output research/studies/etf_timing/_cache/pool37_event_adjusted_20260930 --jq-market research/studies/etf_rotation/fit_market.csv --jq-sdk-market research/studies/etf_timing/_cache/pool37_joinquant_20250711_20260718/market.csv
```

本次SDK账号只允许2025-07-11至2026-07-18，已合法取得实际交易日2025-07-11至2026-07-17的37只原始/后复权行情共9139行；与已有平台导出3705行重叠，差异仅为旧导出的三位小数舍入。修正后37只均通过该区间核验，日收益差最大约0.000396个百分点。

有效研究快照在 `pool37_event_adjusted_20260930/market.csv`，共73311行：原15只直接保留完整聚宽导出，新22只使用原始价及显式权益事件。`close` 为原始价，`event_close` 为乘法复权价格，因子起点常数不影响区间收益；`source` 明确逐行来源。`equity_events/` 保留事件及出处，manifest保留生效日、精确因子来源和核验误差。新增行业早期历史依据公开事件，逐值同源核验只覆盖SDK授权区间，不声称全十年均通过SDK验证。停牌/缺价仍需下游明确排除，不填旧价。

## 读取 AD 缓存

```python
from research.tools.etf_data.data import LocalEtfData

data = LocalEtfData()
day = "2026-09-30"
codes = ["512690", "159516"]

# 默认排除截止日，与聚宽日线历史窗口的边界一致。
past = data.history(25, "close", codes, end_date=day)
# 收盘研究明确选择包含当日；该信号应在之后的交易时点执行。
signal_prices = data.history(26, "close", codes, end_date=day, include_end=True)
money = data.history(20, "money", codes, end_date=day)
liquidity = money.mean(skipna=False)  # 不把缺额压缩成更短的20日均额。
raw_trade_price = data.last_price("512690", day)
status = data.data_status("159516", day)
```

两个窗口访问器都默认不含截止日，`include_end=True` 明确纳入。
`attribute_history(..., skip_missing=True)` 先跳过缺行再取 count 根，
只是本地缺行处理，不承诺与平台停牌字段等价。

价格 `adjust` 可选：

| 值 | 含义 |
| --- | --- |
| `pre`（默认） | 供应商后复权 OHLC × 截止日原始收盘 / 截止日后复权收盘 |
| `hfq` | 供应商连续后复权价格 |
| `raw` | 原始价格，用于成交和持仓核算 |

`pre` 按每次研究截止日归一化，历史窗口与当日原始价同一量纲，
不会把未来截止日作为窗口基准。它保留后复权的价格比值，
不承诺与行情软件“今天的前复权快照”或聚宽收益逐值相同。
`last_price` 默认返回原始价；其他价格访问默认使用复权信号价。
原始价持仓核算仍需分红、折算的权益事件，工具尚未提供真实权益事件台账；
不能从后复权价格直接推断现金分红或持仓份额变化。

## 数据质量与范围

`data_status` 区分 `ok`、`adjustment_missing`、`zero_activity`、`invalid_price`、`missing`。
异常行保留在单只文件中；读取信号时屏蔽为 NaN。
有价格但成交量或金额缺失/非正数，保守标为 `zero_activity`，不推断真实停牌。
`is_tradable` 只是数据侧可交易代理；缺行情不会补零或自动填旧价。
未知代码返回 NaN / `missing`，不会因构造空序列崩溃。

`etf_securities.csv` 保存首根行情代理的上市日、末根行情日和来源；
末根行情日不等于退市日。只有真实 `delist_date` 提供时才据此过滤。
`calendar.csv` 来自基准实际交易日，`quality_report.csv` 记录各品种质量和末日覆盖，
`manifest.json` 记录数据版本、截止日、完整复权品种数和失败代码。
同时列出复权未齐和末日未覆盖的品种数；纯本地重建不会抹掉尚未解决的获取失败。
仅当前/曾缓存标的以及当前名称的限制仍影响长期研究，不能据此宣称无幸存者偏差。
