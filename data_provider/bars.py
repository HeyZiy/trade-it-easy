# -*- coding: utf-8 -*-
"""ETF/指数单源日线与场内证券指定日原始收盘。信号日线用 data_provider.etf。"""

import logging
import math
from datetime import datetime, timedelta
from typing import Literal, Optional, Sequence

import pandas as pd

from .codes import (_split_prefix, is_a_stock_code, is_listed_fund_code, is_sh_lof_code,
                    market_suffix, normalize_stock_code)
from .types import safe_float

logger = logging.getLogger(__name__)

# 归一化输出列（与下游 analyze_etf / _score_etf 假定一致）
STD_COLS = ["date", "open", "high", "low", "close", "volume"]


def _etf_sym(code: str) -> str:
    """ETF 代码 → 新浪带市场前缀符号。显式前缀优先；裸码按 codes.market_suffix 判市场（码族外抛错）。"""
    num, pref = _split_prefix(code)
    if pref:
        return pref + num
    num = normalize_stock_code(num)
    suffix = market_suffix(num)
    if suffix is None:
        raise ValueError(f"无法确定 ETF {code} 的市场归属（码族外）")
    return suffix.lower() + num


def _norm_etf(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[STD_COLS]


def _norm_cn(df: pd.DataFrame) -> pd.DataFrame:
    df = df.rename(columns={
        "日期": "date", "开盘": "open", "最高": "high",
        "最低": "low", "收盘": "close", "成交量": "volume",
    })
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[STD_COLS]


def _finalize(df: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """去空、按日期升序；空则返回 None。"""
    if df is None or df.empty:
        return None
    df = df.dropna(subset=["close", "volume"]).sort_values("date").reset_index(drop=True)
    return df if len(df) > 0 else None


def get_etf_daily_em(
    code: str, end_date: str, *, begin_date: str = "1990-01-01",
    adjust: Literal["raw", "hfq", "both"] = "both",
) -> pd.DataFrame:
    """东财 ETF/沪市 LOF 日线。后复权字段带 hfq_ 前缀，量为份、额为元。"""
    import akshare as ak

    symbol = normalize_stock_code(code)
    if len(symbol) != 6 or not symbol.isdigit() or not is_listed_fund_code(symbol):
        raise ValueError(f"{code} 不是受支持的场内基金代码")
    fetch = ak.fund_lof_hist_em if is_sh_lof_code(symbol) else ak.fund_etf_hist_em
    adjustments = {"raw": ("",), "hfq": ("hfq",), "both": ("", "hfq")}.get(adjust)
    if adjustments is None:
        raise ValueError(f"不支持的东财 ETF 复权口径: {adjust}")
    rename = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
              "收盘": "close", "成交量": "volume", "成交额": "amount"}
    price_columns = ["open", "high", "low", "close"]
    start, end = begin_date.replace("-", ""), end_date.replace("-", "")
    tables = []
    for adjustment in adjustments:
        df = fetch(symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjustment)
        if df is None or df.empty:
            raise ValueError(f"{code} 东财 {adjustment or 'raw'} 行情为空")
        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"], errors="raise").dt.strftime("%Y-%m-%d")
        if df["date"].duplicated().any():
            raise ValueError(f"{code} 东财行情日期重复")
        if adjustment:
            df = df[["date", *price_columns]].rename(
                columns={column: f"hfq_{column}" for column in price_columns})
        else:
            df = df[["date", *price_columns, "volume", "amount"]].copy()
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
        tables.append(df)
    result = tables[0]
    if adjust == "both":
        result = result.merge(tables[1], on="date", how="left", validate="one_to_one")
    return result.sort_values("date")


def get_etf_daily(code: str) -> Optional[pd.DataFrame]:
    """ETF 日线全历史（新浪 fund_etf_hist_sina 单源，未复权原始价，带 amount）。

    台账收盘价保底（ClosingPrices）和 observe 现价补口走这里。
    信号日线走 data_provider.etf，不用本函数。
    返回形状唯一：date 列升序，
    columns=['date','open','high','low','close','volume','amount']；
    空/失败返回 None；非 ETF 码族拒绝（个股走 DataFetcherManager）。
    amount 为元口径。
    """
    if not is_listed_fund_code(code):
        logger.warning(f"[bars] {code} 不是 ETF：ETF 日线请传 ETF 代码；个股走 DataFetcherManager")
        return None
    import akshare as ak
    try:
        raw = ak.fund_etf_hist_sina(symbol=_etf_sym(code))
    except Exception as e:
        logger.warning(f"[bars] ETF {code} 取数失败: {e}")
        return None
    if raw is None or raw.empty:
        return None
    df = raw.copy()
    if "amount" not in df.columns:
        df["amount"] = 0.0                 # 源缺列时补零（流动性门槛会自然剔除）
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        if c not in df.columns:
            df[c] = float("nan")
        df[c] = pd.to_numeric(df[c], errors="coerce")
    out = _finalize(df)
    return out


def close_on_date(frame: Optional[pd.DataFrame], asof: str, date_column: str = "date") -> Optional[float]:
    """只接受指定交易日唯一有效的原始收盘，不用其他日期补价。"""
    if frame is None or frame.empty or not {date_column, "close"} <= set(frame.columns):
        return None
    days = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
    rows = frame.loc[days == pd.Timestamp(asof)]
    if len(rows) != 1:
        return None
    value = safe_float(rows["close"].iloc[0])
    return value if value is not None and math.isfinite(value) and value > 0 else None


def _etf_fallback_close(code: str, asof: str) -> Optional[float]:
    try:
        price = close_on_date(get_etf_daily_em(code, asof, begin_date=asof, adjust="raw"), asof)
        if price is not None:
            return price
    except Exception:
        logger.debug("东财原始收盘不可用：%s %s", code, asof, exc_info=True)

    try:
        return close_on_date(get_etf_daily(code), asof)
    except Exception:
        logger.exception("新浪原始收盘不可用：%s %s", code, asof)
        return None


def listed_close_prices(codes: Sequence[str], asof: str) -> dict[str, float]:
    """股票与场内基金共用 AmazingData 批量收盘；基金缺价再尝试东财和新浪。"""
    pending = {}
    for code in codes:
        if is_listed_fund_code(code) or is_a_stock_code(code):
            pending[code] = normalize_stock_code(code)
        else:
            logger.error("未识别的股票或场内基金代码：%s %s", code, asof)
    if not pending:
        return {}

    from .fetchers.amazingdata.market import fetch_kline

    try:
        frames = fetch_kline(list(dict.fromkeys(pending.values())), asof, asof)
    except Exception:
        logger.exception("AmazingData 批量收盘数据查询失败：%s", asof)
        frames = {}

    prices = {}
    for code, bare_code in pending.items():
        price = close_on_date(frames.get(bare_code), asof, "kline_time")
        if price is None and is_listed_fund_code(code):
            price = _etf_fallback_close(code, asof)
        if price is not None:
            prices[code] = price
    return prices


def get_index_daily(code: str, days: int = 400) -> Optional[pd.DataFrame]:
    """A股指数日线（三腿：csindex → 东财 → 新浪）。

    code 显式前缀优先（sh000001 / sz399006）；裸码按确定规则推断：399→sz，其余→sh。
    days 用于推算取数起点（日历日 ×2 留余量），默认 400（≈530 根日线）。

    三腿性质：csindex 服务 000/上证/中证（权威源）；399 深证/国证不在 csindex，
    由东财补；东财不可达时新浪保底（000 行业指数在新浪可能停更，仅前两者失败才触发）。
    """
    num, pref = _split_prefix(code)
    sina_pref = pref or ("sz" if num.startswith("399") else "sh")
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")

    import akshare as ak
    df = None
    try:
        # 中证官网优先（000 权威源，含滚动PE等元数据）
        raw = ak.stock_zh_index_hist_csindex(symbol=num, start_date=start, end_date=end)
        if raw is not None and not raw.empty:
            df = _norm_cn(raw)
    except Exception:
        pass
    # 东财备援：399 深证/国证等 csindex 不服务的指数
    if df is None or df.empty:
        try:
            raw = ak.index_zh_a_hist(symbol=num, period="daily",
                                     start_date=start, end_date=end)
            df = _norm_cn(raw)
        except Exception:
            pass
    # 新浪最后兜底（东财不可达时保底；000 行业指数可能停更，仅在前两者失败才触发）
    if df is None or df.empty:
        try:
            raw = ak.stock_zh_index_daily(symbol=f"{sina_pref}{num}")
            df = _norm_etf(raw)
        except Exception:
            pass
    return _finalize(df)


# ── 动态规则池取数（全市场在市 ETF 名单）──

def get_etf_universe() -> Optional[pd.DataFrame]:
    """全市场在市 ETF 名单（新浪单源）：DataFrame[code(6位裸码), name]。

    动态规则池的时点标的表。仅当前在市品种，已退市 ETF 不在列——
    回测需要含已退市的时点全表时走研究线平台数据，不用本函数。
    """
    import akshare as ak
    try:
        raw = ak.fund_etf_category_sina(symbol="ETF基金")
    except Exception as e:
        logger.warning(f"[bars] ETF 名单获取失败: {e}")
        return None
    if raw is None or raw.empty:
        return None
    cols = list(raw.columns)
    code_col = "代码" if "代码" in cols else cols[0]
    name_col = "名称" if "名称" in cols else cols[-1]
    out = pd.DataFrame({
        "code": raw[code_col].astype(str).str.extract(r"(\d{6})$", expand=False),
        "name": raw[name_col].astype(str).str.strip(),
    }).dropna(subset=["code"])
    return out.drop_duplicates(subset="code").sort_values("code").reset_index(drop=True)
