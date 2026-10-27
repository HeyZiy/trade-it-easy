# -*- coding: utf-8 -*-
"""QDII ETF 溢价研究缓存：python -m research.tools.qdii_premium.fetch --help。

数据源（免费、全历史，价格三源回退：东财→新浪→腾讯）：
  close         不复权原始收盘价（元）；volume 统一转成份；amount 元（腾讯源缺为 NaN）
  nav           天天基金 pingzhongdata 官方单位净值（QDII 净值晚间/次晨公布，晚于价格）

溢价双口径（两列并存，消费方按用途选）：
  premium       = close/nav - 1         当日净值口径，复盘统计用（含收盘后美股信息）
  premium_prev  = close/上一可得nav - 1  价格收盘时点真实可见口径（集思录式，信号用）

份额折算边界自动清洗：净值序列先于价格序列切换口径（历史事件：
513500 2022-03 一拆二、513100 2022-01 一拆五）。检测规则=净值单日
|涨跌|>25% 视为折算事件，剔除净值切换日起至价格同幅切换前的错位行
（典型 1~2 行，不处理会出现 +96%/+414% 假溢价）。真实高溢价期
（2016 QDII 紧额度、2020 疫情恐慌）保留不清洗，只在输出里提示复核。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CACHE = Path(__file__).resolve().parent / "_cache"
SCHEMA_VERSION = 1
DEFAULT_CODES = ("513500", "513100")  # 标普500ETF博时 / 纳指ETF国泰
SPLIT_NAV_JUMP = 0.25  # 净值单日跳变阈值，判定份额折算事件
HEADERS = {"User-Agent": "Mozilla/5.0"}


def robust_get(url, *, params=None, headers=None, tries=6):
    """push2his 走系统代理时好时坏：直连/代理交替，递增退避重试。"""
    import requests

    last = None
    for i in range(tries):
        try:
            s = requests.Session()
            s.trust_env = (i % 2 == 1)
            r = s.get(url, params=params, headers=headers or HEADERS, timeout=15)
            if r.status_code == 200:
                return r
            last = f"status {r.status_code}"
        except Exception as exc:
            last = f"{type(exc).__name__}"
        time.sleep(1.5 + i)
    raise RuntimeError(f"{url} 重试 {tries} 次失败: {last}")


def fetch_nav(code):
    """天天基金 pingzhongdata 的 Data_netWorthTrend：官方单位净值全历史。"""
    import re

    r = robust_get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                   headers={**HEADERS, "Referer": f"https://fund.eastmoney.com/{code}.html"})
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", r.text)
    if not m:
        raise ValueError(f"{code} pingzhongdata 无净值序列")
    df = pd.DataFrame(json.loads(m.group(1)))
    df["date"] = pd.to_datetime(df["x"], unit="ms").dt.strftime("%Y-%m-%d")
    return (df[["date", "y"]].rename(columns={"y": "nav"})
            .sort_values("date").reset_index(drop=True))


def _market_sym(code):
    """QDII 码族 → 市场前缀符号（15x 深、51x 沪）。"""
    return ("sz" if code.startswith("15") else "sh") + code


def fetch_close_em(code):
    """东财 push2his 不复权日线（首选，带名称；volume 手→份，amount 元）。https 失败再试 http。"""
    secid = f"0.{code}" if code.startswith("15") else f"1.{code}"
    params = {"secid": secid, "klt": 101, "fqt": 0, "beg": 20130101, "end": 20991231,
              "fields1": "f1,f2,f3,f4,f5,f6",
              "fields2": "f51,f52,f53,f54,f55,f56,f57",
              "ut": "7eea3edcaed734bea9cbfc24409ed989"}
    last = None
    for url in ("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                "http://push2his.eastmoney.com/api/qt/stock/kline/get"):
        try:
            r = robust_get(url, params=params)
            d = r.json()["data"]
            if not d or not d.get("klines"):
                raise ValueError("无行情")
            rows = [k.split(",") for k in d["klines"]]
            df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume", "amount"])
            for col in ("close", "amount"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100  # 手 -> 份
            return d["name"], df[["date", "close", "volume", "amount"]], "eastmoney_push2his_raw"
        except Exception as exc:
            last = exc
    raise RuntimeError(f"东财失败: {last}")


def fetch_close_sina(code):
    """新浪 fund_etf_hist_sina（volume 已是份，amount 元，无名称）。"""
    import akshare as ak

    sym = _market_sym(code)
    for _ in range(4):
        try:
            raw = ak.fund_etf_hist_sina(symbol=sym)
            if raw is not None and not raw.empty:
                df = raw[["date", "close", "volume", "amount"]].copy()
                for col in ("close", "volume", "amount"):
                    df[col] = pd.to_numeric(df.get(col), errors="coerce")
                return None, df, "sina_raw"
        except Exception:
            time.sleep(2)
    raise RuntimeError("新浪连续失败")


def fetch_close_tx(code):
    """腾讯 gtimg 日线（volume 手→份，无 amount；名称另走 qt 接口）。"""
    sym = _market_sym(code)
    r = robust_get("https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
                   params={"param": f"{sym},day,,2099-12-31,10000"})
    d = r.json()["data"][sym]["day"]
    df = pd.DataFrame(d, columns=["date", "open", "close", "high", "low", "volume"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100  # 手 -> 份
    df["amount"] = float("nan")
    name = None
    try:
        q = robust_get(f"https://qt.gtimg.cn/q={sym}", tries=2)
        name = q.text.split("~")[1] or None
    except Exception:
        pass
    return name, df[["date", "close", "volume", "amount"]], "tencent_raw"


def fetch_close(code):
    """价格三源回退链：东财→新浪→腾讯。返回 (名称或 None, df, 源标记)。"""
    for fn in (fetch_close_em, fetch_close_sina, fetch_close_tx):
        try:
            name, df, source = fn(code)
            if df is not None and len(df) and df["close"].gt(0).any():
                return name, df, source
        except Exception as exc:
            print(f"  {fn.__name__}({code}) 失败: {exc}", flush=True)
    raise RuntimeError(f"{code} 价格三源均失败")


def split_boundary_drops(nav, px):
    """份额折算错位行检测。

    返回 (剔除日期集合, 事件记录)。净值单日 |涨跌|>25% 判为折算；
    价格随后同幅跳变日之前为口径错位窗口，剔除该窗口内的合并行
    （净值已切新口径、价格仍旧口径）。找不到价格切换则仅剔事件当日并记录。
    """
    nav_ret = nav.set_index("date")["nav"].pct_change()
    px_ret = px.set_index("date")["close"].pct_change()
    drops, events = [], []
    for day, ret in nav_ret.items():
        if pd.isna(ret) or abs(ret) <= SPLIT_NAV_JUMP:
            continue
        later = px_ret[px_ret.index > day]
        switch = later[later.abs() > SPLIT_NAV_JUMP]
        end = switch.index[0] if len(switch) else None
        window = nav["date"][(nav["date"] >= day) & (nav["date"] < (end or "9999-12-31"))]
        drops.extend(window)
        events.append({"nav_switch": day, "price_switch": end,
                       "nav_jump": round(float(ret), 3), "dropped_rows": list(window)})
    return set(drops), events


def atomic_csv(df, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


def atomic_json(value, path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_one(code, cache, refresh=False):
    path = cache / f"{code}.csv"
    meta_path = path.with_suffix(".json")
    if not refresh and path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") == SCHEMA_VERSION:
            return {"code": code, "status": "fresh", "rows": meta["rows"],
                    "span": f"{meta['first_date']} -> {meta['last_date']}",
                    "source": meta["source"]["price"]}

    name, px, source = fetch_close(code)
    nav = fetch_nav(code)
    # 各源日期类型不一（新浪 date 对象、东财/腾讯字符串），统一归一
    px = px.copy()
    px["date"] = pd.to_datetime(px["date"], errors="raise").dt.strftime("%Y-%m-%d")
    nav["prev_nav"] = nav["nav"].shift(1)  # 上一可得净值（跨假期即上一公布日）
    m = px.merge(nav, on="date", how="inner").sort_values("date").reset_index(drop=True)
    drops, events = split_boundary_drops(nav, px)
    m = m[~m["date"].isin(drops)].reset_index(drop=True)
    m["premium"] = m["close"] / m["nav"] - 1
    m["premium_prev"] = m["close"] / m["prev_nav"] - 1
    m = m[["date", "close", "nav", "premium", "premium_prev", "volume", "amount"]]

    atomic_csv(m, path)
    atomic_json({"schema_version": SCHEMA_VERSION, "code": code, "name": name,
                 "source": {"price": source, "nav": "eastmoney_fund_pingzhongdata"},
                 "fetched_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                 "rows": len(m), "first_date": m.date.min(), "last_date": m.date.max(),
                 "split_events": events,
                 "premium_basis": "premium=close/当日净值(复盘口径)；premium_prev=close/上一可得净值(收盘时点可见，信号口径)",
                 "notes": ["QDII净值公布晚于价格，premium 含收盘后美股信息，做信号用 premium_prev",
                           "2020-03 前后净值披露延迟，两口径溢价噪声均偏大",
                           "份额折算错位行已剔除，见 split_events",
                           "内连接自动截断到净值最后公布日，盘中运行无未收盘价混入",
                           "volume 单位份，amount 元（腾讯源缺 amount）"]},
                meta_path)
    extreme = m[m["premium"].abs() > 0.25]
    summary = {"code": code, "name": name or code, "status": "built", "rows": len(m),
               "source": source,
               "span": f"{m.date.min()} -> {m.date.max()}",
               "premium_pct": {"mean": round(m.premium.mean() * 100, 2),
                               "median": round(m.premium.median() * 100, 2),
                               "p95": round(m.premium.quantile(0.95) * 100, 2),
                               "max": round(m.premium.max() * 100, 2),
                               "latest": round(m.premium.iloc[-1] * 100, 2)},
               "split_events": events,
               "extreme_review": extreme[["date", "premium"]].to_dict("records")}
    return summary


def read_premium(code, cache_dir=None):
    """研究侧读取入口：date 解析为 datetime，溢价以小数存储。"""
    cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE
    df = pd.read_csv(cache / f"{code}.csv", parse_dates=["date"])
    if df.empty:
        raise ValueError(f"{code} 溢价缓存为空")
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--codes", nargs="+", default=DEFAULT_CODES, help="QDII ETF 裸代码")
    ap.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--refresh", action="store_true", help="忽略本地缓存强制重取")
    args = ap.parse_args()
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)

    summaries = [build_one(code, cache, args.refresh) for code in args.codes]
    atomic_json({"schema_version": SCHEMA_VERSION,
                 "built_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
                 "codes": {s["code"]: s.get("span") for s in summaries},
                 "premium_basis": "premium=close/当日净值(复盘)；premium_prev=close/上一可得净值(信号)"},
                cache / "manifest.json")
    for s in summaries:
        if s["status"] == "fresh":
            print(f"[fresh] {s['code']} 已是最新 ({s['rows']} 行)")
            continue
        p = s["premium_pct"]
        print(f"[built] {s['code']} {s['name']} {s['rows']} 行 {s['span']} | "
              f"溢价 mean {p['mean']}% / p95 {p['p95']}% / max {p['max']}% / 最新 {p['latest']}%")
        for ev in s["split_events"]:
            print(f"  份额折算边界剔除 {ev['dropped_rows']} (净值跳变 {ev['nav_jump']})")
        if s["extreme_review"]:
            dates = ", ".join(r["date"] for r in s["extreme_review"])
            print(f"  |premium|>25% 待人工复核（多为真实事件）: {dates}")


if __name__ == "__main__":
    main()
