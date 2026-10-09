# -*- coding: utf-8 -*-
"""ETF 公共研究缓存：python -m research.tools.etf_data.fetch --help。"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.tools.etf_data.data import DEFAULT_CACHE, PRICE_FIELDS, SCHEMA_VERSION

BAR_FIELDS = (*PRICE_FIELDS, "volume", "amount", *(f"hfq_{f}" for f in PRICE_FIELDS))
SOURCE = "eastmoney_raw_hfq"


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
    adjusted = out[[f"hfq_{f}" for f in PRICE_FIELDS]]
    adj_valid = adjusted.notna().all(axis=1) & adjusted.gt(0).all(axis=1)
    adj_valid &= (out.hfq_high >= adjusted.max(axis=1)) & (out.hfq_low <= adjusted.min(axis=1))
    active = (out.volume > 0) & (out.amount > 0)
    out["quality"] = np.select([~valid, ~active, ~adj_valid],
                               ["invalid_price", "zero_activity", "adjustment_missing"], default="ok")
    return out


def cache_is_fresh(path, expected_last):
    """只有供应商后复权缓存且精确覆盖目标日，才能跳过更新。"""
    meta_path = path.with_suffix(".json")
    if not path.exists() or not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != SCHEMA_VERSION or meta.get("source") != SOURCE:
            return False
        df = pd.read_csv(path, usecols=["date", "hfq_close"])
        cutoff = pd.Timestamp(expected_last).strftime("%Y-%m-%d")
        return bool(df.date.max() >= cutoff and df.hfq_close.gt(0).all())
    except (OSError, ValueError, KeyError):
        return False


def fetch_one(code, cache, cutoff, refresh=False):
    from data_provider.bars import get_etf_research_daily

    path = cache / "etf_daily" / f"{code}.csv"
    if not refresh and cache_is_fresh(path, cutoff):
        return None
    df = normalize_bars(get_etf_research_daily(code, cutoff))
    if df.empty:
        raise ValueError("行情为空")
    atomic_csv(df, path)
    atomic_json({"schema_version": SCHEMA_VERSION, "source": SOURCE,
                 "fetched_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                 "requested_end": cutoff, "first_bar": df.date.min(), "last_bar": df.date.max()},
                path.with_suffix(".json"))
    return df.date.max()


def build_cache(cache, cutoff=None, failures=None):
    """只重建派生文件。旧猜测复权列忽略；供应商复权必须有来源标记。"""
    old_path = cache / "etf_securities.csv"
    old = pd.read_csv(old_path, dtype={"code": str}).set_index("code") if old_path.exists() else pd.DataFrame()
    uni_path = cache / "etf_universe.csv"
    uni = pd.read_csv(uni_path, dtype={"code": str}) if uni_path.exists() else pd.DataFrame(columns=["code", "name"])
    names = dict(zip(uni.code, uni.name))
    current_codes = set(uni.code)
    if "display_name" in old:
        names = {**old.display_name.to_dict(), **names}
    daily = {}
    reports = []
    for path in sorted((cache / "etf_daily").glob("*.csv")):
        raw = pd.read_csv(path)
        meta_path = path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        verified = meta.get("source") == SOURCE and meta.get("schema_version") == SCHEMA_VERSION
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
                        "adjusted_rows": int(df[[f"hfq_{f}" for f in PRICE_FIELDS]].gt(0).all(axis=1).sum()),
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
    if "delist_date" in old:
        sec["delist_date"] = sec.code.map(old.delist_date)
    sec["missing_last_day"] = sec.last_bar_date < end
    atomic_csv(sec, cache / "etf_securities.csv")
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--end-date", help="固定数据截止日；默认最近已完成日线")
    ap.add_argument("--codes", nargs="+", help="仅更新指定裸代码，保留其他缓存")
    ap.add_argument("--limit", type=int, help="仅抓前 N 只，manifest 如实记录复权覆盖")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="纯本地迁移/重建，不联网")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=0.2)
    args = ap.parse_args()
    if args.workers < 1 or args.sleep < 0 or (args.limit is not None and args.limit < 1):
        ap.error("workers/limit 必须为正，sleep 不得为负")
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "etf_daily").mkdir(exist_ok=True)
    cutoff = pd.Timestamp(args.end_date).strftime("%Y-%m-%d") if args.end_date else None
    if args.rebuild:
        build_cache(cache, cutoff)
        return
    from data_provider.bars import get_etf_universe, get_index_daily
    now = pd.Timestamp.now(tz="Asia/Shanghai")
    completed = (now.normalize() - pd.Timedelta(days=int(now.hour < 16))).strftime("%Y-%m-%d")
    if cutoff is not None and cutoff > completed:
        ap.error("截止日尚未完成日线，不能把盘中行情作为完整日线")
    benchmark = get_index_daily("000300", days=4000)
    if benchmark is None or benchmark.empty:
        raise SystemExit("基准获取失败；未更新缓存，已有数据可用 --rebuild")
    benchmark = benchmark[benchmark.date <= (cutoff or completed)]
    if benchmark.empty:
        raise SystemExit("目标区间没有基准交易日")
    cutoff = benchmark.date.max()
    atomic_csv(benchmark, cache / "index_000300.csv")
    uni = get_etf_universe()
    if uni is None or uni.empty:
        raise SystemExit("名单获取失败；未沿用过期名单假装更新成功")
    atomic_csv(uni, cache / "etf_universe.csv")
    codes = list(dict.fromkeys(args.codes if args.codes else sorted(uni.code.tolist())))
    if args.limit:
        codes = codes[:args.limit]
    from requests.exceptions import ConnectionError, Timeout

    failures = {}
    def update(code):
        error = None
        for attempt in range(2):
            try:
                result = fetch_one(code, cache, cutoff, args.refresh)
                time.sleep(args.sleep)
                return result
            except Exception as exc:
                error = exc
                time.sleep(1 + attempt)
        raise error
    completed_codes = set()
    network_streak = 0
    stopped = False
    remaining = iter(codes)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            jobs = {}
            for _ in range(args.workers):
                code = next(remaining, None)
                if code is not None:
                    jobs[pool.submit(update, code)] = code
            while jobs:
                done, _ = wait(jobs, return_when=FIRST_COMPLETED)
                for job in done:
                    code = jobs.pop(job)
                    try:
                        job.result()
                        network_streak = 0
                    except Exception as exc:
                        network_streak = network_streak + 1 if isinstance(exc, (ConnectionError, Timeout)) else 0
                        failures[code] = f"{type(exc).__name__}: {str(exc)[:180]}"
                        print(f"[failed] {code}: {failures[code]}", flush=True)
                    completed_codes.add(code)
                    if network_streak >= 10 and not stopped:
                        stopped = True
                        print("[stop] 连续10只连接失败，停止新请求，保留已有数据。", flush=True)
                while not stopped and len(jobs) < args.workers:
                    code = next(remaining, None)
                    if code is None:
                        break
                    jobs[pool.submit(update, code)] = code
                i = len(completed_codes)
                if i % 50 == 0 or not jobs:
                    print(f"[fetch] {i}/{len(codes)}，失败 {len(failures)}", flush=True)
    except KeyboardInterrupt:
        stopped = True
        print("[stop] 更新中断；重建已成功下载的数据。", flush=True)
    if stopped:
        for code in set(codes) - completed_codes:
            if not cache_is_fresh(cache / "etf_daily" / f"{code}.csv", cutoff):
                failures[code] = "未完成：更新中断或连续连接失败"
    build_cache(cache, cutoff, failures)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
