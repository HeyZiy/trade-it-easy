# -*- coding: utf-8 -*-
"""ETF 日线缓存的质量标记、来源校验与派生文件构建。"""
import json

import numpy as np
import pandas as pd

from research.tools.etf_data.data import PRICE_FIELDS, SCHEMA_VERSION

HFQ_FIELDS = [f"hfq_{field}" for field in PRICE_FIELDS]
BAR_FIELDS = (*PRICE_FIELDS, "volume", "amount", *HFQ_FIELDS)
AD_SOURCE = "amazingdata_raw_hfq"
TRUSTED_HFQ_SOURCES = {"eastmoney_raw_hfq", AD_SOURCE}


def atomic_csv(df, path, *, index=False):
    temporary = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(temporary, index=index)
    temporary.replace(path)


def atomic_json(value, path):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def normalize_bars(df):
    """验证原始/后复权两条价格线。异常行保留，quality 供读取层屏蔽。"""
    out = df.reindex(columns=["date", *BAR_FIELDS]).copy()
    out["date"] = pd.to_datetime(out.date, errors="raise").dt.strftime("%Y-%m-%d")
    if out.date.duplicated().any():
        raise ValueError("单只行情有重复日期")
    out = out.sort_values("date").reset_index(drop=True)
    out[list(BAR_FIELDS)] = out[list(BAR_FIELDS)].apply(pd.to_numeric, errors="coerce")
    out[list(BAR_FIELDS)] = out[list(BAR_FIELDS)].replace([np.inf, -np.inf], np.nan)
    prices = out[list(PRICE_FIELDS)]
    valid = prices.notna().all(axis=1) & prices.gt(0).all(axis=1)
    valid &= (out.high >= prices.max(axis=1)) & (out.low <= prices.min(axis=1))
    adjusted = out[HFQ_FIELDS]
    adj_valid = adjusted.notna().all(axis=1) & adjusted.gt(0).all(axis=1)
    adj_valid &= (out.hfq_high >= adjusted.max(axis=1)) & (out.hfq_low <= adjusted.min(axis=1))
    active = (out.volume > 0) & (out.amount > 0)
    out["quality"] = np.select([~valid, ~active, ~adj_valid],
                               ["invalid_price", "zero_activity", "adjustment_missing"], default="ok")
    return out


def _has_trusted_adjustment(meta):
    return (meta.get("schema_version") == SCHEMA_VERSION
            and meta.get("source") in TRUSTED_HFQ_SOURCES)


def cache_is_fresh(path, expected_last):
    """仅当复权来源可信且行情覆盖目标日时跳过更新。"""
    meta_path = path.with_suffix(".json")
    if not path.exists() or not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not _has_trusted_adjustment(meta):
            return False
        df = pd.read_csv(path, usecols=["date", "hfq_close"])
        cutoff = pd.Timestamp(expected_last).strftime("%Y-%m-%d")
        return bool(df.date.max() >= cutoff and df.hfq_close.gt(0).all())
    except (OSError, ValueError, KeyError):
        return False


