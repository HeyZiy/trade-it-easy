# -*- coding: utf-8 -*-
"""
===================================
妙想 — 持仓公共工具
===================================

持仓事实来源已迁至名义成交台账（src/trade_ledger，derive 供料同形 dict，
entry_map 由流水推导）；本模块保留与来源无关的持仓 dict 公共口径：
有效持仓过滤、盈亏百分比兜底。代码前缀判定已上收 data_provider/codes.py
（码族→市场归属唯一权威），此处仅为存活的内部调用方转发。
"""

from typing import List

# is_a_stock_code 本体已上收 codes.py；本模块随死代码清除一并退场。
from data_provider.codes import is_a_stock_code


def filter_held_positions(positions: List[dict], min_count: int = 0) -> List[dict]:
    """过滤有效持仓：代码非空且股数 > min_count。"""
    held: List[dict] = []
    for p in positions or []:
        code = str(p.get("code", "") or "").strip()
        if not code:
            continue
        if int(p.get("count", 0) or 0) <= min_count:
            continue
        held.append(p)
    return held


def filter_stock_positions(positions: List[dict], min_count: int = 0) -> List[dict]:
    """过滤出当前持仓的 A 股股票（排除 ETF/基金/债券等非股票持仓）。

    趋势策略只对股票持仓输出卖出信号；ETF 持仓由 ETF 系统独立管理。
    """
    return [
        p for p in filter_held_positions(positions, min_count)
        if is_a_stock_code(p.get("code", ""))
    ]


def position_profit_pct(p: dict) -> float:
    """持仓盈亏百分比；接口缺失时用成本价/现价兜底计算。"""
    pct = p.get("profit_pct")
    if pct is not None:
        try:
            return float(pct)
        except (TypeError, ValueError):
            pass
    cost = float(p.get("cost_price", 0) or 0)
    price = float(p.get("current_price", 0) or 0)
    if cost > 0 and price > 0:
        return (price / cost - 1) * 100
    return 0.0
