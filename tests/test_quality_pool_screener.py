# -*- coding: utf-8 -*-
"""质量池筛选核测试 — 规格一、二节逐条映射（strategy/roe_quality_pool.md）。"""

import numpy as np
import pandas as pd
import pytest

from src.quality_pool import screener
from src.quality_pool.config import SCORE_CLOSES, SCORE_SKIP_RECENT, VOL_LOOKBACK


# ── 造数工具 ──

def _closes(spec: dict, rows: int = SCORE_CLOSES, end: str = "2026-09-30") -> pd.DataFrame:
    """spec: code → 收盘序列（不足 rows 前面补该股自己的起始价）。

    日期索引为倒数 rows 个交易日（仅作行数容器，判定不看真实日历）。
    """
    idx = pd.date_range(end=end, periods=rows, freq="B")
    cols = {}
    for code, tail in spec.items():
        series = list(tail)
        pad = series[0] if series else 10.0
        full = [pad] * (rows - len(series)) + series
        cols[code] = pd.Series(full, index=idx, dtype=float)
    return pd.DataFrame(cols)


def _fundamentals(spec: dict) -> pd.DataFrame:
    """spec: code → dict(roe_single_pct, np_yoy_pct, pe_ttm, pb)。"""
    return pd.DataFrame.from_dict(spec, orient="index")


def _status(spec: dict) -> pd.DataFrame:
    """spec: code → dict(is_st, is_suspended)；缺省行为正常在市（状态层 fail-closed，
    测试造数须覆盖全部 universe 代码）。"""
    return pd.DataFrame(
        {col: {code: bool(v.get(col, False)) for code, v in spec.items()}
         for col in ("is_st", "is_suspended")})


PASS_FUND = dict(roe_single_pct=4.0, np_yoy_pct=20.0, pe_ttm=15.0, pb=2.0)


# ── 一、股票池 ──

def test_qualifying_stock_passes():
    universe = {"600001"}
    pool = screener.build_pool(
        universe, _fundamentals({"600001": PASS_FUND}),
        _status({"600001": {}}), _closes({"600001": [10.0]}), set())
    assert pool == ["600001"]


def test_roe_threshold_strictly_greater():
    fund = dict(PASS_FUND, roe_single_pct=3.0)   # ×4 = 12%，规格要求 > 12%
    audit = {}
    pool = screener.build_pool({"600001"}, _fundamentals({"600001": fund}),
                               _status({"600001": {}}),
                               _closes({"600001": [10.0]}),
                               set(), audit=audit)
    assert pool == []
    assert audit["600001"]["pool_reason"] == "roe_low"


def test_growth_threshold_and_combined_reason():
    audit = {}
    fund = dict(PASS_FUND, roe_single_pct=2.0, np_yoy_pct=10.0)
    screener.build_pool({"600001"}, _fundamentals({"600001": fund}),
                        _status({}), _closes({"600001": [10.0]}), set(), audit=audit)
    assert audit["600001"]["pool_reason"] == "roe_low|profit_growth_low"


def test_financial_missing_reason():
    audit = {}
    pool = screener.build_pool({"600001"}, _fundamentals({}),
                               _status({"600001": {}}),
                               _closes({"600001": [10.0]}),
                               set(), audit=audit)
    assert pool == []
    assert audit["600001"]["pool_reason"] == "financial_missing"


def test_status_filters_st_and_suspended():
    fund = _fundamentals({"600001": PASS_FUND, "600002": PASS_FUND,
                          "600003": PASS_FUND})
    st = _status({"600001": {},
                  "600002": dict(is_st=True, is_suspended=False),
                  "600003": dict(is_st=False, is_suspended=True)})
    audit = {}
    pool = screener.build_pool({"600001", "600002", "600003"}, fund, st,
                               _closes({c: [10.0] for c in fund.index}), set(),
                               audit=audit)
    assert pool == ["600001"]
    assert audit["600002"]["pool_reason"] == "st"
    assert audit["600003"]["pool_reason"] == "paused"


def test_status_missing_fail_closed():
    pool = screener.build_pool({"600001"}, _fundamentals({"600001": PASS_FUND}),
                               _status({}), _closes({"600001": [10.0]}), set())
    assert pool == []


def test_valuation_upper_bounds_only():
    fund = _fundamentals({
        "600001": dict(PASS_FUND, pe_ttm=-8.0),      # 负 PE 无下界，可通过
        "600002": dict(PASS_FUND, pe_ttm=30.0),      # ≥30 剔除
        "600003": dict(PASS_FUND, pb=5.0),           # PB ≥5 剔除
        "600004": dict(PASS_FUND, pe_ttm=np.nan),    # 无效估值剔除
    })
    audit = {}
    pool = screener.build_pool(set(fund.index), fund,
                               _status({c: {} for c in fund.index}),
                               _closes({c: [10.0] for c in fund.index}),
                               set(), audit=audit)
    assert pool == ["600001"]
    assert audit["600002"]["pool_reason"] == "pe_high"
    assert audit["600003"]["pool_reason"] == "pb_high"
    assert audit["600004"]["pool_reason"] == "valuation_invalid"


