# -*- coding: utf-8 -*-
"""
===================================
日线取数（ETF / A股指数）
===================================

项目 ETF 与 A股指数日线的唯一取数入口，替代各模块散落的 akshare 直连
（rebalancer / market_gate /
data_collector / momentum_check 此前各自拼 sh/sz 前缀、各自选接口）。

设计原则：
- 函数名即类型：get_etf_daily / get_index_daily 两个入口，调用方自己声明要什么，
  不做"从裸代码推断标的类型"（000 家族天生歧义，推断必然出错）。
- 市场前缀显式：sh/sz 前缀优先；裸码仅按确定规则推断（ETF 码族→市场无歧义；
  指数 399→sz、其余→sh），规则写死在各自函数内，不扩散。
- 源选择（单源为主；指数三腿是"互斥代码族路由 + 一次网络保底"，不是 failover）：
    ETF(51/52/53/55/56/58/15/16/18) → 新浪 fund_etf_hist_sina（带 amount；
      未复权原始价；份额折算前复权 adjust="qfq"，单点 adjust_series）
    A股指数(000/399)          → 中证官网 csindex（000 权威源）
                                → 东财 index_zh_a_hist（399 深证/国证不在 csindex）
                                → 新浪 stock_zh_index_daily（东财不可达时保底；
                                  000 行业指数可能停更，仅前两者失败触发）
- 个股不在本模块范围：项目个股走 DataFetcherManager（AmazingData>Tushare 多源
  + 实时报价合并）。传个股进来会明确拒绝，避免静默降级 AmazingData 优先级。
- 全部归一化到 ['date','open','high','low','close','volume']，date 为 'YYYY-MM-DD' 字符串。
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from .codes import _split_prefix, is_etf_code, market_suffix

logger = logging.getLogger(__name__)

# 归一化输出列（与下游 analyze_etf / _score_etf 假定一致）
STD_COLS = ["date", "open", "high", "low", "close", "volume"]


def _etf_sym(code: str) -> str:
    """ETF 代码 → 新浪带市场前缀符号。显式前缀优先；裸码按 codes.market_suffix 判市场（码族外抛错）。"""
    num, pref = _split_prefix(code)
    if pref:
        return pref + num
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


# 份额折算/拆分前复权（复权口径单点；「原始价含份额折算须前复权」的落点）
SPLIT_JUMP = 0.25               # 单日 |ret|>25% 视为份额折算/拆分（涨跌停 ±10/20%，不可能到达）


def adjust_series(close: pd.Series) -> pd.Series:
    """份额折算/拆分前复权（研究线 adjust_splits 单列版）。

    单日 |ret| > SPLIT_JUMP 视为折算/合并：factor = cur/prev，此前价格全乘
    factor（连续化），多次折算按时间顺序累积即前复权到样本末。amount 为元
    口径不调整，与本函数无关。
    """
    s = close.dropna().astype(float)
    if len(s) < 2:
        return close
    out = s.copy()
    r = s.pct_change()
    for d in r[r.abs() > SPLIT_JUMP].index:
        i = s.index.get_loc(d)
        factor = float(s.at[d]) / float(s.iloc[i - 1])
        out.loc[out.index < d] *= factor
    return out


def get_etf_research_daily(code: str, end_date: str) -> pd.DataFrame:
    """研究日线：东财原始 OHLC + 供应商后复权 OHLC，禁止价格跳变猜复权。

    价格两条线来自同一供应商；成交额为元，成交量由手转换为份。
    返回失败时抛错，让构建方保留旧缓存并记录失败，不能回退到猜测复权。
    """
    import akshare as ak

    if not is_etf_code(code):
        raise ValueError(f"{code} 不是受支持的 ETF 代码")
    symbol = _etf_sym(code)[2:]
    rename = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
              "收盘": "close", "成交量": "volume", "成交额": "amount"}
    tables = []
    for adjustment in ("", "hfq"):
        df = ak.fund_etf_hist_em(symbol=symbol, period="daily", start_date="19900101",
                                 end_date=end_date.replace("-", ""), adjust=adjustment)
        if df is None or df.empty:
            raise ValueError(f"{code} 东财 {adjustment or 'raw'} 行情为空")
        df = df.rename(columns=rename)
        df["date"] = pd.to_datetime(df["date"], errors="raise").dt.strftime("%Y-%m-%d")
        if df["date"].duplicated().any():
            raise ValueError(f"{code} 东财行情日期重复")
        if adjustment:
            df = df[["date", "open", "high", "low", "close"]].rename(
                columns={f: f"hfq_{f}" for f in ("open", "high", "low", "close")})
        else:
            df = df[["date", "open", "high", "low", "close", "volume", "amount"]].copy()
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100
        tables.append(df)
    return tables[0].merge(tables[1], on="date", how="left", validate="one_to_one").sort_values("date")


def get_etf_daily(code: str, *, adjust: Optional[str] = None) -> Optional[pd.DataFrame]:
    """ETF 日线全历史（新浪 fund_etf_hist_sina 单源，带 amount 成交额）。

    统一入口（三路合一）：池分数线（momentum）、observe/台账读侧现价补口
    都走这里。返回形状唯一：date 列升序，
    columns=['date','open','high','low','close','volume','amount']；
    空/失败返回 None；非 ETF 码族拒绝（个股走 DataFetcherManager）。

    adjust 口径（复权单点，勿在调用方自行折算）：
    - None（默认）：未复权原始价。现货价口径用这个——折算后的最新价本就是现价；
    - "qfq"：份额折算/拆分前复权（见 adjust_series）。历史比值口径
      （强弱分数等）必须用这个——原始价含份额折算会伪造假跳水。

    amount 为元口径，不参与复权（与东财逐值一致，研究线实测核对过）。
    """
    if not is_etf_code(code):
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
    if out is None:
        return None
    if adjust == "qfq":
        out = out.copy()
        out["close"] = adjust_series(out["close"])
    elif adjust is not None:
        raise ValueError(f"不支持的 adjust 口径: {adjust!r}（None / 'qfq'）")
    return out


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


# ── 动态规则池取数（strategy/industry_momentum.md v3_1 口径专用）──

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
