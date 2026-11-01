"""现金支撑：季度可见性、冻结分组、真实回调导出及本地复算。"""
import ast
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_quality_pool_v2 import world  # noqa: E402,F401
from test_platform_quality_momentum_turnover_probe import probe, run  # noqa: E402
from research.studies.quality_momentum.internal.cash_support import cash_support_features
from research.studies.quality_momentum.internal.build_cash_support import render, TARGET
from research.studies.quality_momentum.internal.analyze_exports import analyze_exports


def reports(code='A'):
    dates = pd.to_datetime(['2023-06-30', '2023-09-30', '2023-12-31', '2024-03-31'])
    common = dict(code=[code] * 4, statDate=dates, pubDate=dates + pd.Timedelta(days=25))
    return (pd.DataFrame(dict(common, net_operate_cash_flow=[1., 2., 3., 4.])),
            pd.DataFrame(dict(common, operating_profit=[2., 3., 4., 5.])))


def test_single_quarter_sum_and_cumulative_formula_have_same_economic_result():
    cash, profit = reports()
    actual = cash_support_features(cash, profit, ['A'], '2024-05-01').loc['A']
    assert actual.cash_status == 'ok'
    assert actual.cash_ttm == 10 and actual.profit_ttm == 14
    # 对应累计口径：当年Q1 + 上年全年 - 上年Q1。
    from research.tools.fundamental_analysis.core import get_ttm, safe_div
    cumulative = pd.DataFrame(dict(REPORTING_PERIOD=['20230331', '20231231', '20240331'],
                                   cf=[7., 13., 4.], op=[8., 17., 5.]))
    expected = safe_div(get_ttm(cumulative, 'cf'), get_ttm(cumulative, 'op'))[-1]
    assert actual.cash_support == pytest.approx(expected)


@pytest.mark.parametrize('bad,expected', [
    ('future', 'invalid_visibility'), ('duplicate', 'duplicate_report'),
    ('gap', 'missing_quarters'), ('mismatch', 'report_mismatch'),
    ('zero', 'nonpositive_profit'), ('negative', 'nonpositive_profit'),
    ('nan', 'invalid_value'), ('stale', 'stale_report')])
def test_invalid_financials_are_unknown_without_backfill(bad, expected):
    cash, profit = reports()
    asof = '2024-05-01'
    if bad == 'future':
        cash.loc[3, 'pubDate'] = pd.Timestamp('2024-05-02')
    elif bad == 'duplicate':
        cash = pd.concat([cash, cash.iloc[[0]]], ignore_index=True)
    elif bad == 'gap':
        cash = cash.drop(1)
    elif bad == 'mismatch':
        cash['statDate'] -= pd.offsets.QuarterEnd()
    elif bad == 'zero':
        profit['operating_profit'] = 0.
    elif bad == 'negative':
        profit['operating_profit'] = -1.
    elif bad == 'nan':
        cash.loc[0, 'net_operate_cash_flow'] = np.nan
    elif bad == 'stale':
        asof = '2025-05-01'
    row = cash_support_features(cash, profit, ['A'], asof).loc['A']
    assert row.cash_status == expected
    assert pd.isna(row.cash_support)


def test_generated_file_is_current_and_has_no_local_imports():
    source = render()
    assert source == TARGET.read_text(encoding='utf-8')
    imports = [n.module for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)]
    assert not any(n and n.startswith(('research', 'data_provider')) for n in imports)


def cash_probe(world):
    stub, _, codes, cal = probe(world)
    cash_field, profit_field = object(), object()
    stub._module.cash_flow = SimpleNamespace(statDate=object(), pubDate=object(), net_operate_cash_flow=cash_field)
    stub._module.income = SimpleNamespace(statDate=object(), pubDate=object(), operating_profit=profit_field)
    calls = []

    def history(securities, fields, watch_date, count, interval, stat_by_year):
        assert pd.Timestamp(watch_date) < stub.today
        assert count == 4 and interval == '1q' and not stat_by_year
        calls.append((watch_date, fields[-1]))
        blocks = []
        for code in securities:
            cash, profit = reports(code)
            for frame in (cash, profit):
                frame['statDate'] += pd.offsets.QuarterEnd(2)
                frame['pubDate'] = frame.statDate + pd.Timedelta(days=25)
            if fields[-1] is cash_field:
                # 与动量不同的次序，避免只测到已有动量。
                cash.net_operate_cash_flow *= 1 + (int(code[3:6]) * 7 % 59) / 10
                blocks.append(cash)
            else:
                blocks.append(profit)
        return pd.concat(blocks, ignore_index=True)

    original = stub._module.get_fundamentals

    def fundamentals(q, date=None, **kwargs):
        result = original(q, date=date, **kwargs)
        if 'pe_ratio' in result:
            result['market_cap'] = [50. + int(c[3:6]) for c in result.code]
        return result

    stub._module.get_history_fundamentals = history
    stub._module.get_fundamentals = fundamentals
    module = stub.load_script(TARGET)
    stub.scheduled.clear()
    stub.initialize()
    return stub, module, codes, cal, calls


def test_cash_callbacks_preserve_missing_endpoints_and_recompute_exports(world, tmp_path):
    stub, module, codes, cal, calls = cash_probe(world)
    run(stub, module, cal[270:271])
    frozen = stub.g.pending[0]['frame'].copy()
    assert len(frozen) == 60 and frozen.cash_status.eq('ok').all()
    assert frozen.group_pool_raw.notna().all()
    stub.paused_endpoints.add((cal[330], codes[0]))
    run(stub, module, cal[271:336])
    module.on_strategy_end(stub.ctx)
    assert calls and stub.g.sample_count == 4
    samples = pd.read_csv(StringIO(stub.exports[module.OUTPUT_PREFIX + '_samples.csv']))
    first = samples[samples.signal_day.eq(str(cal[270].date()))].set_index('code')
    assert first.at[codes[0], 'status_r60'] == 'exit_paused'
    assert pd.isna(first.at[codes[0], 'r60'])
    pd.testing.assert_series_equal(first.group_pool_raw, frozen.group_pool_raw,
                                  check_names=False, check_dtype=False)
    for name, content in stub.exports.items():
        (tmp_path / name).write_text(content, encoding='utf-8')
    prefix = tmp_path / module.OUTPUT_PREFIX
    results = analyze_exports(prefix, tmp_path / 'analysis')
    assert results['summary'].group.eq('high_minus_low').any()
    path = tmp_path / (module.OUTPUT_PREFIX + '_quarters.csv')
    quarters = pd.read_csv(path)
    quarters.loc[0, 'value'] *= 2
    quarters.to_csv(path, index=False)
    with pytest.raises(ValueError, match='不一致'):
        analyze_exports(prefix, tmp_path / 'changed')
