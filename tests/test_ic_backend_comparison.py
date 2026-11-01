"""计算函数等价性与现有样本门槛；不把 SDK 的收益构造引入研究引擎。"""
import numpy as np
import pandas as pd
import pytest

from research.tools.factor_analysis import clean, evaluate_period


def comparison_cases():
    """固定方向、同分、缺失、错序、常数及小样本，再加固定种子的随机样例。"""
    factor = pd.Series([1., 2., 3., 4., 5.], index=list('ABCDE'))
    yield 'positive', factor, factor / 100, 3
    yield 'negative', factor, -factor / 100, 3
    yield 'ties', pd.Series([1., 1., 2., 3., 3.], index=factor.index), factor / 100, 3
    yield 'reordered_codes', factor, (factor / 100).iloc[::-1], 3
    yield 'different_code_sets', factor, pd.Series([.2, .1, .4, .3], index=list('BCDF')), 3
    yield 'nonfinite', factor, pd.Series([np.nan, .2, .3, .1, np.inf], index=factor.index), 3
    yield 'constant_factor', factor * 0, factor / 100, 3
    yield 'constant_return', factor, factor * 0, 3
    yield 'below_threshold', factor.iloc[:2], factor.iloc[:2] / 100, 3
    yield 'two_pairs_allowed', factor.iloc[:2], factor.iloc[:2] / 100, 2
    yield 'empty', factor.iloc[:0], factor.iloc[:0], 3
    rng = np.random.default_rng(20261101)
    for i in range(50):
        x, y = pd.Series(rng.integers(-5, 6, 100), dtype=float), pd.Series(rng.normal(size=100))
        x.iloc[rng.choice(100, 8, replace=False)] = np.nan
        y.iloc[rng.choice(100, 8, replace=False)] = np.nan
        yield f'random_{i:02d}', x, y.iloc[::-1], 20


def test_scipy_matches_old_rank_correlation_on_identical_inputs():
    for name, factor, future, minimum in comparison_cases():
        matched = pd.concat([clean(factor).rename('factor'), clean(future).rename('return')],
                            axis=1).reindex(factor.index).dropna()
        expected = np.nan
        if (len(matched) >= minimum and matched.factor.nunique() > 1
                and matched['return'].nunique() > 1):
            expected = matched.factor.rank().corr(matched['return'].rank())
        actual = evaluate_period(factor, future, n_groups=2, min_ic_stocks=minimum)[0]['ic']
        assert np.isclose(actual, expected, atol=1e-12, rtol=1e-12, equal_nan=True), name


def test_scipy_uses_given_endpoint_returns_and_preserves_coverage():
    factor = pd.Series([1., 1., 2., 3., 4.], index=list('ABCDE'))
    future = pd.Series([.1, np.nan, .3, .2, .4], index=list('ABCDE')).iloc[::-1]
    stats, groups = evaluate_period(factor, future, n_groups=2, min_ic_stocks=4)
    assert stats['ic'] == pytest.approx(.8)
    assert stats['paired_n'] == 4
    assert stats['factor_n'] == 5
    assert stats['coverage'] == .8
    assert groups.loc['A'] == groups.loc['B']
    assert pd.isna(stats['G1'])
    below, _ = evaluate_period(factor, future, n_groups=2, min_ic_stocks=5)
    assert pd.isna(below['ic'])
