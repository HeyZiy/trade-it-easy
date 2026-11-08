# -*- coding: utf-8 -*-
"""从 AmazingData 构建规范化 ETF 研究缓存：python -m research.tools.etf_data.fetch_ad --help。

原始 OHLCV 来自 query_kline，复权来自 get_adj_factor 单次事件乘数按日 cumprod。
派生宽表/日历/名单/manifest 复用东财工具的 build_cache，产出与 LocalEtfData 兼容的
schema-2 缓存，默认落在 research/data/etf_daily_ad/（研究数据规范布局，非 _cache）。
"""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.tools.etf_data.data import PRICE_FIELDS, SCHEMA_VERSION
from research.tools.etf_data.fetch import (AD_SOURCE, atomic_csv, atomic_json,
                                           build_cache, cache_is_fresh, normalize_bars)

DEFAULT_AD_CACHE = ROOT / "research" / "data" / "etf_daily_ad"
BATCH = 200


def _plain(code):
    return str(code).split(".")[0]


def write_benchmark(cache, begin, end):
    from data_provider.fetchers.amazingdata import market
    bench = market.get_benchmark("000300.SH", begin, end)
    if bench is None or bench.empty:
        raise SystemExit("AD 基准获取失败；未更新缓存")
    bench = bench.dropna(subset=["close"]).sort_index()
    frame = pd.DataFrame({"date": bench.index.strftime("%Y-%m-%d"),
                          "close": bench["close"].to_numpy()})
    atomic_csv(frame, cache / "index_000300.csv")
    return frame["date"].max()


def write_universe(cache, codes):
    from data_provider import security_names
    known = security_names.names_for(codes)
    frame = pd.DataFrame({"code": codes, "name": [known.get(c, c) for c in codes]})
    atomic_csv(frame, cache / "etf_universe.csv")
    return int((frame.name != frame.code).sum())


def build_one(code, klines, factors, factor_cols):
    raw = klines.get(code)
    if raw is None or raw.empty:
        raise ValueError("AD 无行情")
    dates = pd.to_datetime(raw["kline_time"], errors="coerce").dt.normalize()
    if dates.isna().any() or dates.duplicated().any():
        raise ValueError("K线日期无效或重复")
    prices = raw[list(PRICE_FIELDS)].apply(pd.to_numeric, errors="coerce")
    prices.index = pd.DatetimeIndex(dates)
    prices = prices.sort_index()
    out = pd.DataFrame({"date": prices.index.strftime("%Y-%m-%d")})
    for field in PRICE_FIELDS:
        out[field] = prices[field].to_numpy()
    for field in ("volume", "amount"):
        out[field] = pd.to_numeric(raw[field], errors="coerce").to_numpy() \
            if field in raw else np.nan
    column = factor_cols.get(code)
    if column is not None:
        factor = factors[column].reindex(prices.index).ffill()
        adjusted = prices.mul(factor.cumprod(), axis=0)
        for field in PRICE_FIELDS:
            out[f"hfq_{field}"] = adjusted[field].to_numpy()
    return normalize_bars(out)


def write_readme(cache, manifest, named):
    text = f"""# ETF 日线研究数据（AmazingData）

来源：AmazingData SDK。原始开高低收/成交量/成交额来自 `query_kline`；后复权由
`get_adj_factor` 单次事件乘数按日累积相乘（cumprod）再乘原始价得到，非价格跳变猜测。
派生层复用 `research/tools/etf_data` 的 `build_cache`，与 `LocalEtfData` 的 schema-2 兼容。

- 数据截止：{manifest['data_as_of']}
- 标的数：{manifest['codes']}（当前名单，仍有幸存者偏差；点时历史名单需另取 get_hist_code_list）
- 完整复权：{manifest['adjusted_codes']}，复权未齐：{manifest['unadjusted_codes']}
- 简称命中：{named}（其余以代码占位，来自本地 security_names 缓存）

## 口径与边界

- `close` 等为原始价，用于成交与持仓核算；`hfq_*` 为事件累积后复权，用于信号。
- `amount` 成交额按接口原始单位（元）用于流动性过滤；若上游单位变更需重新校准门槛。
- 零成交、缺行情、无复权因子分别标 `zero_activity`/`missing`/`adjustment_missing`，
  读取信号时屏蔽为 NaN，不填旧价、不推断真实停牌或权益事件。
- 上市日为首根行情代理；末根行情日不等于退市日。分红现金不建模。

## 重生成

```bash
.conda/python.exe -m research.tools.etf_data.fetch_ad --end-date {manifest['data_as_of']}
```

`--codes` 只更新指定品种，`--limit` 只抓前 N 只，`--refresh` 强制重取，
`--rebuild` 纯本地重建派生文件。已有合格缓存精确覆盖截止日则跳过。
"""
    (cache / "README.md").write_text(text, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_AD_CACHE)
    ap.add_argument("--begin-date", default="2012-01-01")
    ap.add_argument("--end-date", default="2026-09-30")
    ap.add_argument("--codes", nargs="+", help="仅更新指定裸代码")
    ap.add_argument("--limit", type=int, help="仅抓前 N 只")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="纯本地重建派生文件，不联网")
    args = ap.parse_args()
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "etf_daily").mkdir(exist_ok=True)
    if args.rebuild:
        build_cache(cache, args.end_date)
        return

    from data_provider.fetchers.amazingdata import basics, market
    cutoff = write_benchmark(cache, args.begin_date, args.end_date)
    if args.codes:
        codes = list(dict.fromkeys(_plain(c) for c in args.codes))
        named = write_universe(cache, codes)
    else:
        sdk_codes = basics.get_fund_list()
        codes = sorted({_plain(c) for c in sdk_codes})
        named = write_universe(cache, codes)
    if args.limit:
        codes = codes[:args.limit]

    failures = {}
    done = 0
    for start in range(0, len(codes), BATCH):
        batch = codes[start:start + BATCH]
        todo = [c for c in batch if args.refresh
                or not cache_is_fresh(cache / "etf_daily" / f"{c}.csv", cutoff)]
        if not todo:
            done += len(batch)
            continue
        try:
            klines = market.fetch_kline(todo, args.begin_date, cutoff)
            factors = market.fetch_adj_factors(todo, args.begin_date, cutoff)
            factor_cols = {_plain(c): c for c in factors.columns}
        except Exception as exc:
            for c in todo:
                failures[c] = f"{type(exc).__name__}: {str(exc)[:180]}"
            print(f"[batch failed] {batch[0]}..{batch[-1]}: {exc}", flush=True)
            done += len(batch)
            continue
        for code in todo:
            path = cache / "etf_daily" / f"{code}.csv"
            try:
                df = build_one(code, klines, factors, factor_cols)
                if df.empty:
                    raise ValueError("行情为空")
                atomic_csv(df, path)
                atomic_json({"schema_version": SCHEMA_VERSION, "source": AD_SOURCE,
                             "fetched_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                             "requested_end": cutoff, "first_bar": df.date.min(),
                             "last_bar": df.date.max()}, path.with_suffix(".json"))
            except Exception as exc:
                failures[code] = f"{type(exc).__name__}: {str(exc)[:180]}"
                print(f"[failed] {code}: {failures[code]}", flush=True)
        done += len(batch)
        print(f"[fetch] {done}/{len(codes)}，失败 {len(failures)}", flush=True)

    manifest = build_cache(cache, cutoff, failures)
    write_readme(cache, manifest, named)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()