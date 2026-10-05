# -*- coding: utf-8 -*-
"""
===================================
AmazingData 批量 InfoData/BaseData 包装 — 质量池数据层专用
===================================

质量池策略需要的全市场批量接口薄包装（登录态与本地 HDF5 缓存复用
amazingdata_fetcher 的单点）。签名与手册一致（docs/xysz-master/xysz/
AmazingData开发手册.md），入参一律 6 位代码，内部转 TGW 格式。

约定：与既有可用组合（_fetch_float_shares_series / amazing_factors）同款——
local_path + is_local=False（服务端取全量并更新本地缓存），不传日期过滤
（那是本地缓存模式二选一的参数组）。取回结果已含全量历史，下游自截窗口。

接口清单（返回均已做基础规整）：
- fetch_stock_basics(code_list)          → DataFrame（get_stock_basic，无本地缓存）
- fetch_status(code_list, date)          → DataFrame（get_history_stock_status，按日）
- fetch_income(code_list)                → dict[code, DataFrame]（get_income）
- fetch_balance_sheets(code_list)        → dict[code, DataFrame]（get_balance_sheet）
- fetch_equity_structure(code_list)      → dict[code, DataFrame]（get_equity_structure）
- fetch_restricted(code_list, begin, end)→ DataFrame（get_equity_restricted）
- fetch_adj_factors(code_list)           → DataFrame（get_adj_factor，日期×代码）
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

_INFO_CHUNK = 500     # InfoData 全市场全量历史，按批调用防单次载荷过大
_KLINE_CHUNK = 200


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


def _info_call(method_name: str, codes: List[str], chunk: int = _INFO_CHUNK):
    """InfoData 批量调用公共壳：分块 → 服务端取全量（更新本地缓存）。

    返回 {6位代码: DataFrame}；SDK 返回 dict 直接用，返回单表则按 MARKET_CODE 拆。
    """
    info = _info()
    result: Dict[str, pd.DataFrame] = {}
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    for batch in _chunks(_tgw_codes(codes), chunk):
        try:
            raw = getattr(info, method_name)(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False)
        except Exception as e:
            raise DataFetchError(f"AmazingData {method_name} 失败: {e}") from e
        result.update(_normalize_map(raw, batch))
    return result


def _plain_code(tgw_code: str) -> str:
    return tgw_code.split(".")[0]


def _normalize_map(raw, batch: List[str]) -> Dict[str, pd.DataFrame]:
    """SDK 返回形态归一：dict[code, df] 或单 DataFrame（含 MARKET_CODE 列）。"""
    out: Dict[str, pd.DataFrame] = {}
    if isinstance(raw, dict):
        for tgw, df in raw.items():
            if df is not None and len(df):
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
    info = _info()
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    day = int(date.replace("-", ""))
    frames = []
    for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
        try:
            raw = info.get_history_stock_status(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False,
                begin_date=day, end_date=day)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_history_stock_status 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            frames.append(raw)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_income(codes: List[str]) -> Dict[str, pd.DataFrame]:
    return _info_call("get_income", codes)


def fetch_balance_sheets(codes: List[str]) -> Dict[str, pd.DataFrame]:
    return _info_call("get_balance_sheet", codes)


def fetch_equity_structure(codes: List[str]) -> Dict[str, pd.DataFrame]:
    return _info_call("get_equity_structure", codes)


def fetch_restricted(codes: List[str], begin_date: str, end_date: str) -> pd.DataFrame:
    """get_equity_restricted：[begin, end] 内解禁事件明细（单表，含 MARKET_CODE）。"""
    info = _info()
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    frames = []
    for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
        try:
            raw = info.get_equity_restricted(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False,
                begin_date=int(begin_date.replace("-", "")),
                end_date=int(end_date.replace("-", "")))
        except Exception as e:
            raise DataFetchError(f"AmazingData get_equity_restricted 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            frames.append(raw)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_adj_factors(codes: List[str]) -> pd.DataFrame:
    """get_adj_factor 单次复权因子：index=交易日期，column=TGW 代码。

    仅作比例调整用（收益率/相对强弱对序列常数缩放不敏感），无需归一化。
    """
    base = AmazingDataFetcher.get_base_data()
    os.makedirs(AMAZINGDATA_CACHE_DIR, exist_ok=True)
    frames = []
    for batch in _chunks(_tgw_codes(codes), _INFO_CHUNK):
        try:
            raw = base.get_adj_factor(
                batch, local_path=AMAZINGDATA_CACHE_DIR, is_local=False)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_adj_factor 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            frames.append(raw)
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
