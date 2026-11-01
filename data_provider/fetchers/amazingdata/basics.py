"""交易日历、当前/历史代码表与股票基础信息。"""
from datetime import date
from typing import List, Optional
import pandas as pd
from data_provider.types import DataFetchError
from .client import client
from ._utils import (_chunks, _tgw_codes, _temporary_sdk_cache, date_range, date_to_int)


def get_calendar(market="SH", data_type="datetime"):
    if data_type not in {"datetime", "int", "str"}:
        raise ValueError("data_type 必须为 datetime、int 或 str")
    calendar = sorted(date_to_int(d) for d in client.get_base_data().get_calendar(
        data_type="str", market=market))
    if data_type == "int":
        return calendar
    if data_type == "str":
        return [str(d) for d in calendar]
    return [pd.Timestamp(str(d)) for d in calendar]


def _code_list(raw):
    if isinstance(raw, pd.DataFrame):
        if raw.empty:
            return []
        col = next((c for c in ("MARKET_CODE", "code", "SECURITY_CODE", "symbol")
                    if c in raw.columns), raw.columns[0])
        seq = raw[col]
    elif isinstance(raw, dict):
        seq = raw.keys()
    else:
        seq = raw if raw is not None else []
    return [str(code).strip() for code in seq if pd.notna(code) and str(code).strip()]


def get_code_list(security_type="EXTRA_STOCK_A_SH_SZ"):
    """SDK 代码格式（含市场后缀）；每次在线查询，不将现时名单当成历史名单。"""
    try:
        return _code_list(client.get_base_data().get_code_list(security_type))
    except Exception as exc:
        raise DataFetchError(f"AmazingData get_code_list 失败: {exc}") from exc


def get_hist_code_list(security_type="EXTRA_STOCK_A_SH_SZ", start_date=20130101,
                       end_date=None):
    begin, end = date_range(start_date, date.today() if end_date is None else end_date)
    try:
        base = client.get_base_data()
        with _temporary_sdk_cache() as cache:
            return _code_list(base.get_hist_code_list(
                security_type, start_date=begin, end_date=end, local_path=cache))
    except Exception as exc:
        raise DataFetchError(f"AmazingData get_hist_code_list 失败: {exc}") from exc


def get_stock_list():
    return get_code_list("EXTRA_STOCK_A_SH_SZ")


def get_fund_list():
    return get_code_list("EXTRA_ETF")


def get_bond_list():
    return get_code_list("EXTRA_KZZ")


def fetch_code_list(security_type="EXTRA_STOCK_A_SH_SZ", asof=None):
    """策略接口：返回 6 位代码；asof 指定时只查当日历史名单。"""
    raw = get_code_list(security_type) if asof is None else get_hist_code_list(
        security_type, asof, asof)
    return [code.split(".")[0] for code in raw]

def fetch_stock_basics(codes: List[str]) -> pd.DataFrame:
    """get_stock_basic：代码/简称/上市板/上市日/退市日（无本地缓存，走服务端）。"""
    info = client.get_info_data()
    frames = []
    for batch in _chunks(_tgw_codes(codes), 32):
        try:
            raw = info.get_stock_basic(batch)
        except Exception as e:
            raise DataFetchError(f"AmazingData get_stock_basic 失败: {e}") from e
        if isinstance(raw, pd.DataFrame) and not raw.empty:
            frames.append(raw)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
