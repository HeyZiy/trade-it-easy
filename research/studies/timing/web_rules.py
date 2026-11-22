# -*- coding: utf-8 -*-
"""网页择时大表的规则集：机制覆盖、单变量对照、用户声明估值。

只复用共享账户引擎（backtest）与通道/ATR 纯函数，不改动研究一级/二级配置。
信号一律收盘确认、次日开盘执行；后复权价空间；0/1 满仓空仓（基准三行除外）。
全期窗口从最慢规则预热完成起算：RV20 的 252 日分位需 272 根有效收盘。
估值行由用户声明指数代码、网页现取乐咕 PE 后启用，不依赖研究标的清单。
"""
from functools import partial

import numpy as np
import pandas as pd

from .common import backtest as base
from .common import donchian as dc
from .common import atr_channel as atr
from .common import experience

WARMUP = base.WARMUP   # 272 = 21（RV20 需 20 个日收益）+ 252（分位回看）- 1
PE_LO, PE_HI = 0.20, 0.80

FAMILY = {
    "长拿": "基准", "半仓长拿": "基准", "三成长拿": "基准",
    "PE分位": "绝对估值",
    "5日收益反转": "超跌反转",
    "网格·每月重定": "网格", "网格·每季重定": "网格", "网格·不重定": "网格",
    "双均线5/20": "均线交叉", "双均线20/60": "均线交叉", "双均线60/120": "均线交叉",
    "MA20+ATR×1.5": "均线择时", "MA60+ATR×1.5": "均线择时",
    "MA200单线": "均线择时", "MA200连续2日确认": "均线择时",
    "唐奇安20/10": "通道突破", "唐奇安55/20": "通道突破",
    "唐奇安55/20+MA200过滤": "通道突破", "唐奇安+吊灯": "通道突破",
    "放量突破": "量价突破", "波动扩张突破": "量价突破",
    "MOM63": "时序动量", "MOM126": "时序动量", "MOM252": "时序动量", "MOM252-21": "时序动量",
    "高波动回避": "波动状态",
}
# 默认曲线与多起点 rolling 仅覆盖这 6 条代表规则。
STAR = ("长拿", "半仓长拿", "网格·每月重定", "MA20+ATR×1.5", "唐奇安20/10", "MOM252")


def index_symbol(index_code):
    """6 位指数代码 → 乐咕 indexCode（实测：000→.SH，399→.SZ，930→.CSI）。"""
    if index_code.startswith("399"):
        return index_code + ".SZ"
    if index_code.startswith("000"):
        return index_code + ".SH"
    if index_code.startswith("930"):
        return index_code + ".CSI"
    raise ValueError("暂不支持该指数代码段（支持 000/399/930 开头）。")


def window_start(history):
    """最慢规则（RV20 的 252 日分位）预热完成的首个交易日。"""
    return base.window_start(history)


def _exec(history, signal):
    """收盘信号序列（0/1/NaN）→ {次日开盘执行日: 目标}，复用通道族的执行口径。"""
    execution = pd.Series(history.index, index=history.index).shift(-1)
    return dc.targets_from_signals(pd.DataFrame({"target": signal, "execution_date": execution}))


# ---------------------------------------------------------------- 目标生成器
def dual_ma_target(history, fast, slow):
    mf = history.close.rolling(fast, min_periods=fast).mean()
    ms = history.close.rolling(slow, min_periods=slow).mean()
    valid = mf.notna() & ms.notna()
    return _exec(history, (mf > ms).astype(float).where(valid))


def ma_confirm_target(history, window=200, days=2):
    """连续 days 日收盘高于均线买、连续 days 日低于卖，其间维持（时间缓冲）。"""
    ma = history.close.rolling(window, min_periods=window).mean()
    valid = ma.notna() & history.close.notna()
    above = ((history.close > ma) & valid).astype(int)
    below = ((history.close < ma) & valid).astype(int)
    buy = above.rolling(days, min_periods=days).min().eq(1)
    sell = below.rolling(days, min_periods=days).min().eq(1)
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[buy] = 1.0
    signal.loc[sell] = 0.0
    return _exec(history, signal.ffill().fillna(0.0))


