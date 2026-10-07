"""现行独立实验的八季财务、固定分组与控制回归。"""

import numpy as np
import pandas as pd
import pytest

from research.studies.expectations.internal import low_expectations as pilot


def snapshot(pe=9.0, code='600001.XSHG'):
    return pd.DataFrame([dict(code=code, signal='2024-11-01', asof='2024-10-31',
                              report='2024-09-30', pe_ratio=pe, market_cap=100.0,
                              momentum120=.1, industry='801010')])


def history(code='600001.XSHG', profit=None):
    quarters = pd.period_range('2022Q4', periods=8, freq='Q')
    values = [1, 2, 3, 4, 2, 3, 4, 5] if profit is None else profit
    return pd.DataFrame([dict(code=code, statDate=q.end_time.normalize(),
                              pubDate=q.end_time.normalize() + pd.Timedelta(days=30),
                              np_parent_company_owners=value)
                         for q, value in zip(quarters, values)])


def test_ttm_uses_eight_single_quarters_and_boundary_pe_is_not_low():
    feature = pilot.prepare_profit_features(history(), snapshot())
    row = feature.iloc[0]
    assert row.profit_status == 'ok'
    assert row.prior_ttm == 10 and row.current_ttm == 14
    assert row.profit_yoy == pytest.approx(.4)
    assert pilot.freeze_profit_groups(snapshot(), feature).cell.iloc[0] == 'A'
    at_boundary = snapshot(pe=10)
    assert pilot.freeze_profit_groups(at_boundary, feature).cell.iloc[0] == 'C'
    flat = pilot.prepare_profit_features(history(profit=[1] * 8), snapshot())
    assert pilot.freeze_profit_groups(snapshot(), flat).profit_maintained.iloc[0]


@pytest.mark.parametrize('change,reason', [
    ('truncated', 'insufficient_quarters'), ('future', 'not_visible'),
    ('duplicate', 'ambiguous_quarter'), ('gap', 'nonconsecutive_quarters'),
    ('missing_latest', 'missing_profit'), ('prior_loss', 'nonpositive_prior_ttm'),
    ('current_loss', 'nonpositive_current_ttm'), ('wrong_report', 'report_differs_from_original'),
])
def test_invalid_history_never_gets_backfilled_as_profit_stability(change, reason):
    rows, original = history(), snapshot()
    if change == 'truncated':
        rows = rows.tail(5)
    elif change == 'future':
        rows.loc[7, 'pubDate'] = pd.Timestamp('2024-11-02')
    elif change == 'duplicate':
        rows = pd.concat([rows, rows.tail(1)], ignore_index=True)
    elif change == 'gap':
        rows.loc[0, 'statDate'] = pd.Timestamp('2022-09-30')
        rows.loc[0, 'pubDate'] = pd.Timestamp('2022-10-30')
    elif change == 'missing_latest':
        rows.loc[7, 'np_parent_company_owners'] = np.nan
    elif change == 'prior_loss':
        rows.loc[:3, 'np_parent_company_owners'] = -1
    elif change == 'current_loss':
        rows.loc[4:, 'np_parent_company_owners'] = -1
    elif change == 'wrong_report':
        original.loc[0, 'report'] = '2024-06-30'
    features = pilot.prepare_profit_features(rows, original)
    assert features.profit_status.iloc[0] == reason
    assert pilot.freeze_profit_groups(original, features).empty


def group_frame():
    return pd.DataFrame(dict(code=['a1', 'a2', 'b1', 'b2', 'c1', 'c2', 'd1', 'd2'],
                             cell=list('AABBCCDD'), low_expectations=[True] * 4 + [False] * 4,
                             profit_maintained=[True, True, False, False] * 2,
                             pe_ratio=[5, 6, 7, 8, 11, 12, 13, 14], market_cap=np.arange(1, 9),
                             momentum120=np.arange(8), industry=['I'] * 8))


def test_observed_and_strict_results_preserve_missing_members_and_common_low_baseline():
    frame = group_frame()
    future = pd.Series([.1, np.nan, 0, 0, .03, .03, .01, .01], index=frame.code)
    before = frame.copy(deep=True)
    stats = pilot.evaluate_profit_period(frame, future, min_cell_n=1, min_n=100)
    assert stats['A_n'] == 2 and stats['A_valid_n'] == 1 and stats['A_coverage'] == .5
    assert stats['LOW_n'] == 4 and stats['LOW_valid_n'] == 3
    assert stats['LOW_observed'] == pytest.approx(.1 / 3)
    assert stats['A_minus_LOW_observed'] == pytest.approx(.1 - .1 / 3)
    assert np.isnan(stats['A_strict']) and np.isnan(stats['A_minus_LOW_strict'])
    assert frame.equals(before)


def test_regression_reports_conditional_stability_even_without_interaction():
    rng = np.random.RandomState(17)
    n = 400
    low, stable = rng.rand(n) > .5, rng.rand(n) > .5
    frame = pd.DataFrame(dict(code=['c%d' % i for i in range(n)], low_expectations=low,
                             profit_maintained=stable, pe_ratio=np.where(low, 4 + rng.rand(n)*4, 12 + rng.rand(n)*20),
                             market_cap=np.exp(rng.normal(4, 1, n)), momentum120=rng.randn(n),
                             industry=np.where(np.arange(n) % 2, 'X', 'Y')))
    y = .02*low + .04*stable + .015*np.log(frame.market_cap)
    y += .03*pilot.centered_rank(frame.momentum120)
    y += .02*pilot.centered_rank(1/frame.pe_ratio)
    y += np.where(frame.industry == 'Y', .005, 0)
    future = pd.Series(y.values, index=frame.code)
    # 缺报价后的设计矩阵仍使用完整冻结样本的秩。
    future.iloc[:7] = np.nan
    out = pilot.stable_regression(frame, future)
    assert out['regression_status'] == 'ok'
    assert out['regression_stable_within_low'] == pytest.approx(.04)
    assert out['regression_interaction'] == pytest.approx(0, abs=1e-10)
    frame['profit_maintained'] = True
    assert pilot.stable_regression(frame, future)['regression_status'] == 'not_identifiable'


def test_hac_does_not_compress_a_missing_scheduled_period():
    table = pd.DataFrame(dict(signal=pd.bdate_range('2024-01-01', periods=6), horizon=[60] * 6))
    for name in pilot.METRICS:
        table[name] = np.arange(6) / 100
    table.loc[2, 'A_minus_B_observed'] = np.nan
    result = pilot.summarize_profit_periods(table)
    row = result[(result.scope == 'all') & (result.metric == 'A_minus_B_observed')].iloc[0]
    assert row.n == 5 and row.scheduled_n == 6 and np.isnan(row.t_hac)
    assert row.hac_lags == 3

