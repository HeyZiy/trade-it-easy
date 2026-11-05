# -*- coding: utf-8 -*-
"""共享账户引擎：统一成本、窗口与成交口径，供一级、二级和实验复用。

统一口径：
- 数据：本研究 _cache/ 东财后复权 OHLC；信号与成交均在复权价空间，
  属于复权份额模拟，尚未独立核验上游复权与分红口径。
- 成本：单边 0.06%（佣金 0.01% + 滑点 0.05%），按 100 股取整（复权价口径）。
- 窗口：各 ETF 从"MA200 首次可得"之日起至缓存截止日（2026-09-30），
  同一标的的所有规则同起点同窗口，保证可比。
- 节奏：趋势月末出信号、次日开盘执行；估值使用早于交易日的 PE。
- 网格：T+1，跳空按开盘价成交；主结果假设 O-L-H-C 路径，O-H-L-C 作对照。

模拟器与信号（参数事先固定，不做寻优）：
- sim_buyhold：期初全仓买入持有。
- sim_grid：以窗口首日开盘价为中枢，几何步长 3%，上下各 10 档；资金 20 等份，
  期初买 10 份（等量份额）；跌破下一档买 1 份、涨破上一档卖 1 份，越界躺平。
  固定日内路径不依赖当日收盘涨跌；当日买入不增加可卖份额。
- sim_target：按目标仓位 0/1 执行，配 build_trend_target（月末收盘 > MA200 满仓，
  否则清仓）与 build_val_target（跟踪指数 PE 全历史分位，月频，乐咕 ttm，
  分位<=30% 满仓、>=70% 清仓、区间维持；交易日用"最近一期 PE 早于该日"的口径，
  无前视；至少 24 期历史）。
"""
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from .. import assets as asset_config
from . import experience

ASSETS = asset_config.ASSETS
STUDY = Path(__file__).resolve().parents[1]
PE_CACHE = STUDY / "_cache"

COST = 0.0006          # 单边：佣金0.01% + 滑点0.05%
LOT = 100
CAPITAL = 1_000_000.0
ANN = 244              # A股年均交易日
GRID_STEP = 0.03
GRID_N = 10            # 上下各10档
VAL_LO, VAL_HI = 0.30, 0.70
VAL_MIN_POINTS = 24


# ---------------------------------------------------------------- 数据加载
def load_etf(code):
    """东财后复权 OHLC，DatetimeIndex 升序；统一截至 2026-09-30。"""
    df = pd.read_csv(PE_CACHE / f"etf_hfq_{code}.csv", index_col="date")
    df.index = pd.to_datetime(df.index)
    return df.sort_index().loc[:"2026-09-30"]


def load_pe(code):
    df = pd.read_csv(PE_CACHE / f"pe_{code}.csv", index_col="date")
    s = df["pe"]
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def window_start(px):
    """MA200 首次可得的首个交易日。"""
    ready = px["close"].rolling(200, min_periods=200).count().ge(200)
    if not ready.any():
        raise ValueError("行情不足 200 个有效收盘价，无法确定回测窗口")
    return ready.idxmax()


def record_trade(ledger, date, side, price, quantity, cash, shares, **details):
    if ledger is not None and quantity > 0:
        ledger.append({"date": date, "side": side, "price": price,
                       "quantity": quantity, "fee": quantity * price * COST,
                       "cash_after": cash, "shares_after": shares, **details})


