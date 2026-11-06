"""场外基金已公布的单位净值。"""

from datetime import date
import math


def latest_fund_nav(code: str, through: str) -> tuple[str, float] | None:
    """取截止日期内最新的有效单位净值。"""
    import efinance as ef

    from data_provider.fetchers.efinance_fetcher import _ef_call_with_timeout

    frame = _ef_call_with_timeout(ef.fund.get_quote_history, code, pz=10, timeout=8)
    latest = None
    for raw_day, raw_nav in frame[["日期", "单位净值"]].itertuples(index=False, name=None):
        try:
            day = date.fromisoformat(str(raw_day)).isoformat()
            nav = float(raw_nav)
        except (TypeError, ValueError):
            continue
        if day <= through and math.isfinite(nav) and 0 < nav <= 1e8 and round(nav, 8) == nav:
            if latest is None or day > latest[0]:
                latest = (day, nav)
    return latest
