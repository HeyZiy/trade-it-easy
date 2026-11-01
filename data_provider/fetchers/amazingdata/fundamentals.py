"""财务、股本、证券状态与解禁的批量查询；按接口日期含义裁剪，保留策略所需字段。"""
from typing import Dict, List
import pandas as pd
from data_provider.types import DataFetchError
from .client import client
from ._utils import (_chunks, _tgw_codes, _normalize_map, _code_to_tgw_format,
                     _plain_code, date_range)

_INFO_CHUNK = 32
_FINANCIAL_COLUMNS = ("MARKET_CODE", "REPORTING_PERIOD", "ANN_DATE",
                      "ACTUAL_ANN_DATE", "STATEMENT_TYPE")
_DATE_COLUMNS = {
    "get_income": "REPORTING_PERIOD", "get_balance_sheet": "REPORTING_PERIOD",
    "get_equity_structure": "CHANGE_DATE", "get_history_stock_status": "TRADE_DATE",
    "get_equity_restricted": "LIST_DATE",
}

def _info_call(method_name: str, codes: List[str], begin_date: str,
               end_date: str, columns: tuple, *, latest_equity: bool = True):
    """按接口各自的日期含义查询；不混用缓存参数，不累积宽表。"""
    begin, end = date_range(begin_date, end_date)
    info = client.get_info_data()
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
            in_window = dates.between(pd.Timestamp(str(begin)), pd.Timestamp(str(end)))
            compact = df.loc[in_window, [c for c in columns if c in df.columns]].copy()
            if compact.empty:
                continue
            if "MARKET_CODE" not in compact.columns:
                compact["MARKET_CODE"] = _code_to_tgw_format(code)
            if method_name == "get_equity_structure" and latest_equity:
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


def fetch_status(codes: List[str], date: str) -> pd.DataFrame:
    """get_history_stock_status 按日快照：MARKET_CODE/TRADE_DATE/IS_ST_SEC/
    IS_SUSP_SEC/HIGH_LIMITED/LOW_LIMITED/PRECLOSE。begin_date/end_date 为 int。"""
    return _info_frame("get_history_stock_status", codes, date, date,
                       ("MARKET_CODE", "TRADE_DATE", "IS_ST_SEC", "IS_SUSP_SEC",
                        "PRECLOSE", "HIGH_LIMITED", "LOW_LIMITED"))


def fetch_income(codes: List[str], begin_date: str,
                 end_date: str) -> Dict[str, pd.DataFrame]:
    return _info_call("get_income", codes, begin_date, end_date,
                      _FINANCIAL_COLUMNS + ("NET_PRO_EXCL_MIN_INT_INC",
                                            "NET_PRO_INCL_MIN_INT_INC"))


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


def _market_value(stock_list, begin_date, end_date, share_field):
    """未复权价格×历史股本；按变动日对齐，有公告日时同时限制可用时间。"""
    from .market import get_close_price

    begin, end = date_range(begin_date, end_date)
    closes = get_close_price(stock_list, begin, end)
    if closes.empty:
        return closes
    history = _info_call(
        "get_equity_structure", stock_list, "1990-01-01", str(end),
        ("MARKET_CODE", "CHANGE_DATE", "ANN_DATE", share_field),
        latest_equity=False)
    result = pd.DataFrame(float("nan"), index=closes.index, columns=closes.columns)
    for code in closes.columns:
        equity = history.get(_plain_code(code))
        if equity is None or share_field not in equity:
            continue
        change = pd.to_datetime(equity["CHANGE_DATE"].astype(str), errors="coerce")
        valid = change.notna()
        effective = change
        if "ANN_DATE" in equity:
            raw_ann = equity["ANN_DATE"]
            announcement = pd.to_datetime(raw_ann.astype(str), errors="coerce")
            blank = raw_ann.isna() | raw_ann.astype(str).str.strip().eq("")
            valid &= blank | announcement.notna()
            effective = pd.concat([change, announcement], axis=1).max(axis=1)
        shares = pd.to_numeric(equity[share_field], errors="coerce")
        frame = pd.DataFrame({"date": effective, "change": change, "shares": shares})
        frame = frame.loc[valid].sort_values(["date", "change"], kind="stable")
        # 晚到的旧股本事件不能覆盖已知的更新股本。
        frame = frame.loc[frame["change"].eq(frame["change"].cummax())]
        frame = frame.drop_duplicates("date", keep="last")
        if frame.empty:
            continue
        # 不向历史起点回填未来股本，也不以总股本替代流通股本。
        aligned = frame.set_index("date")["shares"].reindex(closes.index, method="ffill")
        result[code] = closes[code] * aligned.where(aligned > 0) * 10000
    return result


def get_float_market_value(stock_list, begin_date, end_date):
    return _market_value(stock_list, begin_date, end_date, "FLOAT_A_SHARE")


def get_total_market_value(stock_list, begin_date, end_date):
    return _market_value(stock_list, begin_date, end_date, "TOT_SHARE")