def mom_target(history, window, skip=0):
    """月频时序动量：区间收益＞0 满仓否则空仓；skip 剔除最近 skip 日（MOM252-21）。"""
    ret = history.close.shift(skip) / history.close.shift(window) - 1
    target = {}
    for t in base.month_end_dates(history):
        v = ret.loc[t]
        if pd.notna(v):
            loc = history.index.get_loc(t)
            if loc + 1 < len(history):
                target[history.index[loc + 1]] = 1.0 if v > 0 else 0.0
    return target


def rv_regime_target(history, rv_window=20, lookback=252, lo=0.60, hi=0.80):
    """RV20 低于过去 lookback 日 lo 分位买、高于 hi 分位卖，中间维持（高波动回避）。"""
    rv = history.close.pct_change().rolling(rv_window, min_periods=rv_window).std()
    pct = rv.rolling(lookback, min_periods=lookback).apply(lambda x: (x <= x[-1]).mean(), raw=True)
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[pct < lo] = 1.0
    signal.loc[pct > hi] = 0.0
    return _exec(history, signal.ffill().fillna(0.0))


def reversal_target(history, threshold=-0.05, hold=5):
    """过去 5 日累计收益＜threshold 买满仓，持有 hold 个交易日到期卖；持仓中忽略新信号。"""
    ret = (history.close / history.close.shift(5) - 1).to_numpy()
    idx = history.index
    n = len(history)
    target = {}
    i = 0
    while i < n - 1:
        if np.isfinite(ret[i]) and ret[i] < threshold:
            buy = i + 1
            sell = buy + hold
            target[idx[buy]] = 1.0
            if sell >= n:
                break  # 到期日超出样本，持有到结束
            target[idx[sell]] = 0.0
            i = sell
        else:
            i += 1
    return target


def chandelier_target(history, entry=55, mult=3.0, atr_period=20):
    """突破前 entry 日最高买；持仓期最高价（high）− mult×ATR  trailing 退出。"""
    upper = history.high.rolling(entry, min_periods=entry).max().shift(1)
    a = atr.average_true_range(history, atr_period, "wilder")
    close, high = history.close.to_numpy(), history.high.to_numpy()
    up, av = upper.to_numpy(), a.to_numpy()
    idx = history.index
    n = len(history)
    target = {}
    state, peak = 0, -np.inf
    for i in range(n):
        if state == 1:
            if np.isfinite(high[i]):
                peak = max(peak, high[i])
            if np.isfinite(av[i]) and np.isfinite(close[i]) and close[i] < peak - mult * av[i]:
                if i + 1 < n:
                    target[idx[i + 1]] = 0.0
                state, peak = 0, -np.inf
        elif np.isfinite(up[i]) and np.isfinite(close[i]) and close[i] > up[i] and i + 1 < n:
            target[idx[i + 1]] = 1.0
            state, peak = 1, -np.inf  # 次日起持仓，最高价从入场日算起
    return target


def donchian_ma_filter_target(history, entry=55, exit=20, ma=200):
    """唐奇安 entry/exit 入场加 P＞MA(ma) 过滤，退出不变（单变量对照 55/20）。"""
    signals = dc.channel_signals(history, entry, exit, "ohlc")
    ma_line = history.close.rolling(ma, min_periods=ma).mean()
    keep_entry = signals["target"].ne(1.0) | (history.close > ma_line)
    return dc.targets_from_signals(signals.assign(target=signals["target"].where(keep_entry)))


def volume_breakout_target(history, entry=20, exit=10, vol_mult=1.5, vol_window=20):
    """突破前 entry 日高且成交量＞前 vol_window 日均量×vol_mult 买；跌破前 exit 日低卖。"""
    if "volume" not in history.columns:
        raise ValueError("缺少成交量列，无法生成放量突破")
    upper = history.high.rolling(entry, min_periods=entry).max().shift(1)
    lower = history.low.rolling(exit, min_periods=exit).min().shift(1)
    volma = history.volume.rolling(vol_window, min_periods=vol_window).mean().shift(1)
    valid = history.close.notna() & upper.notna() & lower.notna()
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[valid & (history.close > upper) & (history.volume > volma * vol_mult)] = 1.0
    signal.loc[valid & (history.close < lower)] = 0.0
    return _exec(history, signal)


