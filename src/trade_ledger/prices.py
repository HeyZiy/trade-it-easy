# -*- coding: utf-8 -*-
"""
===================================
名义影子成交台账 — 读侧现价装配
===================================

ledger.derive 本体保持纯函数零 I/O；本模块补上"持仓代码 → 现价"这一段供料：
股票走多源 failover 日线（data_provider manager），ETF/基金走 get_etf_daily
第二级单源接口。单只取数失败 fail-soft（该代码不进 prices → derive 按 0 计，
与妙想缺价口径一致），不阻断整轮。

snapshot_with_prices 是消费 market_value/pos_pct 的入口（组合敞口、核心再平衡、
卫星轮动）的标准读侧供料；只需 count/entry_map 的消费方（卖侧判定，逐票本来
就过 feeds 拿全量日线）直接用 derive 即可。
"""

import logging
from datetime import date, timedelta
from typing import Dict, Iterable, Optional

from src.mx.position_utils import is_a_stock_code

from .ledger import (DEFAULT_LEDGER_PATH, LedgerSnapshot, derive,
                     load_trades)

logger = logging.getLogger(__name__)

# 股票日线回看窗口：只要最近收盘，留足节假日余量即可
STOCK_LOOKBACK_DAYS = 15


def latest_close(code: str, manager=None) -> Optional[float]:
    """单只最新现价（收盘价口径）；任何失败返回 None，由调用方缺省。"""
    try:
        if is_a_stock_code(code):
            from data_provider import get_fetcher
            fm = manager or get_fetcher()
            end = date.today()
            start = end - timedelta(days=STOCK_LOOKBACK_DAYS)
            df = fm.get_daily_data(code, start.strftime("%Y-%m-%d"),
                                   end.strftime("%Y-%m-%d"))
        else:
            from data_provider.bars import get_etf_daily
            df = get_etf_daily(code)
        if df is None or len(df) == 0:
            return None
        return float(df["close"].iloc[-1])
    except Exception as e:
        logger.error(f"台账现价取数失败 {code}（按 0 计入）: {e}")
        return None


def resolve_prices(codes: Iterable[str], manager=None) -> Dict[str, float]:
    """{code: 现价}，取不到的代码不出现在结果里。"""
    prices: Dict[str, float] = {}
    for code in codes:
        px = latest_close(code, manager)
        if px is not None and px > 0:
            prices[code] = px
    return prices


def snapshot_with_prices(as_of: Optional[str] = None, manager=None,
                         path=DEFAULT_LEDGER_PATH) -> LedgerSnapshot:
    """流水 + 持仓现价 → 定价完成的台账快照（入口标准读侧供料）。"""
    trades = load_trades(path)
    as_of = as_of or date.today().isoformat()
    held = [p["code"] for p in derive(trades, as_of).positions]
    return derive(trades, as_of, resolve_prices(held, manager))
