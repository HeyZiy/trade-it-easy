# -*- coding: utf-8 -*-
"""质量池 — 取数 seam（ AmazingData 批量实现 + 规范化装配）。

策略核（screener/execution）只消费本模块产出的规范化 DataFrame/字典；
本模块只做装配，判定逻辑一概不上。 AmazingData 依赖走函数内懒导入，
测试经 monkeypatch 替换 *_raw 层注入假数据。

口径（与定稿回测的质量、成长、估值和波动条件对齐）：
- 前复权收盘 = 原始 close × 单次复权因子（分数/波动率只看序列内比值，
  因子无需归一化）；
- 财务可见性：报告期 ≤ 截止日 且 披露日 ≤ 截止日；同报告期多行取最晚披露；
- ROE_TTM(%) = 四个连续已披露单季度归母净利之和 / 最新期末归母权益 ×100；
- 净利同比(%) = (本期单季合并净利 − 去年同季合并净利) / 去年同季绝对值 ×100；
  使用含少数股东损益的净利润，与回测inc_net_profit_year_on_year对应，基数为0或缺失则未知；
- PE(TTM) = 总市值 / TTM 归母净利，筛选要求PE/PB均为正；
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

# 253根交易日，额外预留节假日余量
CLOSES_WINDOW_DAYS = 430
UNLOCK_EVENT_DAYS = 90
FUNDAMENTAL_BATCH_SIZE = 32
# 查最近八个季度至 asof，覆盖正常披露滞后、最新单季、同比与四季 TTM。
FINANCIAL_LOOKBACK_QUARTERS = 8
# 股本按变动日期查询，需要包含多年未变动的最后一条记录。
EQUITY_HISTORY_BEGIN = "1990-01-01"


# ── 原始取数（测试 monkeypatch 点：替换为本模块内的同名函数） ──

def _raw_code_list(signal_date: str) -> List[str]:
    from data_provider.fetchers.amazingdata_info import fetch_code_list
    return fetch_code_list("EXTRA_STOCK_A_SH_SZ", asof=signal_date)


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

    全集来自 BaseData.get_hist_code_list 的 signal_date 快照；调用方传 T-1
    截止日而非信号日当天——当日代码表服务端盘前未必已发布。
    """
    universe: Set[str] = set()
    for raw in _raw_code_list(signal_date):
        code = str(raw).split(".")[0].strip()
        if code[:2] in ("60", "00"):
            universe.add(code)
    if not universe:
        raise RuntimeError("股票代码全集为空，无法构建股票池")
    return universe


def _raw_industry_base():
    from data_provider.fetchers.amazingdata_info import fetch_industry_base
    return fetch_industry_base()


def _raw_industry_members(index_code):
    from data_provider.fetchers.amazingdata_info import fetch_industry_members
    return fetch_industry_members(index_code)


def _industry_frame(raw):
    if isinstance(raw, pd.DataFrame):
        return raw
    if isinstance(raw, dict):
        frames = [v for v in raw.values() if isinstance(v, pd.DataFrame) and not v.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    raise RuntimeError('行业接口返回类型无效')


def fetch_industries(codes: List[str], asof: str) -> Dict[str, str]:
    """申万一级行业按纳入/剔除日期取截止日成员；缺失或归属冲突不默认独立行业。"""
    base = _industry_frame(_raw_industry_base())
    if not {'INDEX_CODE', 'LEVEL_TYPE'} <= set(base.columns):
        raise RuntimeError('申万行业基础表缺失')
    level1 = base[pd.to_numeric(base['LEVEL_TYPE'], errors='coerce') == 1]
    indices = sorted(set(level1['INDEX_CODE'].dropna().astype(str)))
    if not indices:
        raise RuntimeError('申万一级行业列表为空')
    requested, assignments = set(codes), {}
    cutoff = pd.Timestamp(asof).normalize()
    for index_code in indices:
        frame = _industry_frame(_raw_industry_members(index_code))
        if frame.empty:
            continue
        if not {'CON_CODE', 'INDATE', 'OUTDATE'} <= set(frame.columns):
            raise RuntimeError('行业成分表缺少成员日期')
        entered = pd.to_datetime(frame['INDATE'].astype(str), errors='coerce')
        exited = pd.to_datetime(frame['OUTDATE'].astype(str), errors='coerce')
        # 未退出允许空值；非法非空剔除日不能当作永久有效。
        blank_exit = frame['OUTDATE'].isna() | frame['OUTDATE'].astype(str).str.strip().eq('')
        active = (entered <= cutoff) & (blank_exit | (exited > cutoff))
        for raw_code in frame.loc[active, 'CON_CODE']:
            code = str(raw_code).split('.')[0]
            if code in requested:
                assignments.setdefault(code, set()).add(index_code)
    return {c: next(iter(keys)) for c, keys in assignments.items() if len(keys) == 1}


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

    dates = pd.DatetimeIndex(_calendar_dates(begin.date(), end.date()))
    closes = pd.DataFrame(series_by_code).reindex(dates).tail(rows)
    return closes


def _calendar_dates(begin, end):
    from src.trading_calendar import get_trading_dates
    return get_trading_dates(begin, end)


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
            # 只能向前携带已经出现的调整因子，不能用未来首个因子补历史。
            aligned = f.reindex(f.index.union(index)).sort_index().ffill().reindex(index)
            if aligned.isna().any() or (aligned <= 0).any() or not aligned.map(math.isfinite).all():
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
        "is_st": _numcol("IS_ST_SEC").map({0: False, 1: True}).astype("boolean"),
        "is_suspended": _numcol("IS_SUSP_SEC").map({0: False, 1: True}).astype("boolean"),
        "preclose": _numcol("PRECLOSE"),
        "high_limit": _numcol("HIGH_LIMITED"),
        "low_limit": _numcol("LOW_LIMITED"),
    })
    df.index = raw["MARKET_CODE"].astype(str).str.split(".").str[0]
    if df.index.has_duplicates:
        raise RuntimeError('状态表同一股票出现重复记录')
    return df


