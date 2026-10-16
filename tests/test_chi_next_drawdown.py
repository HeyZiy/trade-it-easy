"""核对诊断的账户分解，覆盖区间起初已有持仓和期末未平仓。"""
import numpy as np
import pandas as pd

from research.studies.broad_index_strategies.experiments import chi_next_drawdown as probe


def account():
    index = pd.bdate_range("2020-01-02", periods=6)
    px = pd.DataFrame({"open": [10., 9., 8., 11., 10., 12.],
                       "close": [11., 10., 9., 12., 9., 11.]}, index=index)
    ledger = []
    curve, _ = probe.base.sim_target(px, {index[0]: 1., index[2]: 0.,
                                         index[3]: 1., index[5]: 0.}, ledger=ledger)
    return px, curve, ledger


def test_daily_accounting_includes_exit_gap_and_purchase_day_pnl():
    px, curve, ledger = account()
    result = probe.daily_pnl(px, curve, ledger)
    quantity = ledger[0]["quantity"]
    assert result.iloc[0]["隔夜损益"] == 0.
    assert result.iloc[0]["日内损益"] == quantity * (11. - 10.)
    assert result.iloc[2]["退出日隔夜损益"] == quantity * (8. - 10.)
    assert result.iloc[2]["日内损益"] == 0.
    np.testing.assert_allclose(result["净值变化"].sum(), curve.nav.iloc[-1] - probe.base.CAPITAL)


def test_segments_mark_carry_in_and_open_position_at_interval_edges():
    px, curve, ledger = account()
    segments = probe.position_segments(px, curve, ledger, px.index[0], px.index[4])
    assert len(segments) == 2
    assert bool(segments.iloc[0]["期初已有持仓"])
    assert bool(segments.iloc[0]["卖出结束"])
    assert not bool(segments.iloc[1]["期初已有持仓"])
    assert not bool(segments.iloc[1]["卖出结束"])
    np.testing.assert_allclose(segments["段损益"].sum(), curve.nav.iloc[4] - curve.nav.iloc[0])
    # 后续卖出价不影响截断日的估值。
    modified = [dict(trade) for trade in ledger]
    modified[-1]["price"] = 1000.
    pd.testing.assert_frame_equal(segments, probe.position_segments(px, curve, modified,
                                                                  px.index[0], px.index[4]))
