# -*- coding: utf-8 -*-
"""唐奇安通道纯计算：timing 研究的共享信号核（research/studies/timing/common/donchian.py 引用）。

只含此前日线的通道、收盘严格突破确认、次日开盘执行；不涉及订单或账户。
"""

import numpy as np
import pandas as pd


def channel_signals(history, entry=55, exit=20, channel="ohlc"):
    """只含此前日线的通道；收盘严格突破，次日开盘执行，区间内维持状态。"""
    if not isinstance(entry, int) or not isinstance(exit, int) or min(entry, exit) < 1:
        raise ValueError("通道周期须为正整数")
    if channel not in ("ohlc", "close"):
        raise ValueError("通道须为 ohlc 或 close")
    upper_source = history["high"] if channel == "ohlc" else history["close"]
    lower_source = history["low"] if channel == "ohlc" else history["close"]
    upper = upper_source.rolling(entry, min_periods=entry).max().shift(1)
    lower = lower_source.rolling(exit, min_periods=exit).min().shift(1)
    valid = history["close"].notna() & upper.notna() & lower.notna()
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[valid & (history["close"] > upper)] = 1.
    signal.loc[valid & (history["close"] < lower)] = 0.
    execution = pd.Series(history.index, index=history.index).shift(-1)
    return pd.DataFrame({"close": history["close"], "entry_upper": upper,
                         "exit_lower": lower, "target": signal,
                         "execution_date": execution}).rename_axis("signal_date")