def _execution_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None)


def _raw_execution_data(codes, begin, end):
    from data_provider.fetchers.amazingdata_info import fetch_execution_data
    return fetch_execution_data(codes, begin, end)


def _risk_warning(value):
    """产品状态标志 4 为风险警示；未知编码不猜成正常。"""
    import re
    if isinstance(value, (list, tuple, set, np.ndarray)):
        values = list(value)
    elif value is None or pd.isna(value):
        return pd.NA
    else:
        values = re.split(r'[,;\s]+', str(value).strip())
    flags = set()
    for flag in values:
        if str(flag) == '':
            continue
        try:
            number = float(flag)
        except (TypeError, ValueError):
            return pd.NA
        if not math.isfinite(number) or not number.is_integer() or not 0 <= number <= 13:
            return pd.NA
        flags.add(int(number))
    return 4 in flags if flags else pd.NA


def _snapshot_suspended(code: str, phase):
    """沪深编码不同；仅确认连续交易且接受交易时放行。"""
    from data_provider.codes import market_suffix
    if not isinstance(phase, str):
        return pd.NA
    market = market_suffix(code)
    if market == 'SH':
        if phase.startswith('P'):
            return True
        if len(phase) >= 4 and phase[0] == 'T' and phase[1:4] == '111':
            return False
    elif market == 'SZ':
        if phase.startswith('H') or (len(phase) >= 2 and phase[1] == '1'):
            return True
        if phase.startswith('T0'):
            return False
    return pd.NA


