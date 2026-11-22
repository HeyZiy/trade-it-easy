# -*- coding: utf-8 -*-
"""
===================================
AmazingData 因子封装层（市场观察周报专用）
===================================

从星耀数智 AmazingData 平台获取市场观察周报所需的因子数据：
- 全市场聚合 PE（31 个申万一级行业市值加权，含近 5 年分位）
- 10 年期国债收益率（股债性价比：风险溢价及其自身历史分位）

设计原则：
- 所有函数失败均返回 None/空值，绝不抛出异常 —— 调用方据此降级回旧逻辑
- 懒登录，复用 data_provider.fetchers.amazingdata.client 的共享会话
- 未配置 TGW 凭证时自动失效（返回 None）
"""

import logging
from typing import Dict, List, Optional
import pandas as pd

logger = logging.getLogger(__name__)

# AmazingData 本地缓存目录（InfoData 接口用 HDF5 缓存，需 pytables）
# 复用 fetcher 的解析单点：相对路径按项目根解析 + 兜底 data/AmazingData_local_data
from data_provider.fetchers.amazingdata._utils import _resolve_cache_dir

LOCAL_DATA_DIR = _resolve_cache_dir()

# 一级行业 PE/PB 分位回看窗口（5 年 ≈ 1250 交易日）
PERCENTILE_LOOKBACK = 1250


def _info() -> Optional[object]:
    """获取 InfoData 实例，失败返回 None。"""
    try:
        from data_provider.fetchers.amazingdata.client import client

        return client.get_info_data()
    except Exception as e:
        logger.debug(f"AmazingData 不可用（降级）: {e}")
        return None


def _is_available() -> bool:
    """AmazingData 是否可用（TGW 凭证是否配置）。"""
    try:
        from data_provider.fetchers.amazingdata.client import is_configured as tgw_configured

        return tgw_configured()
    except Exception:
        return False


# ── 行业基础数据 ──

_industry_base_cache: Optional[pd.DataFrame] = None


def get_industry_base() -> Optional[pd.DataFrame]:
    """
    获取申万行业指数基本信息（INDEX_CODE / LEVEL1_NAME / LEVEL2_NAME / LEVEL3_NAME）。

    Returns:
        DataFrame 或 None（降级）
    """
    global _industry_base_cache
    if _industry_base_cache is not None:
        return _industry_base_cache

    info = _info()
    if info is None:
        return None
    try:
        df = info.get_industry_base_info(local_path=LOCAL_DATA_DIR, is_local=False)
        if df is None or df.empty:
            return None
        _industry_base_cache = df
        return df
    except Exception as e:
        logger.warning(f"获取行业指数基础信息失败: {e}")
        return None


def get_level1_industries() -> List[dict]:
    """
    获取申万一级行业列表。

    Returns:
        [{'code': '801180.SI', 'name': '非银金融'}, ...] 或 []
    """
    base = get_industry_base()
    if base is None:
        return []
    try:
        # LEVEL_TYPE=1 为一级行业；个别字段可能缺失，做兼容
        col_type = "LEVEL_TYPE" if "LEVEL_TYPE" in base.columns else None
        col_name = "LEVEL1_NAME" if "LEVEL1_NAME" in base.columns else None
        if col_type is None or col_name is None:
            return []
        lvl1 = base[base[col_type] == 1]
        seen = {}
        for _, row in lvl1.iterrows():
            name = str(row.get(col_name, "")).strip()
            code = str(row.get("INDEX_CODE", "")).strip()
            if name and code and name not in seen:
                seen[name] = code
        return [{"code": c, "name": n} for n, c in seen.items()]
    except Exception as e:
        logger.warning(f"解析一级行业列表失败: {e}")
        return []


# ── 行业日线（含 PE/PB/市值） ──

_industry_daily_cache: Dict[str, pd.DataFrame] = {}


def get_industry_daily(code: str) -> Optional[pd.DataFrame]:
    """
    获取单个行业指数的日线数据（含 PE/PB/总市值/流通市值）。

    Returns:
        DataFrame（升序，列含 CLOSE/PE/PB/TOTAL_CAP/A_FLOAT_CAP）或 None
    """
    if code in _industry_daily_cache:
        return _industry_daily_cache[code]

    info = _info()
    if info is None:
        return None
    try:
        daily = info.get_industry_daily([code], local_path=LOCAL_DATA_DIR, is_local=False)
        df = daily.get(code)
        if df is None or df.empty:
            return None
        df = df.sort_index().dropna(subset=["CLOSE"])
        _industry_daily_cache[code] = df
        return df
    except Exception as e:
        logger.warning(f"获取行业日线 {code} 失败: {e}")
        return None


