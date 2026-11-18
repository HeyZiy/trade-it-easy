"""收盘成绩供料：原始价格、指定日期、拒绝缺价/陈旧日期/未来日期替代。"""

from datetime import date
import logging
import math

import pandas as pd

from data_provider.codes import is_a_stock_code, is_etf_code, normalize_stock_code
from data_provider.types import safe_float

logger = logging.getLogger(__name__)


def holding_closes(codes, asof, fetch):
    """返回指定日期的有效收盘价与缺价持仓，保留持仓顺序。"""
    prices = {code: price for code, price in fetch(codes, asof).items()
              if type(price) in (int, float) and math.isfinite(price) and price > 0}
    return prices, [code for code in codes if code not in prices]


def _at(frame, asof, date_column):
    if frame is None or frame.empty or not {date_column, "close"} <= set(frame.columns):
        return None
    days = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    rows = frame.loc[days == pd.Timestamp(asof)]
    if len(rows) != 1:
        return None
    value = safe_float(rows["close"].iloc[0])
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _etf_fallback_close(code, asof):
    from data_provider.bars import get_etf_daily, get_etf_daily_em

    try:
        price = _at(get_etf_daily_em(code, asof), asof, "date")
    except Exception:
        price = None
    if price is not None:
        return price
    try:
        return _at(get_etf_daily(code), asof, "date")
    except Exception:
        logger.exception("原始ETF收盘数据缺失：%s %s", code, asof)
        return None


class ClosingPrices:
    """股票和 ETF 优先走 AmazingData 批量日线，ETF 缺价时依次走东财、新浪。"""

    def __init__(self):
        self._prices = {}
        self._indices = {}

    def close_prices(self, codes, asof):
        pending = {}
        etfs = set()
        for code in codes:
            if (code, asof) in self._prices or code in pending:
                continue
            if is_etf_code(code):
                etfs.add(code)
            elif not is_a_stock_code(code):
                logger.error("未识别的股票或 ETF 代码：%s %s", code, asof)
                self._prices[code, asof] = None
                continue
            pending[code] = normalize_stock_code(code)

        frames = {}
        if pending:
            from data_provider.fetchers.amazingdata.market import fetch_kline
            try:
                frames = fetch_kline(list(dict.fromkeys(pending.values())), asof, asof)
            except Exception:
                logger.exception("AmazingData 批量收盘数据查询失败：%s", asof)

        for code, bare_code in pending.items():
            price = _at(frames.get(bare_code), asof, "kline_time")
            if price is None and code in etfs:
                price = _etf_fallback_close(code, asof)
            self._prices[code, asof] = price
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