def build_cache(cache, cutoff=None, failures=None):
    """只重建派生文件。旧猜测复权列忽略；供应商复权必须有来源标记。"""
    securities_path = cache / "etf_securities.csv"
    previous_securities = pd.DataFrame()
    if securities_path.exists():
        previous_securities = pd.read_csv(securities_path, dtype={"code": str}).set_index("code")
    universe_path = cache / "etf_universe.csv"
    universe = pd.DataFrame(columns=["code", "name"])
    if universe_path.exists():
        universe = pd.read_csv(universe_path, dtype={"code": str})
    names = dict(zip(universe.code, universe.name))
    current_codes = set(universe.code)
    if "display_name" in previous_securities:
        names = {**previous_securities.display_name.to_dict(), **names}
    daily = {}
    reports = []
    for path in sorted((cache / "etf_daily").glob("*.csv")):
        raw = pd.read_csv(path)
        meta_path = path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        verified = _has_trusted_adjustment(meta)
        if not verified:
            raw = raw.drop(columns=[c for c in raw if c.startswith("hfq_")], errors="ignore")
        df = normalize_bars(raw)
        if not verified and ("close_qfq" in raw or not set(BAR_FIELDS).issubset(raw.columns)):
            atomic_csv(df, path)  # 保留原始价，删除旧 close_qfq 猜测列。
        if cutoff is not None:
            df = df[df.date <= cutoff]
        if df.empty:
            continue
        code = path.stem
        daily[code] = df.set_index("date")
        reports.append({"code": code, "display_name": names.get(code, code),
                        "start_date": df.date.min(), "last_bar_date": df.date.max(),
                        "n_bars": len(df), "current_listed": code in current_codes,
                        "source": meta.get("source", "legacy_sina_raw"),
                        "adjusted_rows": int(df[HFQ_FIELDS].gt(0).all(axis=1).sum()),
                        **{f"{s}_rows": int((df.quality == s).sum()) for s in
                           ("ok", "zero_activity", "invalid_price", "adjustment_missing")}})
    if not daily:
        raise ValueError("没有可构建的行情，保留既有派生缓存")
    # 基准提供完整交易日历，避免因全体标的缺数据而删掉交易日。
    index_path = cache / "index_000300.csv"
    if not index_path.exists():
        raise FileNotFoundError("缺少 index_000300.csv，先运行联网构建")
    benchmark = pd.read_csv(index_path)
    benchmark = benchmark.dropna(subset=["close"]).sort_values("date")
    end = cutoff or benchmark.date.max()
    if benchmark.date.max() < end:
        raise ValueError(f"基准只覆盖 {benchmark.date.max()}，不足目标日 {end}")
    begin = min(d.index.min() for d in daily.values())
    calendar = pd.Index(benchmark.loc[(benchmark.date >= begin) & (benchmark.date <= end), "date"])
    for code, df in daily.items():
        if len(df.index.difference(calendar)):
            raise ValueError(f"{code} 行情有基准日历之外的日期")
    atomic_csv(pd.DataFrame({"date": calendar}), cache / "calendar.csv")
    for field in (*BAR_FIELDS, "quality"):
        wide = pd.DataFrame({c: df[field] for c, df in daily.items()}).reindex(calendar)
        wide.index.name = "date"
        atomic_csv(wide, cache / f"wide_{field}.csv", index=True)
    sec = pd.DataFrame(reports)
    if "delist_date" in previous_securities:
        sec["delist_date"] = sec.code.map(previous_securities.delist_date)
    sec["missing_last_day"] = sec.last_bar_date < end
    atomic_csv(sec, securities_path)
    atomic_csv(sec, cache / "quality_report.csv")
    previous_path = cache / "manifest.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    recorded_failures = dict(previous.get("failures", {}))
    recorded_failures.update(failures or {})
    for code in list(recorded_failures):
        if cache_is_fresh(cache / "etf_daily" / f"{code}.csv", end):
            del recorded_failures[code]
    manifest = {"schema_version": SCHEMA_VERSION,
                "built_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                "data_as_of": calendar[-1], "benchmark": "000300", "codes": len(daily),
                "price_basis": "供应商后复权按研究截止日归一化；成交价为原始价",
                "adjusted_codes": int((sec.adjusted_rows == sec.n_bars).sum()),
                "unadjusted_codes": int((sec.adjusted_rows != sec.n_bars).sum()),
                "missing_last_day_codes": int(sec.missing_last_day.sum()),
                "failures": recorded_failures,
                "notes": ["历史名单仅含当前或本地曾缓存品种，仍有幸存者偏差",
                          "上市日期为首根行情代理；名称为名单快照，不保证历史名称",
                          "零成交、缺行情、无复权分别标记，未推断真实停牌或权益事件"]}
    atomic_json(manifest, cache / "manifest.json")
    print(f"[build] {len(calendar)} 交易日 × {len(daily)} ETF，"
          f"完整复权 {manifest['adjusted_codes']}，截止 {calendar[-1]}")
    return manifest
