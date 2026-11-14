"""增长口径探针的同季比较、共同资格及分组统计。"""
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from jq_fake import JQStub


SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum/pool/growth_basis.py'


@pytest.fixture
def study(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    factors = ModuleType('jqfactor')
    factors.get_factor_values = None
    monkeypatch.setitem(sys.modules, 'jqfactor', factors)
    return JQStub([], {}).load_script(SCRIPT)


def financials(code='A'):
    days = pd.date_range('2022-12-31', periods=8, freq='QE')
    return pd.DataFrame([dict(code=code, statDate=day, pubDate=day+pd.Timedelta(days=25),
        metric=metric, value=(prior if i < 4 else current))
        for metric, prior, current in [('net_profit', 10., 12.),
            ('np_parent_company_owners', 8., 9.6), ('adjusted_profit', 6., 9.)]
        for i, day in enumerate(days)])


def test_separate_total_parent_and_adjusted_ttm_and_fixed_threshold(study):
    features = study.growth_features(financials(), ['A'], '2025-01-01')
    row = features.loc['A']
    assert row.ttm_total == pytest.approx(20)
    assert row.ttm_parent == pytest.approx(20)
    assert row.ttm_adjusted == pytest.approx(50)
    features['np_yoy_pct'], features['stat_date'] = 10., '2024-09-30'
    frozen = study.freeze_growth(features)
    assert not frozen.loc['A', 'pass_baseline']
    assert frozen.loc['A', 'common'] and frozen.loc['A', 'pass_ttm_adjusted']
    features['stat_date'] = '2024-06-30'
    assert not study.freeze_growth(features).loc['A', 'common']


@pytest.mark.parametrize('problem,status', [('future','invalid_visibility'), ('gap','missing_quarters'),
    ('duplicate','duplicate_report'), ('zero','nonpositive_prior'), ('nan','invalid_value')])
def test_no_fallback_for_invalid_adjusted_profit(study, problem, status):
    raw = financials()
    mask = raw.metric.eq('adjusted_profit')
    idx = raw.index[mask][0]
    if problem == 'future':
        raw.loc[idx, 'pubDate'] = pd.Timestamp('2025-01-02')
    elif problem == 'gap':
        raw = raw.drop(idx)
    elif problem == 'duplicate':
        raw = pd.concat([raw, raw.loc[[idx]]], ignore_index=True)
    elif problem == 'zero':
        raw.loc[raw.index[mask][:4], 'value'] = 0.
    else:
        raw.loc[idx, 'value'] = np.nan
    row = study.growth_features(raw, ['A'], '2025-01-01').loc['A']
    assert row.ttm_adjusted_status == status and pd.isna(row.ttm_adjusted)
    assert row.ttm_total == pytest.approx(20)


def scan_frame():
    return pd.DataFrame(dict(
        roe_ttm_pct=[13., 16., 13., 19., np.nan],
        np_yoy_pct=[11., 11., 21., 31., 31.],
        pe_ttm=[20., 19., 20., 10., 10.],
        pb=[3., 4., 2., 1., 1.],
        vol60=[30., 30., 34., 20., 20.]), index=list('abcde'))


@pytest.mark.parametrize('overrides,expected', [
    (dict(growth=None), [True, True, True, True, False]),
    ({}, [True, True, True, True, False]),
    (dict(growth=20.), [False, False, True, True, False]),
    (dict(roe=15.), [False, True, False, True, False]),
    (dict(pe=20.), [False, True, False, True, False]),
    (dict(pb=3.), [False, False, True, True, False]),
    (dict(vol=25.), [False, False, False, True, False]),
    (dict(growth=20., roe=15., pe=20., pb=3., vol=25.), [False, False, False, True, False]),
])
def test_scan_variants_tighten_thresholds_strictly(study, overrides, expected):
    assert study.variant_pass(scan_frame(), overrides).tolist() == expected
    assert not study.variant_pass(pd.DataFrame(index=['x']), {}).iloc[0]


def test_statistics_emit_scan_groups_and_minus_v5_differences(study):
    frame = scan_frame().iloc[[0, 3]][['roe_ttm_pct', 'np_yoy_pct', 'pe_ttm', 'pb', 'vol60']].copy()
    frame['common'] = [True, False]
    for arm in ('baseline', 'ttm_total', 'ttm_parent', 'ttm_adjusted'):
        frame['pass_' + arm] = True
    for h in (20, 40, 60):
        frame['r%d' % h] = [.01 * h / 20, .03 * h / 20]
    snapshot = dict(signal_day=pd.Timestamp('2024-01-02'), asof=pd.Timestamp('2024-01-01'), error='')
    for h in (20, 40, 60):
        snapshot['status_%d' % h] = 'mature'
    rows = pd.DataFrame(study.growth_period_statistics(snapshot, frame))
    available = rows[rows.scope.eq('available_inputs') & rows.horizon.eq(20)].set_index('group')
    assert available.loc['no_growth', 'n'] == 2
    assert available.loc['v5', 'n'] == 2
    assert available.loc['strict', 'n'] == 1
    assert available.loc['strict_minus_v5', 'return_available'] == pytest.approx(.03 - .02)
    assert 'v5_minus_v5' not in available.index
    common = rows[rows.scope.eq('common_inputs') & rows.horizon.eq(20)].set_index('group')
    assert common.loc['v5', 'n'] == 1 and common.loc['strict', 'n'] == 0
