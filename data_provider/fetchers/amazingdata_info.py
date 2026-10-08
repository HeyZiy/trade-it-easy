# -*- coding: utf-8 -*-
"""
===================================
AmazingData 批量 InfoData/BaseData 包装 — 质量池数据层专用
===================================

InfoData 只使用 begin_date/end_date 查询，不启用全历史 HDF5 缓存。
SDK 不支持服务端字段投影：每批返回后立即复制所需列，释放宽表。
登录态复用 amazingdata_fetcher；入参为 6 位代码，内部转 TGW 格式。
单次复权因子没有日期查询接口，仍使用 SDK 缓存，但只返回所需窗口。

接口清单（返回均已做基础规整）：
- fetch_stock_basics(code_list)          → DataFrame（get_stock_basic，无本地缓存）
- fetch_status(code_list, date)          → DataFrame（get_history_stock_status，按日）
- fetch_income(code_list, begin, end)    → dict[code, DataFrame]（get_income）
- fetch_balance_sheets(code_list, b, e)  → dict[code, DataFrame]（get_balance_sheet）
- fetch_equity_structure(code_list, b, e)→ dict[code, DataFrame]（get_equity_structure）
- fetch_restricted(code_list, begin, end)→ DataFrame（get_equity_restricted）
- fetch_adj_factors(code_list, b, e)     → DataFrame（get_adj_factor，日期×代码）
- fetch_kline(code_list, begin, end)     → dict[code, DataFrame]（query_kline，分块）
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional

import pandas as pd

from data_provider.fetchers.amazingdata_fetcher import (
    AMAZINGDATA_CACHE_DIR, AmazingDataFetcher, DataFetchError,
    _code_to_tgw_format,
)

logger = logging.getLogger(__name__)

_INFO_CHUNK = 32      # 小内存服务器：限制 SDK 单次宽表载荷
_KLINE_CHUNK = 200
_FACTOR_CHUNK = 500   # 单值日期矩阵，避免将共享因子缓存反复重读 80 多次

_FINANCIAL_COLUMNS = ("MARKET_CODE", "REPORTING_PERIOD", "ANN_DATE",
                      "ACTUAL_ANN_DATE", "STATEMENT_TYPE")
_DATE_COLUMNS = {
    "get_income": "REPORTING_PERIOD",
    "get_balance_sheet": "REPORTING_PERIOD",
    "get_equity_structure": "CHANGE_DATE",
    "get_history_stock_status": "TRADE_DATE",
    "get_equity_restricted": "LIST_DATE",
}


def _tgw_codes(codes: Iterable[str]) -> List[str]:
    mapped: List[str] = []
    for code in codes:
        tgw = _code_to_tgw_format(str(code))
        if tgw is None:
            logger.debug(f"[AmazingData] 跳过无法映射的代码 {code}")
            continue
        mapped.append(tgw)
    return mapped


def _chunks(seq: List[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _info():
    return AmazingDataFetcher.get_info_data()


def _info_call(method_name: str, codes: List[str], begin_date: str,
               end_date: str, columns: tuple):
    """按接口各自的日期含义查询；不混用缓存参数，不累积宽表。"""
    begin = int(pd.Timestamp(begin_date).strftime("%Y%m%d"))
    end = int(pd.Timestamp(end_date).strftime("%Y%m%d"))
    if begin > end:
        raise ValueError("begin_date 必须不晚于 end_date")
    info = _info()
    result: Dict[str, pd.DataFrame] = {}
    for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
        try:
            raw = getattr(info, method_name)(
                batch, begin_date=begin, end_date=end)
        except Exception as e:
            raise DataFetchError(f"AmazingData {method_name} 失败: {e}") from e
        normalized = _normalize_map(raw, batch)
        for code, df in normalized.items():
            date_column = _DATE_COLUMNS[method_name]
            if date_column not in df.columns:
                raise DataFetchError(f"AmazingData {method_name} 缺少日期列 {date_column}")
            dates = pd.to_datetime(df[date_column].astype(str), errors="coerce")
            in_window = dates.between(pd.Timestamp(begin_date), pd.Timestamp(end_date))
            compact = df.loc[in_window, [c for c in columns if c in df.columns]].copy()
            if compact.empty:
                continue
            if "MARKET_CODE" not in compact.columns:
                compact["MARKET_CODE"] = _code_to_tgw_format(code)
            if method_name == "get_equity_structure":
                # 股本查询需要覆盖历史，结果只留截止日前最新变动。
                latest = dates.loc[in_window].max()
                compact = compact.loc[dates.loc[in_window] == latest].tail(1).copy()
            result[code] = compact
        del raw, normalized
    return result


def _info_frame(method_name: str, codes: List[str], begin: str, end: str,
                columns: tuple) -> pd.DataFrame:
    frames = _info_call(method_name, codes, begin, end, columns)
    return pd.concat(frames.values(), ignore_index=True) if frames else pd.DataFrame()


def _plain_code(tgw_code: str) -> str:
    return tgw_code.split(".")[0]


def _normalize_map(raw, batch: List[str]) -> Dict[str, pd.DataFrame]:
    """SDK 返回形态归一：dict[code, df] 或单 DataFrame（含 MARKET_CODE 列）。"""
    out: Dict[str, pd.DataFrame] = {}
    requested = {_plain_code(c) for c in batch}
    if isinstance(raw, dict):
        for tgw, df in raw.items():
            if _plain_code(tgw) in requested and df is not None and len(df):
                out[_plain_code(tgw)] = df
    elif isinstance(raw, pd.DataFrame) and not raw.empty:
        if "MARKET_CODE" in raw.columns:
            for tgw in batch:
                part = raw[raw["MARKET_CODE"] == tgw]
                if len(part):
                    out[_plain_code(tgw)] = part
        else:
            logger.warning(f"[AmazingData] 返回单表但缺 MARKET_CODE 列，{len(raw)} 行被丢弃")
    return out


# ── 对外接口 ──

def fetch_stock_basics(codes: List[str]) -> pd.DataFrame:
    """get_stock_basic：代码/简称/上市板/上市日/退市日（无本地缓存，走服务端）。"""
    info = _info()
    frames = []
    for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
        try:
            raw = info.get_stock_basic(batch)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_stock_basic 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            frames.append(raw)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_status(codes: List[str], date: str) -> pd.DataFrame:
    """get_history_stock_status 按日快照：MARKET_CODE/TRADE_DATE/IS_ST_SEC/
    IS_SUSP_SEC/HIGH_LIMITED/LOW_LIMITED/PRECLOSE。begin_date/end_date 为 int。"""
    return _info_frame("get_history_stock_status", codes, date, date,
                       ("MARKET_CODE", "TRADE_DATE", "IS_ST_SEC", "IS_SUSP_SEC",
                        "PRECLOSE", "HIGH_LIMITED", "LOW_LIMITED"))


def fetch_income(codes: List[str], begin_date: str,
                 end_date: str) -> Dict[str, pd.DataFrame]:
    return _info_call("get_income", codes, begin_date, end_date,
                      _FINANCIAL_COLUMNS + ("NET_PRO_EXCL_MIN_INT_INC",))


def fetch_balance_sheets(codes: List[str], begin_date: str,
                         end_date: str) -> Dict[str, pd.DataFrame]:
    return _info_call("get_balance_sheet", codes, begin_date, end_date,
                      _FINANCIAL_COLUMNS + ("TOT_SHARE_EQUITY_EXCL_MIN_INT",))


def fetch_equity_structure(codes: List[str], begin_date: str,
                           end_date: str) -> Dict[str, pd.DataFrame]:
    return _info_call("get_equity_structure", codes, begin_date, end_date,
                      ("MARKET_CODE", "CHANGE_DATE", "TOT_SHARE"))


def fetch_restricted(codes: List[str], begin_date: str, end_date: str) -> pd.DataFrame:
    """get_equity_restricted：[begin, end] 内解禁事件明细（单表，含 MARKET_CODE）。"""
    return _info_frame("get_equity_restricted", codes, begin_date, end_date,
                       ("MARKET_CODE", "LIST_DATE"))


def fetch_adj_factors(codes: List[str], begin_date: str,
                      end_date: str) -> pd.DataFrame:
    """get_adj_factor 单次复权因子：index=交易日期，column=TGW 代码。

    仅作比例调整用（收益率/相对强弱对序列常数缩放不敏感），无需归一化。
    """
    base = AmazingDataFetcher.get_base_data()
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    frames = []
    for batch in _chunks(_tgw_codes(codes), _FACTOR_CHUNK):
        try:
            raw = base.get_adj_factor(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_adj_factor 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            raw = raw.copy(deep=False)
            raw.index = pd.to_datetime(raw.index).normalize()
            # _factor_series 对齐窗口；保留窗口起点之前一条供向前填充。
            before = raw.loc[raw.index < pd.Timestamp(begin_date)].tail(1)
            window = raw.loc[(raw.index >= pd.Timestamp(begin_date)) &
                             (raw.index <= pd.Timestamp(end_date))]
            frames.append(pd.concat([before, window]).copy())
        del raw
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def fetch_kline(codes: List[str], begin_date: str, end_date: str) -> Dict[str, pd.DataFrame]:
    """query_kline 日线批量（分块）：原始列 code/kline_time/open/high/low/close/
    volume/amount，未复权——复权由调用方配 fetch_adj_factors 自算。"""
    market = AmazingDataFetcher.ensure_login()
    from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher as F
    market = F._market_data
    if market is None:
        raise DataFetchError("AmazingData 未登录")
    from AmazingData.utils.constant import Period

    begin = int(begin_date.replace("-", ""))
    end = int(end_date.replace("-", ""))
    result: Dict[str, pd.DataFrame] = {}
    for batch in _chunks(_tgw_codes(codes), _KLINE_CHUNK):
        try:
            raw = market.query_kline(batch, begin_date=begin, end_date=end,
                                     period=Period.day.value)
        except Exception as e:
            raise DataFetchError(f"AmazingData query_kline 失败: {e}") from e
        if isinstance(raw, dict):
            for tgw, df in raw.items():
                if df is not None and len(df):
                    result[_plain_code(tgw)] = df
    return result

def fetch_code_list(security_type: str = "EXTRA_STOCK_A_SH_SZ") -> List[str]:
    """BaseData.get_code_list：沪深 A 股全集（6 位代码列表，兼容带后缀返回）。"""
    base = AmazingDataFetcher.get_base_data()
    try:
        raw = base.get_code_list(security_type)
    except Exception as e:
        raise DataFetchError(f"AmazingData get_code_list 失败: {e}") from e
    codes: List[str] = []
    if isinstance(raw, pd.DataFrame):
        col = next((c for c in ("MARKET_CODE", "code", "SECURITY_CODE", "symbol")
                    if c in raw.columns), None)
        seq = raw[col] if col else raw.iloc[:, 0]
    elif isinstance(raw, dict):
        seq = list(raw.keys())
    else:
        seq = list(raw)
    for item in seq:
        code = str(item).split(".")[0].strip()
        if code:
            codes.append(code)
    return codes