def test_vol60_annualized_std_and_history():
    # 恒定价格 → 收益全 0 → vol 0：通过
    calm = _closes({"600001": [10.0] * VOL_LOOKBACK})
    assert screener.build_pool({"600001"}, _fundamentals({"600001": PASS_FUND}),
                               _status({"600001": {}}), calm, set()) == ["600001"]
    # 窗口内 61 根收盘不可得（次新股：前 61 根无数据）
    audit = {}
    short = _closes({"600002": [10.0] * (SCORE_CLOSES - 61)}, rows=SCORE_CLOSES)
    short.iloc[:61, 0] = np.nan
    pool = screener.build_pool({"600002"}, _fundamentals({"600002": PASS_FUND}),
                               _status({"600002": {}}), short, set(), audit=audit)
    assert pool == []
    assert audit["600002"]["pool_reason"] == "vol_history_missing"


def test_vol60_high_excluded():
    rng = np.random.default_rng(7)
    noisy = list(10 * np.exp(np.cumsum(rng.normal(0, 0.04, VOL_LOOKBACK))))
    vol = screener._annualized_vol(pd.Series(noisy))
    assert vol is not None and vol > 35
    audit = {}
    pool = screener.build_pool({"600001"}, _fundamentals({"600001": PASS_FUND}),
                               _status({"600001": {}}), _closes({"600001": noisy}),
                               set(), audit=audit)
    assert pool == []
    assert audit["600001"]["pool_reason"] == "vol_high"


def test_unlock_window_excludes():
    pool = screener.build_pool({"600001"}, _fundamentals({"600001": PASS_FUND}),
                               _status({}), _closes({"600001": [10.0]}),
                               {"600001"})
    assert pool == []


def test_filter_chain_order_first_failure_recorded():
    """同时挂多关时 pool_reason 记录过滤链上的首个未通过环节。"""
    audit = {}
    fund = dict(PASS_FUND, roe_single_pct=1.0, pe_ttm=99.0)
    screener.build_pool({"600001"}, _fundamentals({"600001": fund}),
                        _status({"600001": dict(is_st=True, is_suspended=False)}),
                        _closes({"600001": [10.0]}), {"600001"}, audit=audit)
    assert audit["600001"]["pool_reason"] == "roe_low"


# ── 二、怎么选 ──

def _window_closes(base: float, final_ratio: float, rows: int = SCORE_CLOSES) -> list:
    """121 根收盘：窗口首尾比 final_ratio，最后 20 根暴涨（不应进分数）。"""
    window = [base * (1 + (final_ratio - 1) * i / (SCORE_CLOSES - SCORE_SKIP_RECENT - 1))
              for i in range(SCORE_CLOSES - SCORE_SKIP_RECENT)]
    spike = [window[-1] * (1 + 0.1 * i) for i in range(1, SCORE_SKIP_RECENT + 1)]
    return window + spike


def test_rank_score_skips_recent_20_days():
    # A 窗口内 +100%，B 窗口内 +10%；B 最近 20 日暴涨 10 倍——分数仍 A > B
    closes = _closes({
        "600001": _window_closes(10.0, 2.0),
        "600002": _window_closes(10.0, 1.1),
    })
    ranked, scores = screener.rank_pool(["600001", "600002"], closes)
    assert ranked == ["600001", "600002"]
    assert scores["600001"] == pytest.approx(1.0)
    assert scores["600002"] == pytest.approx(0.1)


def test_rank_tie_breaks_by_code():
    closes = _closes({"600002": _window_closes(10.0, 1.5),
                      "600001": _window_closes(5.0, 1.5)})
    ranked, _ = screener.rank_pool(["600002", "600001"], closes)
    assert ranked == ["600001", "600002"]


def test_rank_missing_history_unranked():
    short = _closes({"600001": [10.0]}, rows=100)   # 不足 121 根
    ranked, scores = screener.rank_pool(["600001"], short)
    assert ranked == [] and scores == {}


def test_rank_nan_in_window_unranked():
    series = _window_closes(10.0, 1.5)
    closes = _closes({"600001": series})
    closes.iloc[10, 0] = np.nan   # 窗口内停牌缺价
    ranked, _ = screener.rank_pool(["600001"], closes)
    assert ranked == []


def test_select_keeps_buffer_and_fills_from_top():
    ranked = [f"{600000 + i}" for i in range(50)]      # 600000..600049
    held = {ranked[5], ranked[25], ranked[35], ranked[48]}
    selected, kept = screener.select_targets(ranked, held)
    # 排名 5、25（≤30）留存；35、48 出；前 20 名中未持有的补足至 20
    assert kept == [ranked[5], ranked[25]]
    expected_added = [c for c in ranked[:20] if c not in kept][:20 - len(kept)]
    assert selected == kept + expected_added
    assert len(selected) == 20


def test_select_held_missing_rank_exits():
    ranked = ["600001", "600002"]
    selected, kept = screener.select_targets(ranked, {"600009"})
    assert kept == [] and selected == ["600001", "600002"]


def test_select_kept_are_ranked_first_in_target_order():
    ranked = [f"{600000 + i}" for i in range(30)]
    held = {ranked[3], ranked[10], ranked[20]}
    selected, kept = screener.select_targets(ranked, held)
    assert kept == [ranked[3], ranked[10], ranked[20]]
    assert selected[:3] == kept                    # 旧仓排新仓前
    assert selected[3] == ranked[0]                # 补入从第 1 名开始


def test_select_rejects_duplicate_ranked():
    with pytest.raises(ValueError):
        screener.select_targets(["600001", "600001"], set())
