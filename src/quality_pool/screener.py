# -*- coding: utf-8 -*-
"""质量池筛选 + 强弱排名 + 留存补足 — 纯判定核（规格一、二节）。

输入全部是规范化后的 DataFrame/集合（组装与取数在 feeds.py），本模块零 I/O、
零平台依赖，规格逐条落在过滤链上。过滤链顺序沿用聚宽参考实现
（pool_rotation_v3a.build_pool）：ROE → 净利同比 → ST → PE/PB → 停牌 →
波动率 → 解禁；audit 记录首个未通过环节（pool_reason），供退出归因与诊断。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from src.quality_pool.config import (
    KEEP_RANK, NP_YOY_MIN, PB_MAX, PE_MAX, ROE_SINGLE_MIN, SCORE_CLOSES,
    SCORE_SKIP_RECENT, TOP_N, VOL60_MAX, VOL_ANNUAL_DAYS, VOL_LOOKBACK,
)


def _note(audit: Optional[dict], codes, reason: str) -> None:
    """记录首个未通过环节（pool_reason 只写一次）。"""
    if audit is None:
        return
    for code in codes:
        audit.setdefault(code, {})["pool_reason"] = reason


def _join_reasons(failed: List[str]) -> str:
    return "|".join(failed) if failed else "passed"


def build_pool(universe: Set[str], fundamentals: pd.DataFrame,
               status: pd.DataFrame, closes: pd.DataFrame,
               unlock_codes: Set[str], audit: Optional[dict] = None) -> List[str]:
    """按规格一节从全市场筛出质量池，返回池内代码列表。

    universe      信号日主板在市 A 股（60/00 开头）
    fundamentals  index=code：roe_single_pct / np_yoy_pct / pe_ttm / pb
    status        index=code（T 日当时）：is_st / is_suspended
    closes        截止 T−1 的前复权收盘矩阵（dates × codes，≥121 行）
    unlock_codes  [T, T+90 自然日] 内存在解禁事件的代码
    """
    cands = sorted(universe)
    if not cands:
        return []

    # ── 质量 + 成长：最新单季 ROE 与净利同比（财务缺失/未披露/无效先于阈值） ──
    known = set(fundamentals.index)
    _note(audit, set(cands) - known, "financial_missing")
    failed: Dict[str, List[str]] = {}
    for code in set(cands) & known:
        row_failed = []
        if not fundamentals.at[code, "roe_single_pct"] > ROE_SINGLE_MIN:
            row_failed.append("roe_low")
        if not fundamentals.at[code, "np_yoy_pct"] > NP_YOY_MIN:
            row_failed.append("profit_growth_low")
        if row_failed:
            failed[code] = row_failed
    cands = [c for c in cands if c in known and c not in failed]
    _note(audit, ((set(universe) & known) - set(cands)), "roe_low")
    if audit is not None:
        for code, row_failed in failed.items():
            audit.setdefault(code, {})["pool_reason"] = _join_reasons(row_failed)
    if not cands:
        return []

    # ── 状态（T 日当时）：ST、停牌 ──
    def _flag(code: str, column: str) -> bool:
        if code not in status.index:
            return True   # 状态缺失 fail-closed：视为不合格
        return bool(status.at[code, column])

    passing = [c for c in cands if not _flag(c, "is_st")]
    _note(audit, set(cands) - set(passing), "st")
    cands = passing
    if not cands:
        return []
    passing = [c for c in cands if not _flag(c, "is_suspended")]
    _note(audit, set(cands) - set(passing), "paused")
    cands = passing
    if not cands:
        return []

    # ── 估值：PE(TTM)/PB 仅设上界，负值可通过；NaN 视为无效剔除 ──
    val_known = set(fundamentals[["pe_ttm", "pb"]].dropna().index)
    _note(audit, set(cands) - val_known, "valuation_invalid")
    cands = [c for c in cands if c in val_known]
    passing = [c for c in cands
               if fundamentals.at[c, "pe_ttm"] < PE_MAX and fundamentals.at[c, "pb"] < PB_MAX]
    if audit is not None:
        for code in set(cands) - set(passing):
            row_failed = []
            if not fundamentals.at[code, "pe_ttm"] < PE_MAX:
                row_failed.append("pe_high")
            if not fundamentals.at[code, "pb"] < PB_MAX:
                row_failed.append("pb_high")
            audit.setdefault(code, {})["pool_reason"] = _join_reasons(row_failed)
    cands = passing
    if not cands:
        return []

    # ── 波动率：最近 61 根完整收盘 → 60 个收益的样本标准差(ddof=1) × √250 ──
    vol = {c: _annualized_vol(closes[c]) for c in cands if c in closes.columns}
    missing = {c for c, v in vol.items() if v is None}
    _note(audit, (set(cands) - set(vol)) | missing, "vol_history_missing")
    if audit is not None:
        for code, v in vol.items():
            if v is not None:
                audit.setdefault(code, {})["vol60"] = v
    cands = [c for c in cands if vol.get(c) is not None and vol[c] < VOL60_MAX]
    _note(audit, (set(universe) & set(vol)) - set(cands) - missing, "vol_high")
    if not cands:
        return []

    # ── 解禁：[T, T+90 自然日] 内任意已知事件 ──
    passing = [c for c in cands if c not in unlock_codes]
    _note(audit, set(cands) - set(passing), "unlock_90d")
    return passing


def _annualized_vol(series: pd.Series) -> Optional[float]:
    window = series.iloc[-VOL_LOOKBACK:]
    if len(window) < VOL_LOOKBACK or window.isna().any() or (window <= 0).any():
        return None
    rets = window.pct_change().iloc[1:]
    return float(rets.std(ddof=1) * math.sqrt(VOL_ANNUAL_DAYS) * 100.0)


def rank_pool(pool: List[str], closes: pd.DataFrame) -> Tuple[List[str], Dict[str, float]]:
    """强弱分数 R = P(T−21)/P(T−121) − 1：截止 T−1 的倒数第 21 根至第 121 根
    共 101 根收盘的 100 日收益，最近 20 个交易日不进入分数。降序排名，同分按
    代码升序。收盘数据不足 121 根或窗口内有缺失/非正值的无法计分（不进 ranked）。"""
    scores: Dict[str, float] = {}
    for code in pool:
        if code not in closes.columns:
            continue
        window = closes[code].iloc[-SCORE_CLOSES:]
        series = window.iloc[:SCORE_CLOSES - SCORE_SKIP_RECENT]
        if len(series) == SCORE_CLOSES - SCORE_SKIP_RECENT and series.notna().all() \
                and (series > 0).all():
            scores[code] = float(series.iloc[-1] / series.iloc[0] - 1.0)
    ranked = sorted(scores, key=lambda c: (-scores[c], c))
    return ranked, scores


def select_targets(ranked: List[str], held: Set[str]) -> Tuple[List[str], List[str]]:
    """留存 ≤30 名旧仓，再从当期前 20 名依次补空位，最多 20 只。

    返回 (selected, kept)：selected = kept + 补入，旧仓排在补入前（执行序即此序）。
    池内旧仓缺失排名按未留存处理——该情形由调用方按规格「已有池内持仓若缺失
    排序行情，整轮暂停」前置拦截，不落入此处。
    """
    if len(set(ranked)) != len(ranked):
        raise ValueError("Duplicate ranked code")
    rank = {c: i + 1 for i, c in enumerate(ranked)}
    kept = sorted((c for c in held if rank.get(c, KEEP_RANK + 1) <= KEEP_RANK),
                  key=lambda c: rank[c])
    if len(kept) > TOP_N:
        raise ValueError("More eligible existing positions than TOP_N")
    added = [c for c in ranked[:TOP_N] if c not in set(kept)][:TOP_N - len(kept)]
    return kept + added, kept
