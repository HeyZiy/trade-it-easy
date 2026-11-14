# -*- coding: utf-8 -*-
"""东财后复权日线 + 乐咕指数 PE 的取数原语。

存放于 research/data/etf_hfq_em/ 与 research/data/index_pe_lg/，
write_manifests 在抓取后刷新数据集元信息。
共享缓存 research/tools/etf_data 中宽基 ETF 均为 legacy_sina_raw（无复权），
无法计算含分红总回报，故另取 hfq。PE 为乐咕 index-basic-pe 接口
（滚动市盈率 ttmPe）。
抓哪些标的、估值路由由研究入口编排，如 research/studies/timing/fetch_data.py。
"""
import json
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd
import py_mini_racer
import requests

from akshare.stock_feature.stock_a_indicator import get_cookie_csrf
from akshare.stock_feature.stock_a_pe_and_pb import hash_code

DATA = Path(__file__).resolve().parents[2] / "data"
ETF_DIR = DATA / "etf_hfq_em"
PE_DIR = DATA / "index_pe_lg"


def fetch_etf_hfq(code):
    raw = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="hfq")
    df = raw.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                             "最高": "high", "最低": "low"})[["date", "open", "high", "low", "close"]]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(ETF_DIR / f"etf_hfq_{code}.csv", index=False)
    print(f"{code} hfq: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {len(df)} 行")


def _legulegu_token():
    js = py_mini_racer.MiniRacer()
    js.eval(hash_code)
    return js.call("hex", datetime.now().date().isoformat()).lower()


def _pe_frame(raw, date_col, pe_col):
    df = pd.DataFrame({
        "date": pd.to_datetime(raw[date_col]).dt.strftime("%Y-%m-%d"),
        "pe": pd.to_numeric(raw[pe_col], errors="coerce"),
    }).dropna().sort_values("date").drop_duplicates("date")
    return df


def fetch_pe_direct(code, index_code):
    token = _legulegu_token()
    url = "https://legulegu.com/api/stockdata/index-basic-pe"
    r = requests.get(url, params={"token": token, "indexCode": index_code},
                     **get_cookie_csrf(url="https://legulegu.com/stockdata/sz50-ttm-lyr"),
                     timeout=30)
    r.raise_for_status()
    df = _pe_frame(pd.DataFrame(r.json()["data"]), "date", "ttmPe")
    df.to_csv(PE_DIR / f"pe_{code}.csv", index=False)
    print(f"{code} pe: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {len(df)} 行")


def fetch_pe_akshare(code, symbol):
    df = _pe_frame(ak.stock_index_pe_lg(symbol=symbol), "日期", "滚动市盈率")
    df.to_csv(PE_DIR / f"pe_{code}.csv", index=False)
    print(f"{code} pe: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {len(df)} 行")


def write_manifests():
    for directory, prefix, source, basis in (
            (ETF_DIR, "etf_hfq", "东财 fund_etf_hist_em 日线 hfq（akshare）", "后复权 OHLC"),
            (PE_DIR, "pe", "乐咕 index-basic-pe ttmPe", "指数滚动市盈率（月频）")):
        files = {}
        for path in sorted(directory.glob(f"{prefix}_*.csv")):
            df = pd.read_csv(path)
            files[path.name] = {"rows": int(len(df)),
                                "start": df["date"].iloc[0], "end": df["date"].iloc[-1]}
        manifest = {"built_at": datetime.now().astimezone().isoformat(),
                    "data_as_of": max(f["end"] for f in files.values()),
                    "source": source, "basis": basis, "files": files}
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")