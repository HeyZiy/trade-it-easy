"""统一的函数入口、收益时点归属、报告及可选 SDK 扩展。"""
import os
import json
import re
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from research.tools.factor_analysis import (
    analyze_factor, analyze_factors, combine_factors, evaluate_period, rank_ic, render_report,
)
from research.tools.factor_analysis import sdk


@pytest.fixture
def panels():
    dates = pd.date_range('2026-01-01', periods=4)
    factors = pd.DataFrame([np.arange(8)] * 4, index=dates, columns=list('ABCDEFGH'), dtype=float)
    future = factors / 100
    return factors, future


def test_panel_analysis_uses_same_core_and_keeps_members_missing_future(panels):
    factor, future = panels
    future.loc[future.index[1], 'A'] = np.nan
    result = analyze_factor(factor, future.iloc[::-1, ::-1], groups=2, min_ic_stocks=4)
    for date in factor.index:
        expected, members = evaluate_period(factor.loc[date], future.loc[date], 2, 4)
        assert result.periods.loc[date, 'ic'] == pytest.approx(expected['ic'])
        pd.testing.assert_series_equal(result.memberships.loc[date], members, check_names=False)
    assert result.memberships.iloc[1, 0] == result.memberships.iloc[0, 0]
    assert pd.isna(result.periods.iloc[1]['G1'])
    assert result.periods.iloc[1]['G1_coverage'] == .75
    assert result.ic_summary['mean'] == pytest.approx(1.)


def test_new_rank_ic_does_not_select_a_different_return_interval(panels):
    factor, first_return = panels
    second_return = -first_return
    assert rank_ic(factor.iloc[0], first_return.iloc[0], 4) == pytest.approx(1.)
    assert rank_ic(factor.iloc[0], second_return.iloc[0], 4) == pytest.approx(-1.)


def test_multi_factor_reuses_same_analysis_and_propagates_missing(panels):
    factor, future = panels
    other = factor * 2
    other.loc[other.index[1], 'B'] = np.nan
    factors = {'one': factor, 'two': other}
    composite = combine_factors(factors, {'one': .25, 'two': .75})
    assert composite.iloc[0, 1] == 1.75
    assert pd.isna(composite.iloc[1, 1])
    results = analyze_factors(factors, future, groups=2, min_ic_stocks=4)
    expected = analyze_factor(combine_factors(factors), future, name='composite',
                              groups=2, min_ic_stocks=4)
    pd.testing.assert_frame_equal(results['composite'].periods, expected.periods)
    with pytest.raises(ValueError):
        combine_factors(factors, {'one': 1.})
    with pytest.raises(ValueError):
        combine_factors(factors, {'one': 0., 'two': 0.})


def test_duplicate_labels_fail_instead_of_changing_the_sample(panels):
    factor, future = panels
    duplicate = pd.concat([factor, factor.iloc[[0]]])
    with pytest.raises(ValueError, match='唯一'):
        analyze_factor(duplicate, future)
    factor.columns = ['A'] * 8
    with pytest.raises(ValueError, match='唯一'):
        analyze_factor(factor, future)


def test_report_consumes_calculated_results_and_preserves_gaps(panels, tmp_path, monkeypatch):
    import webbrowser

    factor, future = panels
    future.iloc[1, 0] = np.nan
    result = analyze_factor(factor, future, name='sample', groups=2, min_ic_stocks=4)
    monkeypatch.setattr(webbrowser, 'open', lambda *args: pytest.fail('默认不得打开浏览器'))
    path = render_report(result, tmp_path / 'report.html')
    html = Path(path).read_text(encoding='utf-8')
    assert 'IC 统计' in html and '逐期覆盖与统计' in html and '缺测' in html
    data = re.search(r'"name": "G1".*?"data": (\[[^\]]+\])', html)
    values = json.loads(data.group(1))
    assert values[0] == pytest.approx(.015)
    assert values[1:] == [None, None, None]
    assert 'href="#regression-analysis"' not in html
    assert result.periods.iloc[1]['G1_coverage'] == .75


def test_common_functions_do_not_import_amazingdata():
    source = """
import sys
from research.tools.factor_analysis import rank_ic, analyze_factor, render_report
from research.tools.factor_analysis import sdk
assert not any(n == 'AmazingData' or n.startswith('AmazingData.') for n in sys.modules)
"""
    env = dict(os.environ)
    result = subprocess.run([sys.executable, '-c', source],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_sdk_extensions_keep_real_library_interfaces(panels):
    pytest.importorskip('AmazingData.factor_analysis')
    factor, _ = panels
    other = factor + pd.DataFrame(np.random.default_rng(5).normal(size=factor.shape),
                                   index=factor.index, columns=factor.columns)
    factors = {'one': factor, 'two': other}
    weighted = sdk.weight_factors(factors, weight_normalization=False)
    pd.testing.assert_frame_equal(weighted, (factor + other) / 2)
    orthogonalized = sdk.orthogonalize(factors)
    assert set(orthogonalized) == set(factors)
    assert all(frame.shape == factor.shape for frame in orthogonalized.values())
    assert isinstance(sdk.collinearity(factors), dict)
    with pytest.raises(ValueError, match='调用方提供'):
        sdk.weight_factors(factors, method='ic_mean')
