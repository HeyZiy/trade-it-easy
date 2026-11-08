# -*- coding: utf-8 -*-
"""本地日线账户引擎：现金/持仓/成交台账与净值指标，策略无关。

口径与 timing 研究共享引擎一致的部分：100 份整手、净值=现金+持仓市值、
年化交易日 ANN=244、FIFO 回合配对。成本默认照 reversal_v1：单边万一佣金、
最低 5 元、绝对价格滑点 0.002（买加卖减），无印花税。

成交在传入的原始价上加减滑点；信号空间（复权）与成交空间（原始）由调用方分离。
T+1 由 entry_date 表达：当日买入的份额当日不可卖。分红现金不建模。
"""
from collections import deque

import numpy as np
import pandas as pd

ANN = 244
LOT = 100
QTY_EPS = 1e-9


class Account:
    def __init__(self, capital, commission=0.0001, min_commission=5.0,
                 slippage=0.002, lot=LOT):
        self.capital = float(capital)
        self.cash = float(capital)
        self.commission = commission
        self.min_commission = min_commission
        self.slippage = slippage
        self.lot = lot
        self.positions = {}
        self.ledger = []

    def _fee(self, shares, fill):
        return max(shares * fill * self.commission, self.min_commission)

    def buy(self, date, code, shares, price):
        shares = int(shares)
        if shares <= 0 or not np.isfinite(price) or price <= 0:
            return False
        fill = price + self.slippage
        fee = self._fee(shares, fill)
        cost = shares * fill + fee
        if cost > self.cash + 1e-6:
            return False
        self.cash -= cost
        pos = self.positions.get(code)
        if pos is None:
            pos = {"shares": 0, "entry_date": str(date)}
            self.positions[code] = pos
        pos["shares"] += shares
        self.ledger.append({"date": str(date), "side": "buy", "code": code,
                            "price": fill, "quantity": shares, "fee": fee,
                            "cash_after": self.cash})
        return True

    def sell(self, date, code, shares, price):
        pos = self.positions.get(code)
        shares = int(shares)
        if pos is None or shares <= 0 or shares > pos["shares"]:
            return False
        if not np.isfinite(price) or price <= 0:
            return False
        fill = max(price - self.slippage, 0.0)
        fee = self._fee(shares, fill)
        self.cash += shares * fill - fee
        pos["shares"] -= shares
        if pos["shares"] <= QTY_EPS:
            del self.positions[code]
        self.ledger.append({"date": str(date), "side": "sell", "code": code,
                            "price": fill, "quantity": shares, "fee": fee,
                            "cash_after": self.cash})
        return True

    def closeable(self, code, today):
        pos = self.positions.get(code)
        if pos is None or pos["entry_date"] >= str(today):
            return 0
        return pos["shares"]

    def shares(self, code):
        pos = self.positions.get(code)
        return pos["shares"] if pos else 0

    def exposure(self, prices):
        return sum(p["shares"] * float(prices.get(c, 0.0))
                   for c, p in self.positions.items())

    def value(self, prices):
        return self.cash + self.exposure(prices)


def metrics(nav, exposure, trades, capital, ann=ANN):
    nav = nav.dropna()
    previous = nav.shift(1)
    previous.iloc[0] = capital
    ret = nav / previous - 1
    cagr = (nav.iloc[-1] / capital) ** (ann / len(nav)) - 1
    vol = ret.std() * np.sqrt(ann)
    dd = (nav / nav.cummax().clip(lower=capital) - 1).min()
    month_nav = nav.resample("ME").last()
    monthly = month_nav.pct_change()
    monthly.iloc[0] = month_nav.iloc[0] / capital - 1
    monthly = monthly.dropna()
    return {
        "年化收益%": cagr * 100,
        "年化波动%": vol * 100,
        "Sharpe": ret.mean() / vol * ann if vol > 0 else np.nan,
        "最大回撤%": dd * 100,
        "Calmar": cagr / abs(dd) if dd < 0 else np.nan,
        "平均仓位%": exposure.reindex(nav.index).mean() * 100,
        "交易次数": trades,
        "年均成交笔数": trades * ann / len(nav),
        "月胜率%": (monthly > 0).mean() * 100,
        "期末终值(万)": nav.iloc[-1] / 1e4,
    }


def annual_returns(nav, capital):
    nav = nav.dropna()
    ye = nav.resample("YE").last()
    r = ye.pct_change()
    r.iloc[0] = ye.iloc[0] / capital - 1
    r.index = r.index.year
    return r


ROUND_TRIP_COLS = ["完整回合数", "回合胜率%", "平均盈利%", "平均亏损%", "盈亏比", "期末未平批次"]


def round_trip_stats(ledger):
    lots = {}
    returns = []
    open_batches = 0
    for record in ledger:
        code = record["code"]
        price, quantity = float(record["price"]), float(record["quantity"])
        fee = float(record["fee"])
        if record["side"] == "buy":
            unit = (price * quantity + fee) / quantity
            lots.setdefault(code, deque()).append([unit, quantity])
            continue
        queue = lots.get(code, deque())
        unit_net = (price * quantity - fee) / quantity
        while quantity > QTY_EPS and queue:
            matched = min(quantity, queue[0][1])
            returns.append(unit_net / queue[0][0] - 1)
            queue[0][1] -= matched
            quantity -= matched
            if queue[0][1] <= QTY_EPS:
                queue.popleft()
    open_batches = sum(len(q) for q in lots.values())
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    avg_win = float(np.mean(wins)) * 100 if wins else np.nan
    avg_loss = float(np.mean(losses)) * 100 if losses else np.nan
    return {
        "完整回合数": len(returns),
        "回合胜率%": float(np.mean([r > 0 for r in returns])) * 100 if returns else np.nan,
        "平均盈利%": avg_win,
        "平均亏损%": avg_loss,
        "盈亏比": avg_win / abs(avg_loss) if wins and losses else np.nan,
        "期末未平批次": open_batches,
    }