# ---------------------------------------------------------------- 策略实现
def sim_buyhold(px, ledger=None):
    n = len(px)
    nav = np.full(n, np.nan)
    expo = np.zeros(n)
    cash_hist, shares_hist = np.zeros(n), np.zeros(n)
    trades = 0
    cash = CAPITAL
    shares = 0
    o_arr = px["open"].to_numpy()
    c_arr = px["close"].to_numpy()
    for i in range(n):
        o, c = o_arr[i], c_arr[i]
        if shares == 0 and not np.isnan(o):
            shares = int(cash / (o * (1 + COST)) // LOT) * LOT
            cash -= shares * o * (1 + COST)
            if shares > 0:
                trades += 1
                record_trade(ledger, px.index[i], "buy", o, shares, cash, shares,
                             phase="initial")
        if not np.isnan(c):
            nav[i] = cash + shares * c
            if nav[i] > 0:
                expo[i] = shares * c / nav[i]
        cash_hist[i], shares_hist[i] = cash, shares
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


def sim_grid(px, step=GRID_STEP, n=GRID_N, reanchor=True, reanchor_every=1,
             intraday_path="low_first", ledger=None):
    """reanchor=True：每隔 reanchor_every 个月以昨日收盘为新中枢重建网格
    （份额与现金不变），日线不能确定真实路径；low_first=O-L-H-C，high_first=O-H-L-C。
    两种路径是场景假设，不是收益上下界。跳空订单按开盘价改善成交。
    卖出只能消耗昨日库存，期初 n 份合并为一笔买单。
    """
    if n <= 0 or step <= 0 or intraday_path not in ("low_first", "high_first"):
        raise ValueError("网格档数、步长必须为正，日内路径须为 low_first/high_first")
    if reanchor and reanchor_every < 1:
        raise ValueError("reanchor_every 必须为正整数月数")
    nrow = len(px)
    nav = np.full(nrow, np.nan)
    expo = np.zeros(nrow)
    cash_hist, shares_hist = np.zeros(nrow), np.zeros(nrow)
    trades = 0
    cash = CAPITAL
    s0 = 0.0
    units = 0
    last_idx = 0
    levels = None
    cur_month = None
    o_arr = px["open"].to_numpy()
    h_arr = px["high"].to_numpy()
    l_arr = px["low"].to_numpy()
    c_arr = px["close"].to_numpy()
    idx_arr = px.index
    for i in range(nrow):
        o, h, l, c = o_arr[i], h_arr[i], l_arr[i], c_arr[i]
        sellable = units  # 当日新买份额不可卖
        if reanchor and levels is not None \
                and (idx_arr[i].to_period("M").ordinal - cur_month.ordinal) >= reanchor_every \
                and not np.isnan(c_arr[i - 1]):
            # 新月份：以昨日收盘为新中枢，last_idx 归零（份额与现金不动）
            cur_month = idx_arr[i].to_period("M")
            levels = {k: c_arr[i - 1] * (1 + step) ** k for k in range(-n, n + 1)}
            last_idx = 0
        valid_bar = np.isfinite([o, h, l, c]).all() and min(o, h, l, c) > 0
        if valid_bar:
            if not (l <= min(o, c) <= max(o, c) <= h):
                raise ValueError(f"{idx_arr[i]} OHLC 高低价范围不合法")
            if levels is None:
                levels = {k: o * (1 + step) ** k for k in range(-n, n + 1)}
                cur_month = idx_arr[i].to_period("M")
                s0 = float(int((CAPITAL / (2 * n)) / (o * (1 + COST)) // LOT) * LOT)
                if s0 <= 0:
                    raise ValueError("资金不足以买入一档网格份额")
                units = n
                cash -= units * s0 * o * (1 + COST)
                trades += 1
                record_trade(ledger, idx_arr[i], "buy", o, units * s0, cash,
                             units * s0, phase="initial", grid_level=0,
                             sellable_after=0, intraday_path=intraday_path)

            def fill(is_buy, price, level, phase):
                nonlocal cash, units, last_idx, trades, sellable
                if is_buy:
                    cash -= s0 * price * (1 + COST)
                    units += 1
                    last_idx -= 1
                else:
                    cash += s0 * price * (1 - COST)
                    units -= 1
                    sellable -= 1
                    last_idx += 1
                trades += 1
                record_trade(ledger, idx_arr[i], "buy" if is_buy else "sell",
                             price, s0, cash, units * s0, phase=phase,
                             grid_level=level, sellable_after=sellable * s0,
                             intraday_path=intraday_path)

            # 开盘时已越过的挂单，成交价改善为开盘价，而不是不可达的格价。
            for is_buy in (True, False):
                while True:
                    k = last_idx - 1 if is_buy else last_idx + 1
                    if not -n <= k <= n:
                        break
                    level = levels[k]
                    if is_buy:
                        if units >= 2 * n or o > level or cash < s0 * o * (1 + COST):
                            break
                    elif units <= 0 or sellable <= 0 or o < level:
                        break
                    fill(is_buy, o, level, "open_gap")

            points = (o, l, h, c) if intraday_path == "low_first" else (o, h, l, c)
            for segment, (start, end) in enumerate(zip(points, points[1:])):
                if start == end:
                    continue
                is_buy = end < start
                while True:
                    k = last_idx - 1 if is_buy else last_idx + 1
                    if not -n <= k <= n:
                        break
                    level = levels[k]
                    if not min(start, end) <= level <= max(start, end):
                        break
                    if is_buy:
                        if units >= 2 * n or cash < s0 * level * (1 + COST):
                            break
                    elif units <= 0 or sellable <= 0:
                        break
                    fill(is_buy, level, level,
                         "close_segment" if segment == 2 else f"segment_{segment + 1}")
        if not np.isnan(c):
            nav[i] = cash + units * s0 * c
            if nav[i] > 0:
                expo[i] = (nav[i] - cash) / nav[i]
        cash_hist[i], shares_hist[i] = cash, units * s0
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


def sim_target(px, target, ledger=None):
    """target：{日期: 0/1}，目标在当日开盘执行（信号生成严格早于执行时点）。"""
    n = len(px)
    nav = np.full(n, np.nan)
    expo = np.zeros(n)
    cash_hist, shares_hist = np.zeros(n), np.zeros(n)
    trades = 0
    cash = CAPITAL
    shares = 0
    cur = 0.0
    pending = None
    o_arr = px["open"].to_numpy()
    c_arr = px["close"].to_numpy()
    for i, t in enumerate(px.index):
        o, c = o_arr[i], c_arr[i]
        if t in target:
            pending = target[t]
        want = pending
        if want is not None and want != cur and not np.isnan(o):
            if want > cur:  # 买至满仓
                shares = int(cash / (o * (1 + COST)) // LOT) * LOT
                cash -= shares * o * (1 + COST)
                if shares > 0:
                    trades += 1
                    record_trade(ledger, t, "buy", o, shares, cash, shares, phase="target")
            elif shares > 0:  # 清仓
                quantity = shares
                cash += shares * o * (1 - COST)
                shares = 0
                trades += 1
                record_trade(ledger, t, "sell", o, quantity, cash, shares, phase="target")
            cur = want
            pending = None
        if not np.isnan(c):
            nav[i] = cash + shares * c
            if nav[i] > 0:
                expo[i] = shares * c / nav[i]
        cash_hist[i], shares_hist[i] = cash, shares
    return pd.DataFrame({"nav": nav, "exposure": expo, "cash": cash_hist,
                         "shares": shares_hist}, index=px.index), trades


# ---------------------------------------------------------------- 目标序列
def month_end_dates(px):
    return px.index.to_series().groupby(px.index.to_period("M")).max()


def build_trend_target(px, window=200, start=None):
    ma = px["close"].rolling(window, min_periods=window).mean()
    valid = px["close"].notna() & ma.notna()
    sig = pd.Series(np.where(valid, px["close"] > ma, np.nan), index=px.index)
    target = {}
    for t in month_end_dates(px):
        v = sig.loc[t]
        if not np.isnan(v):
            loc = px.index.get_loc(t)
            if loc + 1 < len(px):
                execution = px.index[loc + 1]
                if start is None or execution >= start:
                    target[execution] = 1.0 if v else 0.0
    return target


def build_val_target(px, pe, changes_only=True):
    """全历史分位；对交易日 t 用最近一期早于 t 的 PE，目标当日开盘执行（无前视）。"""
    target = {}
    cur = None
    for i, t in enumerate(px.index):
        avail = pe.loc[pe.index < t]
        if len(avail) < VAL_MIN_POINTS:
            continue
        pct = (avail <= avail.iloc[-1]).mean()
        want = 1.0 if pct <= VAL_LO else (0.0 if pct >= VAL_HI else None)
        if want is not None and (not changes_only or want != cur):
            target[t] = want
            cur = want
    return target


# ---------------------------------------------------------------- 指标
def metrics(nav, expo, trades):
    holding = experience.drawdown_metrics(nav, CAPITAL)
    previous = nav.shift(1)
    previous.iloc[0] = CAPITAL
    ret = nav / previous - 1
    cagr = (nav.iloc[-1] / CAPITAL) ** (ANN / len(nav)) - 1
    vol = ret.std() * np.sqrt(ANN)
    dd = (nav / nav.cummax().clip(lower=CAPITAL) - 1).min()
    month_nav = nav.resample("ME").last()
    monthly = month_nav.pct_change()
    monthly.iloc[0] = month_nav.iloc[0] / CAPITAL - 1
    monthly = monthly.dropna()
    return {
        "年化收益%": cagr * 100,
        "年化波动%": vol * 100,
        "Sharpe": ret.mean() / vol * ANN if vol > 0 else np.nan,
        "最大回撤%": dd * 100,
        "Calmar": cagr / abs(dd) if dd < 0 else np.nan,
        "平均仓位%": expo.loc[nav.index].mean() * 100,
        "交易次数": trades,
        "年均成交笔数": trades * ANN / len(nav),
        "月胜率%": (monthly > 0).mean() * 100,
        "期末终值(万)": nav.iloc[-1] / 1e4,
        **holding,
    }


ROUND_TRIP_COLS = ["完整回合数", "回合胜率%", "平均盈利%", "平均亏损%", "盈亏比", "期末未平批次"]
QTY_EPS = 1e-9


def round_trip_stats(ledger):
    """按 FIFO 把每一笔卖单配到最早买入且尚未卖出的份额，收益含双边成本。

    长拿只有期初一笔买入，没有已平回合；网格的回合是库存份额层面的配对，
    不等于按档位一买一卖的收割。
    """
    lots = deque()
    returns = []
    for record in ledger:
        price, quantity = float(record["price"]), float(record["quantity"])
        if record["side"] == "buy":
            lots.append([price, quantity])
            continue
        while quantity > QTY_EPS and lots:
            matched = min(quantity, lots[0][1])
            returns.append(price * (1 - COST) / (lots[0][0] * (1 + COST)) - 1)
            lots[0][1] -= matched
            quantity -= matched
            if lots[0][1] <= QTY_EPS:
                lots.popleft()
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
        "期末未平批次": len(lots),
    }


def attach_round_trips(met, ledgers):
    rows = {name: round_trip_stats(ledgers.get(name, [])) for name in met.index}
    return met.join(pd.DataFrame.from_dict(rows, orient="index")[ROUND_TRIP_COLS])


def annual_returns(navs):
    rows = {}
    for name, nav in navs.items():
        nav = nav.dropna()
        ye = nav.resample("YE").last()
        r = ye.pct_change()
        r.iloc[0] = ye.iloc[0] / CAPITAL - 1
        rows[name] = r
    df = pd.DataFrame(rows)
    df.index = df.index.year
    return df
