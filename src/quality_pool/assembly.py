"""实际账户持仓、预算与实时行情组成的执行检查快照。"""

import logging
from datetime import date

import pandas as pd

import src.trading_calendar as trading_calendar
from data_provider.security_names import names_for
from src.quality_pool import execution, feeds

logger = logging.getLogger(__name__)


def build_snapshot(exec_date: str, selected: list[str], *, account_facts) -> execution.ExecSnapshot:
    """按实际持仓和候选取价，交易状态缺失时暂停对应建议。"""
    positions = account_facts["positions"]
    counts = {p["code"]: p["count"] for p in positions}
    avail = {p["code"]: p["avail_count"] for p in positions}
    cash, equity = account_facts["cash"], account_facts["budget"]
    codes = sorted(counts.keys() | set(selected))
    prices = feeds.fetch_realtime_prices(codes)

    prev_day = trading_calendar.previous_trading_day(date.fromisoformat(exec_date))
    prev_close = feeds.raw_closes_at(codes, prev_day.isoformat())
    status = feeds.fetch_execution_status(codes, exec_date)
    limits = feeds.day_limits(codes, exec_date, prev_close, status=status)

    suspended: dict = {}
    st: dict = {}
    unavailable = set()
    for code in codes:
        if (code in status.index and all(col in status.columns
                and pd.notna(status.at[code, col]) for col in ('is_st', 'is_suspended'))):
            suspended[code] = bool(status.at[code, "is_suspended"])
            st[code] = bool(status.at[code, "is_st"])
        else:
            unavailable.add(code)
            suspended[code] = True
            st[code] = True
            logger.warning(f"[quality_pool] 状态表缺 {code}，按停牌/ST 处置")
        if code not in limits:
            unavailable.add(code)

    return execution.ExecSnapshot(
        trade_date=exec_date, counts=counts, avail=avail, cash=cash,
        equity=equity, prices=prices, names=names_for(codes),
        suspended=suspended, st=st, limits=limits, unavailable=unavailable)
