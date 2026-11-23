"""收盘成绩供料：原始价格、指定日期、拒绝缺价/陈旧日期/未来日期替代。"""

import math

from data_provider.bars import listed_close_prices


def holding_closes(codes, asof, fetch):
    """返回指定日期的有效收盘价与缺价持仓，保留持仓顺序。"""
    prices = {code: price for code, price in fetch(codes, asof).items()
              if type(price) in (int, float) and math.isfinite(price) and price > 0}
    return prices, [code for code in codes if code not in prices]


class ClosingPrices:
    """单次估值任务内缓存指定日的收盘价，也记住本次查不到的代码。"""

    def __init__(self):
        self._prices = {}

    def close_prices(self, codes, asof):
        pending = list(dict.fromkeys(code for code in codes if (code, asof) not in self._prices))
        if pending:
            prices = listed_close_prices(pending, asof)
            self._prices.update({(code, asof): prices.get(code) for code in pending})
        return {c: self._prices[c, asof] for c in codes if self._prices.get((c, asof)) is not None}
