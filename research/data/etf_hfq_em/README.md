# ETF 后复权日线（东财）

来源：akshare `fund_etf_hist_em`（period=daily, adjust=hfq），每标的一个文件。
消费方：`research/studies/timing`（信号与成交均在复权价空间，复权份额模拟）。

- 数据截止：2026-10-13（515180 为 2026-10-15）；研究在 `load_etf` 统一截断至 2026-09-30
- 标的数：12，名单见 [assets.py](../../studies/timing/assets.py)

## Schema

`etf_hfq_<code>.csv`：`date,open,high,low,close`，date 为 YYYY-MM-DD 升序。

## 口径与边界

- 后复权口径由东财提供，未独立核验分红与复权事件。
- 2026-11-14 自 `research/studies/timing/_cache` 原样迁入，内容未变。

## 重生成

```bash
python research/studies/timing/fetch_data.py
```

抓取后自动刷新 `manifest.json`（built_at、data_as_of、每文件行数与起止日期）。