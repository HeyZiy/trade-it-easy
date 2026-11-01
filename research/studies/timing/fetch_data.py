# -*- coding: utf-8 -*-
"""抓取研究数据并缓存：宽基 ETF 东财后复权日线 + 跟踪指数乐咕 PE（月频）。

共享缓存 research/tools/etf_data 中宽基 ETF 均为 legacy_sina_raw（无复权），
无法计算含分红总回报，故本研究自取 hfq。PE 为乐咕 index-basic-pe 接口
（滚动市盈率 ttmPe）。
从仓库根运行：python research/studies/broad_index_strategies/fetch_data.py
"""
from datetime import datetime
from pathlib import Path

import akshare as ak
import pandas as pd
import py_mini_racer
import requests

from akshare.stock_feature.stock_a_indicator import get_cookie_csrf
from akshare.stock_feature.stock_a_pe_and_pb import hash_code

if __package__:
    from .assets import ASSETS
else:
    from assets import ASSETS

CACHE = Path(__file__).resolve().parent / "_cache"


def fetch_etf_hfq(code):
    raw = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="hfq")
    df = raw.rename(columns={"日期": "date", "开盘": "open", "收盘": "close",
                             "最高": "high", "最低": "low"})[["date", "open", "high", "low", "close"]]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(CACHE / f"etf_hfq_{code}.csv", index=False)
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
    df.to_csv(CACHE / f"pe_{code}.csv", index=False)
    print(f"{code} pe: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {len(df)} 行")


def fetch_pe_akshare(code, symbol):
    df = _pe_frame(ak.stock_index_pe_lg(symbol=symbol), "日期", "滚动市盈率")
    df.to_csv(CACHE / f"pe_{code}.csv", index=False)
    print(f"{code} pe: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}  {len(df)} 行")


def main(assets=ASSETS):
    # 同一指数的多只ETF共享一份PE缓存；冲突配置在请求前报错。
    sources = {}
    for asset in assets:
        if asset.valuation is not None:
            previous = sources.get(asset.index_code)
            if previous is not None and previous != asset.valuation:
                raise ValueError(f"{asset.index_code} 配置了不同的估值取数方式")
            sources[asset.index_code] = asset.valuation
    CACHE.mkdir(exist_ok=True)
    for asset in assets:
        fetch_etf_hfq(asset.etf_code)
    for code, source in sources.items():
        if source.method == "akshare":
            fetch_pe_akshare(code, source.symbol)
        else:
            fetch_pe_direct(code, source.symbol)


if __name__ == "__main__":
    main()
