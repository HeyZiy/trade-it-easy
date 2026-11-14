# 指数滚动市盈率（乐咕）

来源：乐咕 `index-basic-pe` 接口（`ttmPe`，滚动市盈率），月频；
部分标的走 akshare `stock_index_pe_lg`，取数方式见 [assets.py](../../studies/timing/assets.py) 的估值配置。
消费方：`research/studies/timing` 估值规则（全历史分位，月频）。

- 数据截止：2026-10-13
- 指数数：7；跟踪同一指数的多只 ETF 共享一份文件

## Schema

`pe_<index_code>.csv`：`date,pe`，date 为 YYYY-MM-DD 升序，月末频。

## 口径与边界

- PE 为指数口径而非 ETF 口径；分位用全历史，交易日取"最近一期早于该日"，无前视。
- 2026-11-14 自 `research/studies/timing/_cache` 原样迁入，内容未变。

## 重生成

```bash
python research/studies/timing/fetch_data.py
```

抓取后自动刷新 `manifest.json`（built_at、data_as_of、每文件行数与起止日期）。