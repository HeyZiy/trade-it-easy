# -*- coding: utf-8 -*-
"""质量池 — 取数 seam（ AmazingData 批量实现 + 规范化装配）。

策略核（screener/execution）只消费本模块产出的规范化 DataFrame/字典；
本模块只做装配，判定逻辑一概不上。 AmazingData 依赖走函数内懒导入，
测试经 monkeypatch 替换 *_raw 层注入假数据。

口径（对照 research/studies/roe_quality/pool_rotation_v3a.py 与规格一、二节）：
- 前复权收盘 = 原始 close × 单次复权因子（分数/波动率只看序列内比值，
  因子无需归一化）；
- 财务可见性：报告期 ≤ 截止日 且 披露日 ≤ 截止日；同报告期多行取最晚披露；
- 单季 ROE(%) = 单季归母净利 / 最新期末归母权益 × 100（年化 ×4 由阈值 3
  吸收，规格明示这是近似）；
- 净利同比(%) = 单季归母净利 / 去年同季单季归母净利 − 1（去年同季缺失或
  ≤0 视为不可比 → NaN → 该股不进池）；
- PE(TTM) = 总市值 / TTM 归母净利（≤0 → 负值照过上界，规格明示）；
  PB = 总市值 / 期末归母权益；总市值 = 总股本 × 截止日原始收盘。
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from src.quality_pool.config import (
    FINANCIAL_STATEMENT_TYPE_CONSOL, FINANCIAL_STATEMENT_TYPE_SINGLE,
    SCORE_CLOSES, VOL_LOOKBACK,
)

logger = logging.getLogger(__name__)

# K 线自然日窗口：121 根交易日 ≈ 180 自然日，留节假日与停牌余量
CLOSES_WINDOW_DAYS = 260
UNLOCK_EVENT_DAYS = 90
FUNDAMENTAL_BATCH_SIZE = 32
# 查最近八个季度至 asof，覆盖正常披露滞后、最新单季、同比与四季 TTM。
FINANCIAL_LOOKBACK_QUARTERS = 8
# 股本按变动日期查询，需要包含多年未变动的最后一条记录。
EQUITY_HISTORY_BEGIN = "1990-01-01"


# ── 原始取数（测试 monkeypatch 点：替换为本模块内的同名函数） ──

def _raw_code_list() -> List[str]:
    from data_provider.fetchers.amazingdata_info import fetch_code_list
    return fetch_code_list("EXTRA_STOCK_A_SH_SZ")


def _raw_status(codes: List[str], date: str) -> pd.DataFrame:
    from data_provider.fetchers.amazingdata_info import fetch_status
    return fetch_status(codes, date)


def _raw_income(codes: List[str], begin: str, end: str) -> Dict[str, pd.DataFrame]:
    from data_provider.fetchers.amazingdata_info import fetch_income
    return fetch_income(codes, begin, end)


def _raw_balance_sheets(codes: List[str], begin: str, end: str) -> Dict[str, pd.DataFrame]:
    from data_provider.fetchers.amazingdata_info import fetch_balance_sheets
    return fetch_balance_sheets(codes, begin, end)


def _raw_equity_structure(codes: List[str], begin: str, end: str) -> Dict[str, pd.DataFrame]:
    from data_provider.fetchers.amazingdata_info import fetch_equity_structure
    return fetch_equity_structure(codes, begin, end)


def _raw_restricted(codes: List[str], begin: str, end: str) -> pd.DataFrame:
    from data_provider.fetchers.amazingdata_info import fetch_restricted
    return fetch_restricted(codes, begin, end)


def _raw_adj_factors(codes: List[str], begin: str, end: str) -> pd.DataFrame:
    from data_provider.fetchers.amazingdata_info import fetch_adj_factors
    return fetch_adj_factors(codes, begin, end)


def _raw_kline(codes: List[str], begin: str, end: str) -> Dict[str, pd.DataFrame]:
    from data_provider.fetchers.amazingdata_info import fetch_kline
    return fetch_kline(codes, begin, end)


# ── 装配 ──

def fetch_universe(signal_date: str) -> Set[str]:
    """信号日主板在市 A 股：60/00 开头（白名单同时排除科创/创业/北交所/B股）。

    全集来自 BaseData.get_code_list（当日全市场，信号日即今日，无需历史宇宙）。
    """
    universe: Set[str] = set()
    for raw in _raw_code_list():
        code = str(raw).split(".")[0].strip()
        if code[:2] in ("60", "00"):
            universe.add(code)
    if not universe:
        raise RuntimeError("股票代码全集为空，无法构建股票池")
    return universe


def fetch_closes(codes: List[str], asof: str,
                 rows: int = SCORE_CLOSES) -> pd.DataFrame:
    """截止 asof 的前复权收盘矩阵（dates × codes，恰好 rows 行）。

    上市不足 rows 或窗口内停牌造成的缺失保留 NaN（筛选/计分各自处理）。
    """
    end = pd.Timestamp(asof)
    begin = end - pd.Timedelta(days=CLOSES_WINDOW_DAYS)
    kline = _raw_kline(codes, begin.strftime("%Y-%m-%d"), asof)
    factors = _raw_adj_factors(codes, begin.strftime("%Y-%m-%d"), asof)

    series_by_code: Dict[str, pd.Series] = {}
    for code, df in kline.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        s = pd.Series(
            pd.to_numeric(df["close"], errors="coerce").values,
            index=pd.to_datetime(df["kline_time"]).dt.normalize(),
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        factor = _factor_series(factors, code, s.index)
        if factor is None:
            logger.warning(f"[quality_pool] {code} 无复权因子，收盘不参与计分")
            continue
        series_by_code[code] = s * factor
    if not series_by_code:
        return pd.DataFrame()

    closes = pd.DataFrame(series_by_code)
    closes = closes.loc[(closes.index <= pd.Timestamp(asof))].tail(rows)
    return closes


def _factor_series(factors: pd.DataFrame, code: str,
                   index: pd.DatetimeIndex) -> Optional[pd.Series]:
    """单次复权因子对齐到收盘序列索引（按日期 ffill，缺失返回 None）。"""
    if factors is None or factors.empty:
        return None
    for col in (code, f"{code[:6]}.SH" if code.startswith("6") else f"{code[:6]}.SZ"):
        if col in factors.columns:
            f = pd.to_numeric(factors[col], errors="coerce")
            f.index = pd.to_datetime(f.index).normalize()
            f = f[~f.index.duplicated(keep="last")].sort_index()
            aligned = f.reindex(index).ffill().bfill()
            if aligned.isna().any():
                return None
            return aligned
    return None


def fetch_status(codes: List[str], date: str) -> pd.DataFrame:
    """按日状态快照 → index=code：is_st / is_suspended / preclose / 涨跌停价。"""
    raw = _raw_status(codes, date)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["is_st", "is_suspended"])
    def _numcol(name: str) -> pd.Series:
        if name in raw.columns:
            return pd.to_numeric(raw[name], errors="coerce")
        return pd.Series(np.nan, index=raw.index)

    df = pd.DataFrame({
        "is_st": _numcol("IS_ST_SEC").fillna(0) == 1,
        "is_suspended": _numcol("IS_SUSP_SEC").fillna(0) == 1,
        "preclose": _numcol("PRECLOSE"),
        "high_limit": _numcol("HIGH_LIMITED"),
        "low_limit": _numcol("LOW_LIMITED"),
    })
    df.index = raw["MARKET_CODE"].astype(str).str.split(".").str[0]
    return df


def fetch_unlock_codes(codes: List[str], signal_date: str) -> Set[str]:
    """[信号日, +90 自然日] 内存在任意已知解禁事件的代码集合。"""
    begin = pd.Timestamp(signal_date)
    end = begin + pd.Timedelta(days=UNLOCK_EVENT_DAYS)
    raw = _raw_restricted(codes, begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if raw is None or raw.empty or "LIST_DATE" not in raw.columns:
        return set()
    in_window = pd.to_datetime(raw["LIST_DATE"], errors="coerce").between(
        begin, end)
    raw = raw[in_window]
    if "MARKET_CODE" not in raw.columns:
        return set()
    return {str(c).split(".")[0] for c in raw["MARKET_CODE"].dropna().unique()}


def fetch_fundamentals(codes: List[str], asof: str,
                       raw_closes: Optional[Dict[str, float]] = None) -> pd.DataFrame:
    """截止 asof 的财务/估值快照 → index=code：
    roe_single_pct / np_yoy_pct / pe_ttm / pb（任一不可计算的整股剔除）。

    每批算完只留下四个比率，不把全市场三类原表同时装入内存。
    """
    rows: Dict[str, dict] = {}
    for start in range(0, len(codes), FUNDAMENTAL_BATCH_SIZE):
        batch = codes[start:start + FUNDAMENTAL_BATCH_SIZE]
        rows.update(_fundamental_batch(batch, asof, raw_closes))
        logger.info("[quality_pool] 财务快照 %s/%s，累计有效 %s",
                    start + len(batch), len(codes), len(rows))
    frame = pd.DataFrame.from_dict(rows, orient="index")
    return frame.replace([np.inf, -np.inf], np.nan).dropna()


def _fundamental_batch(codes: List[str], asof: str,
                       raw_closes: Optional[Dict[str, float]]) -> Dict[str, dict]:
    asof_ts = pd.Timestamp(asof)
    begin = (asof_ts.to_period("Q") - FINANCIAL_LOOKBACK_QUARTERS).start_time
    income = _raw_income(codes, begin.strftime("%Y-%m-%d"), asof)
    balances = _raw_balance_sheets(codes, begin.strftime("%Y-%m-%d"), asof)
    structures = _raw_equity_structure(codes, EQUITY_HISTORY_BEGIN, asof)

    rows: Dict[str, dict] = {}
    for code in codes:
        np_quarters = _visible_quarterly_np(income.get(code), asof_ts)
        if np_quarters is None:
            continue
        np_latest = np_quarters.iloc[-1]
        np_prev_year = _same_quarter_last_year(np_quarters, np_quarters.index[-1])
        if np_prev_year is None or not np_prev_year > 0:
            continue   # 去年同季缺失/非正：同比不可比
        equity = _latest_visible_equity(balances.get(code), asof_ts)
        if equity is None or equity <= 0:
            continue
        shares = _latest_total_shares(structures.get(code), asof_ts)
        close_raw = (raw_closes or {}).get(code)
        if shares is None or close_raw is None or close_raw <= 0:
            continue
        total_mv = shares * 1e4 * close_raw     # TOT_SHARE 单位万股
        np_ttm = _ttm_np(np_quarters)
        if np_ttm is None:
            continue
        row = {
            "roe_single_pct": np_latest / equity * 100.0,
            "np_yoy_pct": (np_latest / np_prev_year - 1.0) * 100.0,
            "pe_ttm": total_mv / np_ttm if np_ttm != 0 else np.nan,
            "pb": total_mv / equity,
        }
        rows[code] = row
    return rows


def _parse_period(value) -> pd.Timestamp:
    """报告期解析：日期串（20260331/2026-03-31）或 '2026Q1' 风格，非法返回 NaT。"""
    text = str(value).strip().lower()
    try:
        if len(text) == 6 and text[:4].isdigit() and text[4] == "q" and text[5] in "1234":
            return pd.Period(text.upper(), freq="Q").end_time.normalize()
        ts = pd.Timestamp(value)
        return ts.normalize()
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


def _visible(frame: Optional[pd.DataFrame], statement_type: int,
             asof: pd.Timestamp) -> Optional[pd.DataFrame]:
    """三张报表公共规整：指定报表类型 + 报告期/披露日均 ≤ 截止日 + 同期取最晚披露。"""
    if frame is None or frame.empty:
        return None
    need = {"REPORTING_PERIOD", "ANN_DATE", "STATEMENT_TYPE"}
    if not need <= set(frame.columns):
        return None
    df = frame.copy()
    df = df[pd.to_numeric(df["STATEMENT_TYPE"], errors="coerce") == statement_type]
    df["period"] = df["REPORTING_PERIOD"].map(_parse_period)
    ann = _to_dates(df["ACTUAL_ANN_DATE"] if "ACTUAL_ANN_DATE" in df.columns else None)
    ann = ann.reindex(df.index).fillna(_to_dates(df["ANN_DATE"]).reindex(df.index))
    df["ann"] = ann
    df = df.dropna(subset=["period", "ann"])
    df = df[(df["period"] <= asof) & (df["ann"] <= asof)]
    if df.empty:
        return None
    return df.sort_values("ann").drop_duplicates("period", keep="last").sort_values("period")


def _to_dates(column) -> pd.Series:
    if column is None:
        return pd.Series(pd.NaT, index=range(0))
    return pd.to_datetime(column, errors="coerce")


def _visible_quarterly_np(income: Optional[pd.DataFrame],
                          asof: pd.Timestamp) -> Optional[pd.Series]:
    df = _visible(income, FINANCIAL_STATEMENT_TYPE_SINGLE, asof)
    if df is None or "NET_PRO_EXCL_MIN_INT_INC" not in df.columns:
        return None
    np_series = pd.to_numeric(df["NET_PRO_EXCL_MIN_INT_INC"], errors="coerce")
    np_series.index = df["period"]
    return np_series.dropna()


def _same_quarter_last_year(np_quarters: pd.Series,
                            last_period: pd.Timestamp) -> Optional[float]:
    target = last_period - pd.DateOffset(years=1)
    for period, value in np_quarters.items():
        if period == target:
            return float(value)
    return None


def _ttm_np(np_quarters: pd.Series) -> Optional[float]:
    last4 = np_quarters.iloc[-4:]
    # 必须四个连续报告季，窗口或服务端缺季不能冒充完整 TTM。
    if len(last4) != 4:
        return None
    quarters = pd.DatetimeIndex(last4.index).to_period("Q").asi8
    if not (np.diff(quarters) == 1).all():
        return None
    return float(last4.sum())


def _latest_visible_equity(balance: Optional[pd.DataFrame],
                           asof: pd.Timestamp) -> Optional[float]:
    df = _visible(balance, FINANCIAL_STATEMENT_TYPE_CONSOL, asof)
    if df is None or "TOT_SHARE_EQUITY_EXCL_MIN_INT" not in df.columns:
        return None
    value = pd.to_numeric(df["TOT_SHARE_EQUITY_EXCL_MIN_INT"], errors="coerce").iloc[-1]
    return None if pd.isna(value) else float(value)


def _latest_total_shares(structure: Optional[pd.DataFrame],
                         asof: pd.Timestamp) -> Optional[float]:
    if structure is None or structure.empty or "TOT_SHARE" not in structure.columns:
        return None
    df = structure.copy()
    change = pd.to_datetime(df.get("CHANGE_DATE"), errors="coerce")
    df = df[change <= asof]
    if df.empty:
        return None
    df = df.assign(_change=change[change <= asof]).sort_values("_change")
    value = pd.to_numeric(df["TOT_SHARE"], errors="coerce").iloc[-1]
    return None if pd.isna(value) else float(value)


def fetch_realtime_quotes(codes: List[str]) -> Tuple[Dict[str, float], Dict[str, str]]:
    """09:31 实时价与简称：单只跨源合并，失败/无效价不入结果（fail-soft）。"""
    prices: Dict[str, float] = {}
    names: Dict[str, str] = {}
    from data_provider import get_fetcher
    manager = get_fetcher()
    for code in codes:
        try:
            quote = manager.get_realtime_quote(code)
        except Exception as e:
            logger.warning(f"[quality_pool] 实时行情失败 {code}: {e}")
            continue
        if quote is None:
            continue
        if quote.name:
            names[code] = quote.name
        px = getattr(quote, "price", None)
        if px is not None and math.isfinite(px) and px > 0:
            prices[code] = float(px)
    return prices, names


def raw_closes_at(codes: List[str], asof: str) -> Dict[str, float]:
    """截止 asof 的原始（未复权）收盘，供总市值计算。"""
    kline = _raw_kline(codes, (pd.Timestamp(asof) - pd.Timedelta(days=20))
                       .strftime("%Y-%m-%d"), asof)
    out: Dict[str, float] = {}
    for code, df in kline.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        s = pd.to_numeric(df["close"], errors="coerce").dropna()
        if len(s):
            out[code] = float(s.iloc[-1])
    return out


def day_limits(codes: List[str], exec_date: str,
               prev_close: Dict[str, float],
               status: Optional[pd.DataFrame] = None) -> Dict[str, Tuple[float, float]]:
    """执行日涨跌停价：状态表有值用官方值，缺失回退 前收×(1±10%)（主板）。

    status 可传已拉取的状态表（装配层与停牌/ST 共享单次调用）；缺省时自行拉取。
    """
    limits: Dict[str, Tuple[float, float]] = {}
    if status is None:
        status = fetch_status(codes, exec_date)
    for code in codes:
        if code in status.index:
            high = status.at[code, "high_limit"]
            low = status.at[code, "low_limit"]
            if pd.notna(high) and pd.notna(low):
                limits[code] = (float(high), float(low))
                continue
        px = prev_close.get(code)
        if px:
            limits[code] = (round(px * 1.1, 2), round(px * 0.9, 2))
    return limits
