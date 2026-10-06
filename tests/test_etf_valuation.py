# -*- coding: utf-8 -*-
"""标的级估值口径契约：只有跟踪指数锚，不做兜底。

零网络：_fetch_csindex_indicator / get_treasury_yield_y10 全部 monkeypatch。
"""
from collections import namedtuple

import pandas as pd
import pytest

from src.etf import amazing_factors as af

Etf = namedtuple("Etf", "code name")


@pytest.fixture(autouse=True)
def _clear_csindex_cache():
    af._csindex_cache.clear()
    yield
    af._csindex_cache.clear()


def _fake_csindex_df(pe=7.1, dy=5.2, index_name="中证红利指数"):
    """官网明细形态：按日期降序，首行为最新。"""
    return pd.DataFrame([{
        "index_code": "000922", "index_name": index_name,
        "date": "2026-10-06", "pe": pe, "dy": dy,
    }])


# ══════════════ _etf_pe_info 三个出口 ══════════════

def test_overseas_exit_without_fetch(monkeypatch):
    """海外 ETF 显式出口，不碰取数。"""
    calls = []
    monkeypatch.setattr(af, "_fetch_csindex_indicator",
                        lambda code: calls.append(code) or _fake_csindex_df())
    info = af._etf_pe_info("513100")
    assert info == {"source_type": "overseas", "pe": None, "source_name": "海外"}
    assert calls == []


def test_csindex_exit_carries_pe_dividend_and_no_percentile(monkeypatch):
    """跟踪指数锚：带 PE/股息率/利差，且不带 pe_pct（标的级无分位口径）。"""
    monkeypatch.setattr(af, "_fetch_csindex_indicator", lambda code: _fake_csindex_df())
    monkeypatch.setattr(af, "get_treasury_yield_y10", lambda: 2.1)
    info = af._etf_pe_info("515180")
    assert info["source_type"] == "csindex"
    assert info["source_name"] == "中证红利指数"
    assert info["pe"] == 7.1 and info["div_yield"] == 5.2
    assert info["div_yield_spread"] == pytest.approx(3.1)
    assert "pe_pct" not in info


def test_untracked_code_returns_none_without_fetch(monkeypatch):
    """未配跟踪指数锚 → None（不退回行业/全市场口径）。"""
    calls = []
    monkeypatch.setattr(af, "_fetch_csindex_indicator",
                        lambda code: calls.append(code) or _fake_csindex_df())
    assert af._etf_pe_info("159915") is None
    assert calls == []


def test_csindex_failure_returns_none(monkeypatch):
    """官网未取到 → None，报告显示无锚，不出结论。"""
    monkeypatch.setattr(af, "_fetch_csindex_indicator", lambda code: None)
    assert af._etf_pe_info("515180") is None


# ══════════════ 失败不锁死整轮取数 ══════════════

def test_failed_valuation_is_not_cached(monkeypatch):
    """失败值不写缓存：下一次调用仍会重新取数。"""
    calls = []

    def fail(code):
        calls.append(code)
        return None

    monkeypatch.setattr(af, "_fetch_csindex_indicator", fail)
    assert af.get_csindex_valuation("000922") is None
    assert af.get_csindex_valuation("000922") is None
    assert calls == ["000922", "000922"]
    assert "000922" not in af._csindex_cache


def test_successful_valuation_is_cached(monkeypatch):
    calls = []

    def fetch(code):
        calls.append(code)
        return _fake_csindex_df()

    monkeypatch.setattr(af, "_fetch_csindex_indicator", fetch)
    af.get_csindex_valuation("000922")
    af.get_csindex_valuation("000922")
    assert calls == ["000922"]


# ══════════════ 报告行契约 ══════════════

def test_valuation_rows_marks_missing_anchor(monkeypatch):
    monkeypatch.setattr(af, "_fetch_csindex_indicator", lambda code: None)
    rows = af.etf_valuation_rows([Etf("515180", "红利ETF")])
    assert rows[0]["pe"] is None and rows[0]["div_yield"] is None
    assert rows[0]["source_name"] == ""
    assert "无估值锚" in rows[0]["level_text"]


def test_valuation_rows_dividend_uses_spread_note(monkeypatch):
    monkeypatch.setattr(af, "_fetch_csindex_indicator", lambda code: _fake_csindex_df())
    monkeypatch.setattr(af, "get_treasury_yield_y10", lambda: 2.1)
    rows = af.etf_valuation_rows([Etf("515180", "红利ETF")])
    assert "利差" in rows[0]["level_text"]
    assert "⭐" not in rows[0]["level_text"]      # 星标优先级文案已删


def test_valuation_rows_keep_input_order_no_ranking(monkeypatch):
    """不再按"最便宜在前"排序：估值不决定买入顺序，顺序归基准。"""
    monkeypatch.setattr(af, "_fetch_csindex_indicator", lambda code: _fake_csindex_df())
    monkeypatch.setattr(af, "get_treasury_yield_y10", lambda: 2.1)
    rows = af.etf_valuation_rows([Etf("159915", "创业板ETF"),
                                  Etf("515180", "红利ETF")])
    assert [r["code"] for r in rows] == ["159915", "515180"]


# ══════════════ 基准完整性契约（删兜底后，标的级估值只剩锚） ══════════════

def test_every_equity_leg_has_a_valuation_exit():
    """每只核心权益腿必须有出口：跟踪指数锚或显式海外，否则静默显示"无估值锚"。

    半年人工对齐基准时新增权益 ETF 若忘了配 TRACKED_INDEX，不会报错只会少一行数字，
    故在此钉住（黄金/现金按设计不适用估值锚，不在断言范围）。
    """
    from src.etf.config import CORE_BASELINE, TRACKED_INDEX
    equity = {a.code for a in CORE_BASELINE if a.asset_type == "equity"}
    assert equity <= (set(TRACKED_INDEX) | set(af._OVERSEAS_CODES)), \
        f"无估值出口（既无跟踪指数锚也非海外）: {sorted(equity - set(TRACKED_INDEX) - set(af._OVERSEAS_CODES))}"


def test_no_orphan_tracked_anchor():
    """TRACKED_INDEX 不留孤儿锚：基准删掉的标的其锚代码应一并撤下。"""
    from src.etf.config import CORE_BASELINE, TRACKED_INDEX
    baseline = {a.code for a in CORE_BASELINE}
    assert set(TRACKED_INDEX) <= baseline, \
        f"锚指向已不在基准里的标的: {sorted(set(TRACKED_INDEX) - baseline)}"


def test_anchor_value_looks_like_index_code():
    """锚值须为 6 位指数代码（csindex 按它取数）。"""
    from src.etf.config import TRACKED_INDEX
    assert all(len(str(v)) == 6 and str(v).isdigit() for v in TRACKED_INDEX.values()), \
        TRACKED_INDEX


def test_baseline_weights_sum_to_unity():
    from src.etf.config import CORE_BASELINE
    assert sum(a.neutral_weight for a in CORE_BASELINE) == pytest.approx(1.0)


def test_dividend_style_codes_are_in_baseline():
    """利差备注只服务基准内标的，漂移了就是死配置。"""
    from src.etf.config import CORE_BASELINE, DIVIDEND_STYLE_CODES
    assert set(DIVIDEND_STYLE_CODES) <= {a.code for a in CORE_BASELINE}
