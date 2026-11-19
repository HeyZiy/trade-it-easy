"""ETF 信号日线：按用途整段选源，不跨数据源拼接同一标的的日线。"""

import logging
import math
from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np
import pandas as pd

from . import bars
from .codes import is_etf_code, normalize_stock_code

logger = logging.getLogger(__name__)

EASTMONEY = "东财"
AMAZINGDATA = "AmazingData"
OHLC = ("open", "high", "low", "close")
HLC = OHLC[1:]
_SOURCE_ORDER = {
    "rotation": (EASTMONEY,),
    "observation": (AMAZINGDATA, EASTMONEY),
    "online_research": (EASTMONEY, AMAZINGDATA),
}


@dataclass(frozen=True)
class EtfBars:
    history: pd.DataFrame
    source: str


def adjusted_etf_history(
    rows: pd.DataFrame, end_date: str, *, fields: Sequence[str] = OHLC, min_bars: int = 1,
) -> pd.DataFrame:
    """校验截止日内的后复权日线，返回日期索引及不带 hfq_ 前缀的价格列。"""
    if rows is None or rows.empty:
        raise ValueError("行情为空")
    dates = pd.to_datetime(rows["date"], errors="raise").dt.normalize()
    if dates.isna().any():
        raise ValueError("行情日期无效")
    window = rows.set_index(dates).sort_index().loc[:end_date]
    if not window.index.is_unique:
        raise ValueError("行情日期重复")
    if len(window) < min_bars:
        raise ValueError(f"已收盘日线不足{min_bars}日")
    history = window[[f"hfq_{field}" for field in fields]].set_axis(fields, axis=1)
    history = history.apply(pd.to_numeric, errors="coerce")
    if (not np.isfinite(history.to_numpy()).all() or (history <= 0).any().any()
            or (history.high < history.max(axis=1)).any() or (history.low > history.min(axis=1)).any()):
        raise ValueError("复权OHLC无效")
    return history


def _rotation_history(rows: pd.DataFrame, decision_date: str) -> pd.DataFrame:
    frame = rows[["date", "close", "hfq_close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date").set_index("date")
    decision = pd.Timestamp(decision_date)
    frame = frame.loc[frame.index <= decision]
    history = frame.loc[frame.index < decision]
    if history.empty:
        raise ValueError("没有可用于锚定的已完成收盘")
    raw_last = float(history["close"].iloc[-1])
    hfq_last = float(history["hfq_close"].iloc[-1])
    if not (math.isfinite(raw_last) and raw_last > 0 and math.isfinite(hfq_last) and hfq_last > 0):
        raise ValueError("最近已完成 bar 原始/后复权收盘无效，无法锚定")
    return frame[["hfq_close"]].rename(columns={"hfq_close": "close"}) * (raw_last / hfq_last)


def _amazingdata_rows(codes: list[str], end_date: str, *, full_history: bool) -> dict[str, pd.DataFrame]:
    from .fetchers.amazingdata import client, market

    if full_history:
        if not client.is_configured():
            return {}
        return market.fetch_etf_hfq_bars(codes, end_date, begin_date="1990-01-01")
    return market.fetch_etf_hfq_bars(codes, end_date)


def etf_daily_bars(
    codes: Sequence[str], end_date: str, *,
    policy: Literal["rotation", "observation", "online_research"],
    completed_through: str | None = None, min_bars: int = 1,
) -> dict[str, EtfBars]:
    """批量取信号日线并保留来源。无合格序列的标的不返回；结果以请求代码为键。

    rotation 返回锚定至决策日前最后一根原始收盘的 close，保留决策日行供展示补价。
    observation/online_research 返回截止 completed_through（默认 end_date）的后复权 HLC/OHLC。
    """
    requested = {code: normalize_stock_code(code) for code in codes if is_etf_code(code)}
    result: dict[str, EtfBars] = {}
    cutoff = min(end_date, completed_through) if completed_through is not None else end_date
    fields = HLC if policy == "observation" else OHLC
    for source in _SOURCE_ORDER[policy]:
        pending = {code: bare_code for code, bare_code in requested.items() if code not in result}
        if not pending:
            break
        uses_batch = source == AMAZINGDATA
        batch = {}
        if uses_batch:
            try:
                batch = _amazingdata_rows(
                    list(dict.fromkeys(pending.values())), end_date, full_history=policy == "online_research",
                )
            except Exception as exc:
                logger.warning("ETF 日线 %s 批量取数失败：%s", source, exc)
                continue
        for code, bare_code in pending.items():
            try:
                rows = (batch.get(bare_code) if uses_batch
                        else bars.get_etf_daily_em(bare_code, end_date))
                if rows is None:
                    continue
                if policy == "rotation":
                    history = _rotation_history(rows, end_date)
                else:
                    history = adjusted_etf_history(rows, cutoff, fields=fields, min_bars=min_bars)
            except Exception as exc:
                logger.warning("ETF 日线 %s %s 不可用：%s", source, code, exc)
                continue
            result[code] = EtfBars(history, source)
    return result