def vol_expansion_target(history, entry=20, exit=10, atr_period=20):
    """突破前 entry 日高且 ATR20 上升（当日＞前一日）买；跌破前 exit 日低卖。"""
    upper = history.high.rolling(entry, min_periods=entry).max().shift(1)
    lower = history.low.rolling(exit, min_periods=exit).min().shift(1)
    a = atr.average_true_range(history, atr_period, "wilder")
    valid = history.close.notna() & upper.notna() & lower.notna() & a.notna()
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[valid & (history.close > upper) & (a > a.shift(1))] = 1.0
    signal.loc[valid & (history.close < lower)] = 0.0
    return _exec(history, signal)


# ---------------------------------------------------------------- 组装
def strategy_runners(history, pe=None):
    """网页大表 runner（插入序即展示序）；PE 行需 pe，放量突破需成交量列。"""
    runners = {
        "长拿": base.sim_buyhold,
        "半仓长拿": partial(base.sim_buyhold, weight=0.5),
        "三成长拿": partial(base.sim_buyhold, weight=0.3),
    }
    if pe is not None:
        runners["PE分位"] = partial(base.sim_target, target=base.build_val_target(
            history, pe, changes_only=False, lo=PE_LO, hi=PE_HI))
    runners["5日收益反转"] = partial(base.sim_target, target=reversal_target(history))
    runners["网格·每月重定"] = base.sim_grid
    runners["网格·每季重定"] = partial(base.sim_grid, reanchor_every=3)
    runners["网格·不重定"] = partial(base.sim_grid, reanchor=False)
    for name, (fast, slow) in {"双均线5/20": (5, 20), "双均线20/60": (20, 60),
                               "双均线60/120": (60, 120)}.items():
        runners[name] = partial(base.sim_target, target=dual_ma_target(history, fast, slow))
    runners["MA20+ATR×1.5"] = partial(base.sim_target, target=dc.targets_from_signals(
        atr.channel_signals(history, 20, 20, 1.5)))
    runners["MA60+ATR×1.5"] = partial(base.sim_target, target=dc.targets_from_signals(
        atr.channel_signals(history, 60, 20, 1.5)))
    runners["MA200单线"] = partial(base.sim_target, target=dc.daily_ma_target(history, window=200))
    runners["MA200连续2日确认"] = partial(base.sim_target, target=ma_confirm_target(history))
    runners["唐奇安20/10"] = partial(base.sim_target, target=dc.build_target(history, 20, 10))
    runners["唐奇安55/20"] = partial(base.sim_target, target=dc.build_target(history, 55, 20))
    runners["唐奇安55/20+MA200过滤"] = partial(base.sim_target, target=donchian_ma_filter_target(history))
    runners["唐奇安+吊灯"] = partial(base.sim_target, target=chandelier_target(history))
    if "volume" in history.columns:
        runners["放量突破"] = partial(base.sim_target, target=volume_breakout_target(history))
    runners["波动扩张突破"] = partial(base.sim_target, target=vol_expansion_target(history))
    for name, (window, skip) in {"MOM63": (63, 0), "MOM126": (126, 0),
                                 "MOM252": (252, 0), "MOM252-21": (252, 21)}.items():
        runners[name] = partial(base.sim_target, target=mom_target(history, window, skip))
    runners["高波动回避"] = partial(base.sim_target, target=rv_regime_target(history))
    return runners


def rolling_cohorts(history, runners, years):
    """每月现金新建账、固定持有期；用网页 272 日起点，不继承虚拟持仓。"""
    available = history.loc[window_start(history):]
    rows = []
    for start, end in experience.rolling_windows(available.index, years):
        px = available.loc[(available.index >= start) & (available.index < end)]
        curves, metrics = {}, {}
        for name, run in runners.items():
            curve, count = run(px)
            curves[name] = curve
            metrics[name] = base.metrics(curve.nav, curve.exposure, count)
        for name in runners:
            if name != "长拿":
                rows.append(experience.cohort_row(name, px, years, metrics[name], metrics["长拿"],
                                                  curves[name], curves["长拿"]))
    return pd.DataFrame(rows)