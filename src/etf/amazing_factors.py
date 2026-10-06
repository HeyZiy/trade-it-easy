# -*- coding: utf-8 -*-
"""
===================================
AmazingData 因子封装层（ETF 周度观察专用）
===================================

从星耀数智 AmazingData 平台获取 ETF 周度观察所需的因子数据：
- 行业 PE/PB 历史分位（申万一级行业，2000 年至今）
- 行业市值占比（拥挤度因子）
- 10 年期国债收益率（股债性价比：风险溢价及其自身历史分位）
- 中证指数官网估值（核心仓跟踪指数自身 PE/股息率，本地滚动累积历史供分位计算）

设计原则：
- 所有函数失败均返回 None/空值，绝不抛出异常 —— 调用方据此降级回旧逻辑
- 懒登录，复用 data_provider.amazingdata_fetcher 的登录态（单进程一次登录）
- 未配置 TGW 凭证时自动失效（返回 None）
"""

import logging
from typing import Dict, List, Optional

import pandas as pd

from src.etf.config import TRACKED_INDEX, DIVIDEND_STYLE_CODES

logger = logging.getLogger(__name__)

# AmazingData 本地缓存目录（InfoData 接口用 HDF5 缓存，需 pytables）
# 复用 fetcher 的解析单点：相对路径按项目根解析 + 兜底 data/AmazingData_local_data
from data_provider.fetchers.amazingdata_fetcher import _resolve_cache_dir

LOCAL_DATA_DIR = _resolve_cache_dir()

# 一级行业 PE/PB 分位回看窗口（5 年 ≈ 1250 交易日）
PERCENTILE_LOOKBACK = 1250


def _info() -> Optional[object]:
    """获取 InfoData 实例，失败返回 None。"""
    try:
        from data_provider.fetchers.amazingdata_fetcher import AmazingDataFetcher

        return AmazingDataFetcher.get_info_data()
    except Exception as e:
        logger.debug(f"AmazingData 不可用（降级）: {e}")
        return None


def _is_available() -> bool:
    """AmazingData 是否可用（TGW 凭证是否配置）。"""
    try:
        from data_provider.fetchers.amazingdata_fetcher import tgw_configured

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
        df = info.get_industry_base_info()
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


# ── 因子计算 ──

def get_etf_share_flow(codes: List[str], lookback_days: int = 20) -> Dict[str, dict]:
    """
    ETF 份额变化率（资金流观察字段）：份额增减 = 场内申赎方向。

    Args:
        codes: 标准 6 位 ETF 代码列表
        lookback_days: 变化率窗口（自然交易日）

    Returns:
        {code: {'share': 最新总份额(万份), 'chg': 近 N 日变化率(%)}}；缺数据的代码不在结果中
    """
    info = _info()
    if info is None or not codes:
        return {}
    try:
        from data_provider.fetchers.amazingdata_fetcher import _code_to_tgw_format
        tgw_codes = [c for c in (_code_to_tgw_format(x) for x in codes) if c]
        if not tgw_codes:
            return {}
        data = info.get_fund_share(tgw_codes, local_path=LOCAL_DATA_DIR, is_local=False)
        result: Dict[str, dict] = {}
        for tgw_code, df in (data or {}).items():
            if df is None or df.empty:
                continue
            col = "TOTAL_SHARE" if "TOTAL_SHARE" in df.columns else "FUND_SHARE"
            if col not in df.columns:
                continue
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) < 2:
                continue
            chg = None
            if len(s) >= lookback_days + 1:
                base = float(s.iloc[-1 - lookback_days])
                if base > 0:
                    ratio = float(s.iloc[-1]) / base
                    # 超 50 倍的跳变视为口径切换（真实申购潮极少超此量级）
                    if 0.02 < ratio < 50:
                        chg = round((ratio - 1) * 100, 1)
            result[str(tgw_code).split(".")[0]] = {"share": round(float(s.iloc[-1]), 0), "chg": chg}
        return result
    except Exception as e:
        logger.warning(f"获取 ETF 份额流失败: {e}")
        return {}


def get_treasury_yield_y10() -> Optional[float]:
    """
    获取 10 年期国债收益率（%）。

    Returns:
        float（如 2.13）或 None
    """
    info = _info()
    if info is None:
        return None
    try:
        ty = info.get_treasury_yield(["y10"], local_path=LOCAL_DATA_DIR, is_local=False)
        df = ty.get("y10")
        if df is None or df.empty or "YIELD" not in df.columns:
            return None
        val = float(pd.to_numeric(df["YIELD"], errors="coerce").dropna().iloc[-1])
        return val
    except Exception as e:
        logger.warning(f"获取 10 年期国债收益率失败: {e}")
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


# ── 中证指数官网估值（跟踪指数当前 PE/股息率） ──

# 官网 indicator 文件表头为中英双语（如 "日期Date"），按列位置解析
_CSINDEX_COLS = ["date", "index_code", "index_name", "index_short",
                 "en_full", "en_short", "pe", "pe2", "dy", "dy2"]
