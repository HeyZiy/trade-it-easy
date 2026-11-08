# ETF 日线研究数据（AmazingData）

来源：AmazingData SDK。原始开高低收/成交量/成交额来自 `query_kline`；后复权由
`get_adj_factor` 单次事件乘数按日累积相乘（cumprod）再乘原始价得到，非价格跳变猜测。
派生层复用 `research/tools/etf_data` 的 `build_cache`，与 `LocalEtfData` 的 schema-2 兼容。

- 数据截止：2026-09-30
- 标的数：1653（当前名单，仍有幸存者偏差；点时历史名单需另取 get_hist_code_list）
- 完整复权：1637，复权未齐：16
- 简称命中：61（其余以代码占位，来自本地 security_names 缓存）

## 口径与边界

- `close` 等为原始价，用于成交与持仓核算；`hfq_*` 为事件累积后复权，用于信号。
- `amount` 成交额按接口原始单位（元）用于流动性过滤；若上游单位变更需重新校准门槛。
- 零成交、缺行情、无复权因子分别标 `zero_activity`/`missing`/`adjustment_missing`，
  读取信号时屏蔽为 NaN，不填旧价、不推断真实停牌或权益事件。
- 上市日为首根行情代理；末根行情日不等于退市日。分红现金不建模。

## 重生成

```bash
.conda/python.exe -m research.tools.etf_data.fetch_ad --end-date 2026-09-30
```

`--codes` 只更新指定品种，`--limit` 只抓前 N 只，`--refresh` 强制重取，
`--rebuild` 纯本地重建派生文件。已有合格缓存精确覆盖截止日则跳过。
