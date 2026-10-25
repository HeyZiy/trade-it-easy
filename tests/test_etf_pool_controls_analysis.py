"""六组结果分析的期初收益、跨年锚点及资金核算边界。"""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = (Path(__file__).resolve().parents[1] /
          'research/studies/etf_rotation/analyze_pool_controls.py')
SPEC = importlib.util.spec_from_file_location('pool_controls_analysis_test', SCRIPT)
analysis = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analysis)


def test_first_day_loss_year_boundary_recovery_and_no_duplicate_capital():
    nav = pd.DataFrame([
        ('2016-12-29', '15排名', 90000., 90000., 0., ''),
        ('2016-12-30', '15排名', 110000., 110000., 0., ''),
        ('2017-01-03', '15排名', 99000., 99000., 0., ''),
        ('2017-01-04', '15排名', 111000., 111000., 0., ''),
    ], columns=['date', 'account', 'nav', 'cash', 'invested', 'holdings'])
    decisions = pd.DataFrame({'reason': ['entry', 'keep', 'keep', 'keep']})
    summary, years = analysis.summarize_account(nav, decisions)
    assert summary['return_pct'] == pytest.approx(11.)
    assert summary['max_drawdown_pct'] == pytest.approx(10.)
    assert summary['peak_date'] == 'initial_capital'
    assert summary['trough_date'] == '2016-12-29'
    assert summary['recovery_date'] == '2016-12-30'
    assert summary['longest_underwater_trading_days'] == 1
    assert years[0]['return_pct'] == pytest.approx(10.)
    assert years[1]['start_nav'] == 110000.
    assert years[1]['return_pct'] == pytest.approx((111000./110000.-1)*100)
    assert years[1]['max_drawdown_pct'] == pytest.approx(10.)


def frames():
    labels = analysis.LABELS
    nav = pd.DataFrame([('2020-01-02', name, 100000., 100000., 0., 0., 0., '')
                        for name in labels],
                       columns=['date', 'account', 'nav', 'cash', 'invested',
                                'return_pct', 'drawdown_pct', 'holdings'])
    signals = pd.DataFrame([('2020-01-02', 'A', 1., 'eligible', 1., 1.)],
                           columns=['date', 'code', 'score', 'status', 'rank15', 'rank37'])
    decisions = pd.DataFrame([('2020-01-02', name, '', 'A', 'monthly_equal'
                               if name.endswith('等权') else 'entry') for name in labels],
                            columns=['date', 'account', 'holdings_before', 'target', 'reason'])
    return nav, signals, decisions


def test_incomplete_accounts_and_fabricated_cash_are_rejected():
    nav, signals, decisions = frames()
    analysis.verify(nav, signals, decisions, ('A',), ('A',))
    with pytest.raises(ValueError, match='账户集合不完整'):
        analysis.verify(nav.iloc[:-1], signals, decisions, ('A',), ('A',))
    nav.loc[0, 'cash'] = 99999.
    with pytest.raises(ValueError, match='未对平'):
        analysis.verify(nav, signals, decisions, ('A',), ('A',))


def test_target_and_exit_reason_are_checked_against_actual_signal():
    nav, signals, decisions = frames()
    decisions.loc[2, 'target'] = 'B'
    with pytest.raises(ValueError, match='目标不符合规则'):
        analysis.verify(nav, signals, decisions, ('A',), ('A',))
    decisions.loc[2, 'target'] = 'A'
    decisions.loc[2, 'reason'] = 'rank_replaced'
    with pytest.raises(ValueError, match='退出原因不一致'):
        analysis.verify(nav, signals, decisions, ('A',), ('A',))


def test_signal_gaps_and_ranking_corruption_cannot_pass_validation():
    nav, signals, decisions = frames()
    signals.loc[0, 'rank37'] = 2.
    with pytest.raises(ValueError, match='排名不一致'):
        analysis.verify(nav, signals, decisions, ('A',), ('A',))
    signals.loc[0, 'rank37'] = 1.
    signals.loc[0, 'date'] = '2020-01-03'
    with pytest.raises(ValueError, match='信号与净值日期不一致'):
        analysis.verify(nav, signals, decisions, ('A',), ('A',))