_csindex_cache: Dict[str, Optional[dict]] = {}


def _fetch_csindex_indicator(index_code: str) -> Optional[pd.DataFrame]:
    """下载中证官网估值明细（仅含近 20 个交易日）。"""
    url = (f"https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/"
           f"indicator/{index_code}indicator.xls")
    try:
        df = pd.read_excel(url)
        if df.shape[1] != len(_CSINDEX_COLS):
            logger.warning(f"csindex 估值文件列数异常 {index_code}: {df.shape[1]}")
            return None
        df.columns = _CSINDEX_COLS
        df = df[["date", "index_code", "index_name", "pe", "dy"]].copy()
        df["date"] = df["date"].astype(str)
        # 指数代码列会被读成整数（000922 → 922），补齐前导零
        df["index_code"] = df["index_code"].astype(str).str.zfill(6)
        for col in ("pe", "dy"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["pe"])
        return df if not df.empty else None
    except Exception as e:
        logger.warning(f"csindex 估值下载失败 {index_code}: {e}")
        return None


def get_csindex_valuation(index_code: str) -> Optional[dict]:
    """
    跟踪指数自身估值的当前值（中证官网 PE/股息率，取最新一日）。

    决策记录（2026-09）：不做历史分位积累。官网无历史文件、自建积累需约一年冷启动，
    而逐指数分位唯一用途（新钱估值加速器）与趋势主规则、全局便宜档（全市场 PE 分位）
    高度重叠，收益不抵维护成本。"便宜"判定收归两层：全局节奏（全市场口径）+
    红利类股息率利差（跨资产口径，零历史依赖）。

    Returns:
        {'index_code','index_name','asof','pe','dy'} 或 None。dy = 股息率1（总股本口径）。
    """
    if index_code in _csindex_cache:
        return _csindex_cache[index_code]

    df = _fetch_csindex_indicator(index_code)
    result = None
    if df is not None:
        latest = df.iloc[0]  # 官网明细按日期降序，首行为最新
        result = {
            "index_code": str(latest["index_code"]),
            "index_name": str(latest["index_name"]),
            "asof": str(latest["date"]),
            "pe": round(float(latest["pe"]), 2),
            "dy": round(float(latest["dy"]), 2) if pd.notna(latest["dy"]) else None,
        }
    if result is not None:
        _csindex_cache[index_code] = result   # 失败不缓存：一次抖动不该锁死整轮取数
    return result


# ── ETF 估值参考 ──

# 海外 ETF（无 csindex 数据，标"海外"）
_OVERSEAS_CODES = frozenset({"513100", "513500", "513380"})


def _etf_pe_info(etf_code: str) -> Optional[dict]:
    """单只 ETF 的估值信息——唯一口径 = 跟踪指数自身估值（TRACKED_INDEX → csindex）。

    不做兜底：未配跟踪指数锚、或中证官网未取到时返回 None，由消费方显示"无估值锚"。
    跨口径的行业 PE / 全市场 PE 分位与标的自身不可比，历史上作为兜底会产出假的
    优先级与警示，已收归市场级口径（get_market_pe，仅报告第一/三节使用）。
    csindex 不提供分位（决策记录见 get_csindex_valuation），故返回值无 pe_pct。
    """
    code = str(etf_code).zfill(6)

    if code in _OVERSEAS_CODES:
        return {"source_type": "overseas", "pe": None, "source_name": "海外"}

    index_code = TRACKED_INDEX.get(code)
    if not index_code:
        return None
    val = get_csindex_valuation(index_code)
    if not val:
        return None

    info = {
        "pe": val.get("pe"),
        "source_type": "csindex",
        "source_name": val.get("index_name") or index_code,
    }
    # 股息率与利差（股息率 − 10Y 国债，跨资产口径，零历史依赖）
    if val.get("dy") is not None:
        info["div_yield"] = val["dy"]
        y10 = get_treasury_yield_y10()
        if y10:
            info["div_yield_spread"] = round(val["dy"] - y10, 2)
    return info


def etf_valuation_rows(etf_list) -> list:
    """各核心 ETF 的估值参考行（锚=跟踪指数当前 PE/股息率；跨标的不可比，仅展示）。"""
    results = []
    for etf in etf_list:
        info = _etf_pe_info(etf.code)
        level_text = ""
        if info is None:
            level_text = "无估值锚（未配跟踪指数或中证官网未取到）"
        elif str(etf.code).zfill(6) in DIVIDEND_STYLE_CODES:
            if info.get("div_yield_spread") is not None:
                level_text = f"利差 {info['div_yield_spread']:+.1f}pt（≥1.5 视同低估）"
            elif info.get("div_yield") is not None:
                level_text = "股息率口径"
        results.append({
            "code": etf.code, "name": etf.name,
            "pe": info.get("pe") if info else None,
            "div_yield": info.get("div_yield") if info else None,
            "div_yield_spread": info.get("div_yield_spread") if info else None,
            "source_name": info.get("source_name", "") if info else "",
            "level_text": level_text,
        })
    return results
