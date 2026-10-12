"""收盘成绩供料：原始价格、指定日期、拒绝缺价/陈旧日期/未来日期替代。"""

from datetime import date
import logging
import math

import pandas as pd

from data_provider.codes import is_a_stock_code, is_etf_code

logger = logging.getLogger(__name__)


def _at(frame, asof, date_column):
    if frame is None or frame.empty or not {date_column, "close"} <= set(frame.columns):
        return None
    days = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    rows = frame.loc[days == pd.Timestamp(asof)]
    if len(rows) != 1:
        return None
    value = float(pd.to_numeric(rows["close"], errors="coerce").iloc[0])
    return value if math.isfinite(value) and value > 0 else None


class ClosingPrices:
    """一次收盘任务共享行情缓存，两个资金池重叠代码不重复取数。"""

    def __init__(self):
        self._prices = {}
        self._indices = {}

    def close_prices(self, codes, asof):
        missing = [c for c in codes if (c, asof) not in self._prices]
        stocks = [c for c in missing if is_a_stock_code(c)]
        if stocks:
            from data_provider.fetchers.amazingdata_info import fetch_kline
            try:
                frames = fetch_kline(stocks, asof, asof)
                for code in stocks:
                    self._prices[code, asof] = _at(frames.get(code), asof, "kline_time")
            except Exception:
                logger.exception("原始股票收盘数据缺失：%s", asof)
                self._prices.update({(c, asof): None for c in stocks})
        for code in missing:
            if code in stocks:
                continue
            try:
                if not is_etf_code(code):
                    raise ValueError("不是已知股票或ETF代码")
                from data_provider.bars import get_etf_daily
                # 台账股数与未复权价格配对；不把研究用连续价格当作资产市值。
                self._prices[code, asof] = _at(get_etf_daily(code, adjust=None), asof, "date")
            except Exception:
                logger.exception("原始ETF收盘数据缺失：%s %s", code, asof)
                self._prices[code, asof] = None
        return {c: self._prices[c, asof] for c in codes if self._prices.get((c, asof)) is not None}

    def index_prices(self, codes, asof):
        from data_provider.bars import get_index_daily
        for code in codes:
            if (code, asof) not in self._indices:
                try:
                    days = max(30, (date.today() - date.fromisoformat(asof)).days + 10)
                    self._indices[code, asof] = _at(get_index_daily(code, days=days), asof, "date")
                except Exception:
                    logger.exception("基准指数收盘数据缺失：%s %s", code, asof)
                    self._indices[code, asof] = None
        return {c: self._indices[c, asof] for c in codes if self._indices.get((c, asof)) is not None}
