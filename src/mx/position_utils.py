# -*- coding: utf-8 -*-
"""
===================================
妙想 — 持仓公共工具
===================================

持仓事实来源已迁至名义成交台账（src/trade_ledger，derive 供料同形 dict，
entry_map 由流水推导）；本模块保留与来源无关的持仓 dict 公共口径：
代码前缀判定、有效持仓过滤、盈亏百分比兜底。
"""

from typing import List

# A 股股票代码前缀白名单（基金/ETF/债券等非股票前缀不含在内，无重叠）
_A_STOCK_PREFIXES = (
    "600", "601", "603", "605", "688", "689",  # 沪主板 + 科创板
    "000", "001", "002", "003",                # 深主板
    "300", "301",                              # 创业板
    "43", "83", "87", "88", "920",             # 北交所/新三板
)


def is_a_stock_code(code: str) -> bool:
    """判断 6 位代码是否为 A 股股票（按前缀白名单，排除 ETF/基金/债券）。"""
    c = str(code or "").strip().split(".")[0]
    return len(c) == 6 and c.startswith(_A_STOCK_PREFIXES)


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