# ── 全市场聚合 PE（市场级口径：报告第一节概览 + 第三节新钱参考） ──

def _market_pe_series() -> Optional[pd.Series]:
    """全市场聚合 PE 日度序列（31 个申万一级行业 E/P 加权），供分位与 ERP 共用。

    加权口径：PE_market = Σ市值 / Σ(市值/PE)，避免高 PE 行业被市值权重过分放大。
    """
    industries = get_level1_industries()
    if not industries:
        return None

    # 逐行业拉日线，对齐交易日索引
    cap_dfs = []
    pe_dfs = []
    for item in industries:
        df = get_industry_daily(item["code"])
        if df is None or "PE" not in df.columns or "TOTAL_CAP" not in df.columns:
            continue
        cap = pd.to_numeric(df["TOTAL_CAP"], errors="coerce")
        pe = pd.to_numeric(df["PE"], errors="coerce")
        # 过滤无效 PE 与市值
        mask = (pe > 0) & (cap > 0)
        cap_dfs.append(cap[mask])
        pe_dfs.append(pe[mask])
    if not cap_dfs:
        return None

    total_cap = pd.concat(cap_dfs, axis=1).sum(axis=1, skipna=False)
    # Σ(市值/PE) 按行业加权
    weighted = []
    for item, cap, pe in zip(industries, cap_dfs, pe_dfs):
        valid = cap.notna() & pe.notna() & (pe > 0) & (cap > 0)
        weighted.append((cap[valid] / pe[valid]).reindex(cap.index))
    sum_ep = pd.concat(weighted, axis=1).sum(axis=1, skipna=False)

    market_pe = total_cap / sum_ep
    return market_pe[market_pe > 0].dropna()


def _as_daily_series(s: pd.Series) -> pd.Series:
    """把任意日期索引的序列统一为按天归一的 DatetimeIndex（索引解析失败的行丢弃）。

    行业日线与国债历史的索引口径可能不同（datetime/字符串/yyyymmdd 整数），
    统一走字符串解析，保证跨序列可对齐。
    """
    s = s.sort_index()
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index.astype(str), errors="coerce")
    s = s[s.index.notna()]
    return s.groupby(s.index).last()


def get_market_pe(lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """
    用 31 个申万一级行业 PE 市值加权聚合出全市场 PE 及历史分位（默认近 5 年）。

    Returns:
        {'pe': 当前PE, 'pe_pct': 分位(0-100)} 或 None
    """
    series = _market_pe_series()
    if series is None:
        return None
    recent = series.tail(lookback)
    if len(recent) < 250:
        return None
    current = float(recent.iloc[-1])
    pct = float((recent <= current).mean() * 100)
    return {"pe": round(current, 2), "pe_pct": round(pct, 1)}


_erp_percentile_cache: Optional[dict] = None


def get_erp_percentile(lookback: int = PERCENTILE_LOOKBACK) -> Optional[dict]:
    """
    股权风险溢价（1/全市场PE − 10 年国债）的当前值与自身历史分位（默认近 5 年）。

    与 get_market_pe 同序列、同窗口，两个口径可比、互相印证：PE 分位受回看窗口
    锚定影响大（窗口含深熊底部则普遍读高），ERP 分位是跨资产口径的独立参照。

    Returns:
        {'erp': 当前风险溢价(%), 'erp_pct': 分位(0-100)} 或 None（国债历史不可用/对不齐时降级）
    """
    global _erp_percentile_cache
    if _erp_percentile_cache is not None:
        return _erp_percentile_cache

    pe = _market_pe_series()
    if pe is None:
        return None
    recent_pe = pe.tail(lookback)
    if len(recent_pe) < 250:
        return None

    info = _info()
    if info is None:
        return None
    try:
        ty = info.get_treasury_yield(["y10"], local_path=LOCAL_DATA_DIR, is_local=False)
        df = ty.get("y10") if ty else None
        if df is None or df.empty or "YIELD" not in df.columns:
            return None
        y10 = _as_daily_series(pd.to_numeric(df["YIELD"], errors="coerce").dropna())
        if y10.empty:
            return None
        pe_d = _as_daily_series(recent_pe)
        erp = (100.0 / pe_d - y10.reindex(pe_d.index)).dropna()
        if len(erp) < 250:
            return None
        current = float(erp.iloc[-1])
        pct = float((erp <= current).mean() * 100)
        _erp_percentile_cache = {"erp": round(current, 2), "erp_pct": round(pct, 1)}
        return _erp_percentile_cache
    except Exception as e:
        logger.warning(f"计算风险溢价分位失败: {e}")
        return None
