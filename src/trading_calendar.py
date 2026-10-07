# -*- coding: utf-8 -*-
"""
===================================
A 股交易日历（公共模块）
===================================

提供交易日判断与区间查询，供各入口脚本（etf_observe / quality_pool 等）复用。

数据来源：akshare `tool_trade_date_hist_sina()`（新浪交易日历）。
设计：
- 模块级缓存，进程内只拉一次
- 拉取失败时按交易日放行（避免节假日误杀，宁可多跑也不漏跑）
"""

import logging
from datetime import date, datetime, timedelta
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_trading_dates: List[date] = []
_loaded = False


def _ensure_loaded() -> None:
    """惰性加载交易日历到模块级缓存（幂等）"""
    global _trading_dates, _loaded
    if _loaded:
        return
    try:
        import akshare as ak
        cal_df = ak.tool_trade_date_hist_sina()
        _trading_dates = sorted(
            pd.to_datetime(row["trade_date"]).date()
            for _, row in cal_df.iterrows()
        )
        _loaded = True
        logger.info(f"交易日历已加载：{len(_trading_dates)} 个交易日")
    except Exception as e:
        logger.error(f"获取交易日历失败（按交易日放行）: {e}")
        _loaded = True  # 标记已尝试，避免重复失败


def get_trading_dates(start_date: date, end_date: date) -> List[date]:
    """返回 [start_date, end_date] 区间内的交易日列表（升序）。"""
    _ensure_loaded()
    if not _trading_dates:
        return []
    return [d for d in _trading_dates if start_date <= d <= end_date]


# 新鲜度断言「预期日」谓词的回看窗：须覆盖最长春节假期（历史 8 天休市 + 周末），30 天足够。
TRADING_DAY_LOOKBACK_DAYS = 30


def latest_trading_day_on_or_before(
    d: date, *, lookback_days: int = TRADING_DAY_LOOKBACK_DAYS,
    fallback: Optional[str] = "weekday",
) -> Optional[date]:
    """把日期收敛到 ≤d 的最近交易日（新鲜度断言「预期日」口径的唯一谓词）。

    日历不可用（拉取失败/区间无数据）时的降级方向由 fallback 决定：
    - "weekday"：按周一~周五回退——数据缺失宁可放行也不误杀（daily 新鲜度断言口径）。
    - None：返回 None，调用方跳过断言（market_gate 指数数据日期断言口径）。
    """
    days = get_trading_dates(d - timedelta(days=lookback_days), d)
    if days:
        return days[-1]
    if fallback is None:
        return None
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trading_days_lag(earlier: date, later: date) -> Optional[int]:
    """earlier 落后 later 几个交易日（同日为 0，跨周末/假期不计数）。

    新鲜度类阈值的计数口径；日历区间查不到时返回 None，降级方向归调用方。
    """
    days = get_trading_dates(earlier, later)
    return len(days) - 1 if days else None


def is_trading_day(day: Optional[date] = None) -> bool:
    """判断某天是否为 A 股交易日（默认今天）。

    Returns:
        True 表示交易日；日历拉取失败时按交易日放行。
    """
    _ensure_loaded()
    if not _trading_dates:
        return True
    return (day or datetime.now().date()) in _trading_dates
