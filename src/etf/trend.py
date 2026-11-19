"""唐奇安纯计算：研究与观察报告共用，不涉及订单或账户。"""

import numpy as np
import pandas as pd

from data_provider.etf import HLC, adjusted_etf_history

OBSERVATION_MIN_BARS = 21


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


def observation_cutoff(now):
    """上海时间盘中排除当天。"""
    cutoff = pd.Timestamp(now).date()
    if (now.hour, now.minute) < (15, 0):
        cutoff -= pd.Timedelta(days=1)
    return cutoff.isoformat()


def donchian_observation(bars, *, now):
    """从供应商后复权日线回放20/10，只用已完成的收盘。"""
    return observe_donchian_history(adjusted_etf_history(
        bars, observation_cutoff(now), fields=HLC, min_bars=OBSERVATION_MIN_BARS))


def observe_donchian_history(history):
    """从已经过校验的完整日线回放状态；没有明确突破时不猜初始状态。"""
    signals = channel_signals(history, entry=20, exit=10)
    events = signals.target.dropna()
    latest = signals.iloc[-1]
    return {"state": bool(events.iloc[-1]) if not events.empty else None,
            "asof": history.index[-1].strftime("%Y-%m-%d"),
            "last_signal": events.index[-1].strftime("%Y-%m-%d") if not events.empty else None,
            "upper": float(latest.entry_upper), "lower": float(latest.exit_lower)}
