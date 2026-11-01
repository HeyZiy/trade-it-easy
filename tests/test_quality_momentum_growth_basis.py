"""增长口径：同季、同分母、共同资格及平台回调导出。"""
import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from io import StringIO
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_quality_pool_v2 import world  # noqa: E402,F401
from test_platform_quality_momentum_turnover_probe import probe, run  # noqa: E402
from research.studies.quality_momentum.internal.growth_basis import growth_features, freeze_growth
from research.studies.quality_momentum.internal.build_growth_basis import TARGET, render
from research.studies.quality_momentum.internal.analyze_exports import analyze_exports


def financials(code='A'):
    days = pd.date_range('2022-12-31', periods=8, freq='QE')
    return pd.DataFrame([dict(code=code, statDate=day, pubDate=day+pd.Timedelta(days=25),
        metric=metric, value=(prior if i < 4 else current))
        for metric, prior, current in [('net_profit', 10., 12.),
            ('np_parent_company_owners', 8., 9.6), ('adjusted_profit', 6., 9.)]
        for i, day in enumerate(days)])


def test_separate_total_parent_and_adjusted_ttm_and_fixed_threshold():
    features = growth_features(financials(), ['A'], '2025-01-01')
    row = features.loc['A']
    assert row.ttm_total == pytest.approx(20)
    assert row.ttm_parent == pytest.approx(20)
    assert row.ttm_adjusted == pytest.approx(50)
    features['np_yoy_pct'], features['stat_date'] = 10., '2024-09-30'
    frozen = freeze_growth(features)
    assert not frozen.loc['A', 'pass_baseline']
    assert frozen.loc['A', 'common'] and frozen.loc['A', 'pass_ttm_adjusted']
    features['stat_date'] = '2024-06-30'
    assert not freeze_growth(features).loc['A', 'common']


@pytest.mark.parametrize('problem,status', [('future','invalid_visibility'), ('gap','missing_quarters'),
    ('duplicate','duplicate_report'), ('zero','nonpositive_prior'), ('nan','invalid_value')])
def test_no_fallback_for_invalid_adjusted_profit(problem, status):
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
    row = growth_features(raw, ['A'], '2025-01-01').loc['A']
    assert row.ttm_adjusted_status == status and pd.isna(row.ttm_adjusted)
    assert row.ttm_total == pytest.approx(20)


def test_growth_file_is_standalone_and_current():
    source = render()
    assert source == TARGET.read_text(encoding='utf-8')
    assert not any(isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('research')
                   for n in ast.walk(ast.parse(source)))


def test_growth_expanded_pool_callbacks_and_raw_export_audit(world, tmp_path):
    stub, _, codes, cal = probe(world)
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 0.
    fields = {name: object() for name in ('net_profit', 'np_parent_company_owners', 'adjusted_profit')}
    stub._module.income = SimpleNamespace(statDate=object(), pubDate=object(),
        net_profit=fields['net_profit'], np_parent_company_owners=fields['np_parent_company_owners'])
    stub._module.indicator.adjusted_profit = fields['adjusted_profit']
    stub.date_tables['indicator']['statDate'] = '2024-09-30'
    stub.date_tables['indicator']['pubDate'] = '2024-10-25'

    def history(securities, query_fields, watch_date, count, interval, stat_by_year):
        assert pd.Timestamp(watch_date) < stub.today
        assert count == 8 and interval == '1q' and not stat_by_year
        selected = [name for name, field in fields.items() if any(field is f for f in query_fields)]
        blocks = []
        for code in securities:
            raw = financials(code)
            if code == codes[1]:
                raw = raw.drop(raw.index[raw.metric.eq('adjusted_profit')][0])
            frame = raw[raw.metric.isin(selected)].pivot(index=['code', 'statDate', 'pubDate'], columns='metric', values='value')
            blocks.append(frame.reset_index())
        return pd.concat(blocks, ignore_index=True)

    old = stub._module.get_fundamentals
    def fundamentals(q, date=None, **kwargs):
        frame = old(q, date=date, **kwargs)
        if 'pe_ratio' in frame:
            frame['market_cap'] = 100.
        return frame
    stub._module.get_history_fundamentals = history
    stub._module.get_fundamentals = fundamentals
    module = stub.load_script(TARGET)
    stub.scheduled.clear()
    stub.initialize()
    run(stub, module, cal[270:336])
    module.on_strategy_end(stub.ctx)
    assert stub.g.source_errors == 0
    samples = pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_samples.csv']))
    first = samples[samples.signal_day.eq(str(cal[270].date()))].set_index('code')
    assert len(first) == 60 and not first.at[codes[0], 'pass_baseline']
    assert first.at[codes[0], 'pass_ttm_adjusted']
    assert not first.at[codes[1], 'common']
    assert first.at[codes[1], 'ttm_adjusted_status'] == 'missing_quarters'
    for name, content in stub.exports.items():
        (tmp_path / name).write_text(content, encoding='utf-8')
    prefix = tmp_path / module.OUTPUT_PREFIX
    result = analyze_exports(prefix, tmp_path / 'analysis')
    assert result['summary'].group.eq('ttm_adjusted_minus_baseline').any()
    quarters_path = tmp_path / (module.OUTPUT_PREFIX + '_quarters.csv')
    quarters = pd.read_csv(quarters_path)
    quarters.loc[0, 'value'] *= 2
    quarters.to_csv(quarters_path, index=False)
    with pytest.raises(ValueError, match='不一致'):
        analyze_exports(prefix, tmp_path / 'changed')
