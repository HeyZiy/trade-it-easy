# -*- coding: utf-8 -*-
"""timing 数据准备入口：按本研究的标的配置调用共享取数原语。

取数原语（东财 hfq、乐咕 PE）与 manifest 写入在 research/tools/etf_data/fetch_em.py；
本文件只编排"抓哪些标的"。从仓库根运行：python research/studies/timing/fetch_data.py
"""
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "research.studies.timing"

from .assets import ASSETS
from research.tools.etf_data.fetch_em import (
    ETF_DIR, PE_DIR, fetch_etf_hfq, fetch_pe_akshare, fetch_pe_direct, write_manifests)


def main(assets=ASSETS):
    # 同一指数的多只ETF共享一份PE缓存；冲突配置在请求前报错。
    sources = {}
    for asset in assets:
        if asset.valuation is not None:
            previous = sources.get(asset.index_code)
            if previous is not None and previous != asset.valuation:
                raise ValueError(f"{asset.index_code} 配置了不同的估值取数方式")
            sources[asset.index_code] = asset.valuation
    ETF_DIR.mkdir(parents=True, exist_ok=True)
    PE_DIR.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        fetch_etf_hfq(asset.etf_code)
    for code, source in sources.items():
        if source.method == "akshare":
            fetch_pe_akshare(code, source.symbol)
        else:
            fetch_pe_direct(code, source.symbol)
    write_manifests()


if __name__ == "__main__":
    main()