def fetch_execution_status(codes: List[str], exec_date: str) -> pd.DataFrame:
    """当天风险警示 + 最近120秒的官方交易阶段和涨跌停价。

    只支持当天执行；缺行、过期或失败由装配层按 unavailable 阻断。
    """
    columns = ['is_st', 'is_suspended', 'high_limit', 'low_limit']
    now = _execution_now()
    if not codes or now.date().isoformat() != exec_date or now.hour < 9:
        return pd.DataFrame(columns=columns)
    begin = now - pd.Timedelta(seconds=120)
    try:
        info, snapshots = _raw_execution_data(codes, begin, now)
    except Exception:
        logger.exception('[quality_pool] 当天执行状态不可用，阻止成交')
        return pd.DataFrame(columns=columns)
    # 请求本身可能耗时；新鲜度按返回后的时刻检查。
    now = _execution_now()
    if now.date().isoformat() != exec_date:
        return pd.DataFrame(columns=columns)
    begin = now - pd.Timedelta(seconds=120)
    if info is None or info.empty or 'security_status' not in info:
        return pd.DataFrame(columns=columns)
    info = info.copy()
    info.index = info.index.astype(str).str.split('.').str[0]
    if info.index.has_duplicates:
        raise RuntimeError('当天证券信息同一股票出现重复记录')
    rows = {}
    for code in codes:
        raw = snapshots.get(code)
        if code not in info.index or raw is None or raw.empty:
            continue
        stamps = pd.to_datetime(raw['trade_time'] if 'trade_time' in raw
                                else raw.index, errors='coerce')
        stamps = pd.DatetimeIndex(stamps)
        if stamps.tz is not None:
            stamps = stamps.tz_convert('Asia/Shanghai').tz_localize(None)
        valid = np.flatnonzero(~stamps.isna())
        if not len(valid):
            continue
        # 先取最新记录；不让早先的正常状态掩盖最新的未知状态。
        pos = valid[stamps[valid].argmax()]
        if not begin <= stamps[pos] <= now:
            continue
        row = raw.iloc[pos]
        rows[code] = {
            'is_st': _risk_warning(info.at[code, 'security_status']),
            'is_suspended': _snapshot_suspended(code, row.get('trading_phase_code')),
            'high_limit': pd.to_numeric(row.get('high_limited'), errors='coerce'),
            'low_limit': pd.to_numeric(row.get('low_limited'), errors='coerce'),
        }
        if pd.isna(rows[code]['is_st']):
            logger.warning('[quality_pool] %s 当天风险警示编码未知：%r',
                           code, info.at[code, 'security_status'])
    frame = pd.DataFrame.from_dict(rows, orient='index', columns=columns)
    for col in ('is_st', 'is_suspended'):
        frame[col] = frame[col].astype('boolean')
    return frame


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
    roe_ttm_pct / np_yoy_pct / pe_ttm / pb；个别字段未知保留NaN供三态判定。

    每批算完只留下四个比率，不把全市场三类原表同时装入内存。
    """
    rows: Dict[str, dict] = {}
    for start in range(0, len(codes), FUNDAMENTAL_BATCH_SIZE):
        batch = codes[start:start + FUNDAMENTAL_BATCH_SIZE]
        rows.update(_fundamental_batch(batch, asof, raw_closes))
        logger.info("[quality_pool] 财务快照 %s/%s，累计有效 %s",
                    start + len(batch), len(codes), len(rows))
    frame = pd.DataFrame.from_dict(rows, orient="index")
    return frame.replace([np.inf, -np.inf], np.nan)


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
        consolidated_np = _visible_quarterly_profit(
            income.get(code), asof_ts, 'NET_PRO_INCL_MIN_INT_INC')
        equity = _latest_visible_equity(balances.get(code), asof_ts)
        shares = _latest_total_shares(structures.get(code), asof_ts)
        close_raw = (raw_closes or {}).get(code)
        total_mv = (shares * 1e4 * close_raw
                    if shares is not None and shares > 0 and close_raw is not None
                    and math.isfinite(close_raw) and close_raw > 0 else None)
        np_ttm = _ttm_np(np_quarters) if np_quarters is not None else None
        row = {
            "roe_ttm_pct": (np_ttm / equity * 100.0
                            if np_ttm is not None and equity not in (None, 0) else np.nan),
            "np_yoy_pct": _profit_yoy(consolidated_np),
            "pe_ttm": (total_mv / np_ttm
                       if total_mv is not None and np_ttm not in (None, 0) else np.nan),
            "pb": total_mv / equity if total_mv is not None and equity not in (None, 0) else np.nan,
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
    return _visible_quarterly_profit(income, asof, 'NET_PRO_EXCL_MIN_INT_INC')


def _visible_quarterly_profit(income: Optional[pd.DataFrame],
                              asof: pd.Timestamp, field: str) -> Optional[pd.Series]:
    df = _visible(income, FINANCIAL_STATEMENT_TYPE_SINGLE, asof)
    if df is None or field not in df.columns:
        return None
    np_series = pd.to_numeric(df[field], errors="coerce")
    np_series.index = df["period"]
    return np_series


def _profit_yoy(quarters: Optional[pd.Series]) -> float:
    if quarters is None or quarters.empty:
        return np.nan
    latest = quarters.iloc[-1]
    prior = _same_quarter_last_year(quarters, quarters.index[-1])
    if (prior is None or prior == 0 or not math.isfinite(prior)
            or not math.isfinite(latest)):
        return np.nan
    return float((latest - prior) / abs(prior) * 100.0)


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
    if len(last4) != 4 or last4.isna().any() or not last4.map(math.isfinite).all():
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
    """执行时实时价与简称：单只跨源合并，失败/无效价不入结果（fail-soft）。"""
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
        if "kline_time" not in df.columns:
            continue
        dates = pd.to_datetime(df["kline_time"], errors="coerce").dt.normalize()
        rows = df.loc[dates == pd.Timestamp(asof).normalize()]
        if not rows.empty:
            value = float(pd.to_numeric(rows["close"], errors="coerce").iloc[-1])
            if math.isfinite(value) and value > 0:
                out[code] = value
    return out


def day_limits(codes: List[str], exec_date: str,
               prev_close: Dict[str, float],
               status: Optional[pd.DataFrame] = None) -> Dict[str, Tuple[float, float]]:
    """执行日涨跌停价只接受当天官方有效值；缺失不推造10%限制。

    status 可传已拉取的状态表（装配层与停牌/ST 共享单次调用）；缺省时自行拉取。
    """
    limits: Dict[str, Tuple[float, float]] = {}
    if status is None:
        status = fetch_execution_status(codes, exec_date)
    for code in codes:
        if code in status.index and {'high_limit', 'low_limit'} <= set(status.columns):
            high = status.at[code, "high_limit"]
            low = status.at[code, "low_limit"]
            if (pd.notna(high) and pd.notna(low) and math.isfinite(high)
                    and math.isfinite(low) and high > low > 0):
                limits[code] = (float(high), float(low))
                continue
    return limits
