"""行业基础表与带纳入/剔除日期的成员表；历史归属由调用方按时点装配。"""
import pandas as pd
from data_provider.types import DataFetchError
from .client import client
from ._utils import _temporary_sdk_cache
from ._utils import date_to_int

def _industry_call(method_name: str, *args):
    try:
        with _temporary_sdk_cache() as cache:
            return getattr(client.get_info_data(), method_name)(*args, local_path=cache, is_local=False)
    except Exception as e:
        raise DataFetchError(f"AmazingData {method_name} 失败: {e}") from e


def fetch_industry_base():
    """在线行业基础表；返回已载入内存的 SDK 表，临时缓存不留存。"""
    return _industry_call('get_industry_base_info')


def fetch_industry_members(index_code: str):
    """在线行业成员表（含纳入/剔除日期）；时点归属由调用方装配。"""
    return _industry_call('get_industry_constituent', [index_code])


def _frame(raw):
    if isinstance(raw, pd.DataFrame):
        return raw
    if isinstance(raw, dict):
        frames = [df for df in raw.values() if isinstance(df, pd.DataFrame) and not df.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    raise DataFetchError("AmazingData 行业接口返回类型无效")


def get_industry_class(asof):
    """按显式截止日生成申万一级行业表；冲突归属不返回。"""
    cutoff = pd.Timestamp(str(date_to_int(asof)))
    base = _frame(fetch_industry_base())
    if not {"INDEX_CODE", "LEVEL_TYPE"} <= set(base.columns):
        raise DataFetchError("AmazingData 行业基础表缺少代码或层级")
    level1 = base.loc[pd.to_numeric(base["LEVEL_TYPE"], errors="coerce").eq(1)]
    records = []
    for _, row in level1.iterrows():
        members = _frame(fetch_industry_members(row["INDEX_CODE"]))
        if members.empty:
            continue
        if not {"CON_CODE", "INDATE", "OUTDATE"} <= set(members.columns):
            raise DataFetchError("AmazingData 行业成员缺少代码或纳入/剔除日期")
        entered = pd.to_datetime(members["INDATE"].astype(str), errors="coerce")
        exited = pd.to_datetime(members["OUTDATE"].astype(str), errors="coerce")
        blank = members["OUTDATE"].isna() | members["OUTDATE"].astype(str).str.strip().eq("")
        active = (entered <= cutoff) & (blank | (exited > cutoff))
        for code in members.loc[active, "CON_CODE"].dropna().unique():
            records.append({"stock_code": code, "industry_code": row["INDEX_CODE"],
                            "industry_name": row.get("LEVEL1_NAME", row["INDEX_CODE"])})
    result = pd.DataFrame(records, columns=["stock_code", "industry_code", "industry_name"])
    conflicts = result.groupby("stock_code")["industry_code"].nunique()
    result = result.loc[~result["stock_code"].isin(conflicts[conflicts > 1].index)]
    return result.drop_duplicates("stock_code").set_index("stock_code")


def get_industry_dummies(stock_list, asof):
    industry = get_industry_class(asof)
    # 未知归属保留 NaN，调用方决定剔除或补齐，不伪装成全零行业。
    return pd.get_dummies(industry["industry_code"], dtype=float).reindex(stock_list)


def get_stock_industry_map(stock_list, asof):
    industry = get_industry_class(asof)["industry_code"]
    return {code: industry.get(code, "unknown") for code in stock_list}
