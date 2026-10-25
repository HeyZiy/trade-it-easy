# -*- coding: utf-8 -*-
"""质量池的质量判定与选股；数据未知、确认出池、通过三种状态分开。"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from src.quality_pool.config import (
    INDUSTRY_CAP, NP_YOY_MIN, PB_MAX, PE_MAX, ROE_TTM_MIN, SCORE_CLOSES,
    SCORE_SKIP_RECENT, TOP_N, VOL60_MAX, VOL_ANNUAL_DAYS, VOL_LOOKBACK,
)


def build_pool(universe: Set[str], fundamentals: pd.DataFrame,
               status: pd.DataFrame, closes: pd.DataFrame,
               unlock_codes=None, audit: Optional[dict] = None) -> List[str]:
    """停牌只限制成交，不定义质量出池；解禁不参与现行规则。

    audit.pool_state 为 passed / failed / unknown。确认失败优先于未知，
    因此已知ROE不达标时，即使估值缺失仍可正确退出。
    """
    pool = []
    tracked = set(universe) | set(audit or {})
    for code in sorted(tracked):
        failed, missing = [], []
        values = {}
        if code not in universe:
            failed.append('not_in_mainboard_universe')
        for field, low, high, reason in (
                ('roe_ttm_pct', ROE_TTM_MIN, None, 'roe_low'),
                ('np_yoy_pct', NP_YOY_MIN, None, 'profit_growth_low'),
                ('pe_ttm', 0, PE_MAX, 'pe_high'),
                ('pb', 0, PB_MAX, 'pb_high')):
            value = (fundamentals.at[code, field]
                     if code in fundamentals.index and field in fundamentals.columns else None)
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float('nan')
            values[field] = value
            if not math.isfinite(value):
                missing.append('financial_missing' if high is None else 'valuation_invalid')
            elif value <= low:
                failed.append(reason if high is None else field.split('_')[0] + '_nonpos')
            elif high is not None and value >= high:
                failed.append(reason)
        if (code not in status.index or 'is_st' not in status.columns
                or pd.isna(status.at[code, 'is_st'])):
            missing.append('status_missing')
        elif bool(status.at[code, 'is_st']):
            failed.append('st')
        vol = _annualized_vol(closes[code]) if code in closes.columns else None
        if vol is None:
            missing.append('vol_history_missing')
        elif vol >= VOL60_MAX:
            failed.append('vol_high')
        state = 'failed' if failed else 'unknown' if missing else 'passed'
        if audit is not None:
            audit.setdefault(code, {}).update(
                values, pool_state=state, pool_reason='|'.join(dict.fromkeys(failed or missing))
                or 'passed', vol60=vol)
        if state == 'passed':
            pool.append(code)
    return pool


def _annualized_vol(series: pd.Series) -> Optional[float]:
    window = pd.to_numeric(series.iloc[-VOL_LOOKBACK:], errors='coerce')
    if (len(window) < VOL_LOOKBACK or window.isna().any()
            or not window.map(math.isfinite).all() or (window <= 0).any()):
        return None
    return float(window.pct_change().iloc[1:].std(ddof=1)
                 * math.sqrt(VOL_ANNUAL_DAYS) * 100.0)


def rank_pool(pool: List[str], closes: pd.DataFrame) -> Tuple[List[str], Dict[str, float]]:
    """253根收盘剔除最近21根；同分按代码排序，缺计分历史仅影响新仓。"""
    scores = {}
    for code in pool:
        if code not in closes.columns:
            continue
        series = closes[code].iloc[-SCORE_CLOSES:-SCORE_SKIP_RECENT]
        if (len(series) == SCORE_CLOSES - SCORE_SKIP_RECENT
                and series.notna().all() and series.map(math.isfinite).all()
                and (series > 0).all()):
            scores[code] = float(series.iloc[-1] / series.iloc[0] - 1.0)
    return sorted(scores, key=lambda c: (-scores[c], c)), scores


def industry_buy_block_reason(key: Optional[str], occupied,
                              industries: Dict[str, str]) -> Optional[str]:
    """行业约束单点：新开仓的行业闸门，选股与执行核对共用。

    key = 候选行业；occupied = 已占名额的代码集合（选股=已选名单，
    执行=台账持仓）。候选缺行业或任一 occupied 缺行业 → 'industry_unknown'
    （不能确认集中度就不放行，fail-closed）；同业已满 INDUSTRY_CAP →
    'industry_cap'；放行返回 None。
    """
    if not key or any(not industries.get(c) for c in occupied):
        return 'industry_unknown'
    if sum(industries[c] == key for c in occupied) >= INDUSTRY_CAP:
        return 'industry_cap'
    return None


def select_targets(ranked: List[str], held: Set[str], pool: Set[str],
                   industries: Dict[str, str], deferred: Set[str] = frozenset()
                   ) -> Tuple[List[str], List[str]]:
    """留存质量通过/暂缓判断的旧仓；只从前6名补位，未知行业不绕过约束。"""
    if len(set(ranked)) != len(ranked):
        raise ValueError('Duplicate ranked code')
    rank = {c: i + 1 for i, c in enumerate(ranked)}
    kept = sorted(held & (set(pool) | set(deferred)),
                  key=lambda c: (rank.get(c, len(ranked) + 1), c))
    if len(kept) > TOP_N:
        raise ValueError('More existing positions than TOP_N')
    selected = list(kept)
    # 缺旧仓行业时，不能确认新增后集中度；本轮保留旧仓但暂缓所有新开仓。
    if any(not industries.get(c) for c in kept):
        return selected, kept
    for code in ranked[:TOP_N]:
        if len(selected) >= TOP_N:
            break
        if code in selected or industry_buy_block_reason(
                industries.get(code), selected, industries):
            continue
        selected.append(code)
    return selected, kept
