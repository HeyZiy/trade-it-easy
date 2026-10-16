"""唐奇安纯计算：研究与观察报告共用，不涉及订单或账户。"""

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


def donchian_observation(bars, *, now):
    """从供应商后复权 OHLC 回放20/10；只使用上海时间已收盘的日线。

    状态来自历史最近一次明确突破，而非实际持仓。尚无突破时不猜初始状态。
    now 为上海本地时间（入口传入），盘中排除当天；行情日期显式返回供报告展示。
    """
    if bars is None or bars.empty:
        raise ValueError("行情为空")
    history = bars.copy()
    history.index = pd.to_datetime(history["date"], errors="raise").dt.normalize()
    cutoff = pd.Timestamp(now).normalize()
    if (now.hour, now.minute) < (15, 0):
        history = history.loc[history.index < cutoff]
    else:
        history = history.loc[history.index <= cutoff]
    history = history.sort_index()
    if history.index.has_duplicates:
        raise ValueError("行情日期重复")
    if len(history) < 21:
        raise ValueError("已收盘日线不足21日")
    history = history[["hfq_high", "hfq_low", "hfq_close"]].rename(
        columns=lambda col: col.removeprefix("hfq_"))
    history = history.apply(pd.to_numeric, errors="coerce")
    if (not np.isfinite(history.to_numpy()).all() or (history <= 0).any().any()
            or (history.high < history.low).any()
            or (history.close > history.high).any()
            or (history.close < history.low).any()):
        raise ValueError("复权OHLC无效")
    signals = channel_signals(history, entry=20, exit=10)
    events = signals.target.dropna()
    latest = signals.iloc[-1]
    return {"state": bool(events.iloc[-1]) if not events.empty else None,
            "asof": history.index[-1].strftime("%Y-%m-%d"),
            "last_signal": events.index[-1].strftime("%Y-%m-%d") if not events.empty else None,
            "upper": float(latest.entry_upper), "lower": float(latest.exit_lower)}
