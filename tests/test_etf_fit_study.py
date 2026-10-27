"""Holding segments, fixed-horizon boundaries and read-only JoinQuant exports."""
import ast
import importlib.util
import json
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from research.studies.etf_rotation.study_etf_fit import episode_metrics, summarize
from research.studies.etf_rotation.study_etf_fit_forward import evaluate
from research.studies.etf_rotation.study_etf_fit_control import compare


FOLDER = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation'


def test_holding_metrics_and_open_segment_exclusion():
    episode = episode_metrics(dict(
        index=0, code='A', name='A', entry='2020-01-01', exit='2020-01-03',
        closed=True, entryDayIndex=0, exitDayIndex=2, closeHoldingDays=2, entryNotional=100,
        netPnl=-1, logContribution=0,
        path=[dict(returnOnEntryNotional=value) for value in [.02, .05, -.01]]))
    assert episode['returnOnEntryNotional'] == -.01
    assert episode['observedMae'] == -.01
    assert episode['observedMfe'] == .05
    assert episode['observedMaxDrawdown'] == pytest.approx(1 - .99 / 1.05, abs=1e-12)
    assert episode['observedPeakGiveback'] == pytest.approx(.06, abs=1e-12)
    opened = {**episode, 'closed': False, 'returnOnEntryNotional': 10}
    result = summarize([episode, opened])
    assert result['closedEpisodes'] == 1 and result['openEpisodes'] == 1
    assert result['meanReturn'] == -.01
    assert result['quickLosingExitWithin3DaysRate'] == 1
    assert summarize([opened])['medianReturn'] is None


def forward_world():
    dates = [(date(2015, 12, 1) + timedelta(days=i)).isoformat() for i in range(37)]
    calendar = dates[25:]
    metadata = dict(schema_version=1, source='JoinQuant strategy backtest export', export_complete=True,
                    errors=[], codes=['A', 'B'], as_of=calendar[-1], expected_signal_dates=calendar)
    signals = [dict(date=day, code=code, decision_price=str(100 + 25 + i + (25 + i) % 3 * .2),
                    eligible='1', paused='0', rank=str(1 if code == ('A' if i == 0 else 'B') else 2))
               for i, day in enumerate(calendar) for code in ['A', 'B']]
    market = [dict(date=day, code=code, close_raw=str(100 + i + i % 3 * .2),
                   close_post=str(2 * (100 + i + i % 3 * .2)), volume='10000', money='1000000', paused='0')
              for i, day in enumerate(dates) for code in ['A', 'B']]
    return metadata, signals, market, calendar


def test_fixed_ten_days_continues_after_rank_exit_and_adjustment_is_consistent():
    meta, signals, market, calendar = forward_world()
    result = evaluate(meta, signals, market)
    assert len(result['events']) == 2
    a, b = result['events']
    assert a['observedRankDays'] == 1 and a['rankRunCensored'] is False
    assert a['forwardStatus'] == b['forwardStatus'] == 'complete'
    assert b['rankRunCensored'] is True
    assert a['return10'] == pytest.approx((100 + 35 + 35 % 3 * .2) / (100 + 25 + 25 % 3 * .2) - 1, abs=1e-12)
    assert a['volatility20'] > 0
    split = [{**row, 'close_raw': str(float(row['close_raw']) / 2) if row['date'] >= calendar[5] else row['close_raw']}
             for row in market]
    assert evaluate(meta, signals, split)['events'][0]['return10'] == a['return10']


@pytest.mark.parametrize('mutation,expected', [('missing', 'missing_price'), ('paused', 'paused_or_zero_activity')])
def test_forward_window_does_not_fill_missing_or_paused_days(mutation, expected):
    meta, signals, market, calendar = forward_world()
    if mutation == 'missing':
        changed = [row for row in market if not (row['code'] == 'A' and row['date'] == calendar[2])]
    else:
        changed = [{**row, 'paused': '1'} if row['code'] == 'A' and row['date'] == calendar[2] else row for row in market]
    assert evaluate(meta, signals, changed)['events'][0]['forwardStatus'] == expected


def test_export_censoring_calendar_and_future_boundaries():
    meta, signals, market, calendar = forward_world()
    modified = [{**row, 'rank': '1' if row['code'] == 'A' else '2'} if row['date'] >= calendar[5] else row for row in signals]
    assert evaluate(meta, modified, market)['events'][-1]['forwardStatus'] == 'right_censored'
    with pytest.raises(ValueError, match='Incomplete'):
        evaluate({**meta, 'export_complete': False}, signals, market)
    with pytest.raises(ValueError, match='calendar'):
        evaluate(meta, signals[2:], market)
    with pytest.raises(ValueError, match='boundary'):
        evaluate(meta, signals, [*market, {**market[0], 'date': '2099-01-01'}])
    with pytest.raises(ValueError, match='Duplicate'):
        evaluate(meta, signals, [*market, market[0]])


def test_same_year_control_excludes_initial_state_and_matches_price_basis():
    meta, signals, market, _ = forward_world()
    scored = [{**row, 'score': '1'} for row in signals]
    result = compare(meta, scored, market)
    assert result['method']['excludedInitialStates'] == 1
    assert result['method']['completeTransitions'] == 1
    assert result['method']['dailyStatusCounts']['complete'] == 4
    b = next(row for row in result['summary'] if row['code'] == 'B' and row['control'] == 'all_days')
    assert b['matchedSignals'] == 1 and b['controlDays'] == 2
    a0 = (100 + 35 + 35 % 3 * .2) / (100 + 25 + 25 % 3 * .2) - 1
    a1 = (100 + 36 + 36 % 3 * .2) / (100 + 26 + 26 % 3 * .2) - 1
    assert b['signalMeanReturn10'] == pytest.approx(a1, abs=1e-12)
    assert b['matchedControlMeanReturn10'] == pytest.approx((a0 + a1) / 2, abs=1e-12)
    non = next(row for row in result['summary'] if row['code'] == 'B' and row['control'] == 'not_rank1')
    assert non['controlDays'] == 1
    assert non['matchedControlMeanReturn10'] == pytest.approx(a0, abs=1e-12)
    assert result['overall'][0]['matchedSignals'] == 1
    missing = [{**row, 'score': ''} if row['code'] == 'B' else row for row in scored]
    unavailable = next(row for row in compare(meta, missing, market)['summary'] if row['code'] == 'B' and row['control'] == 'all_days')
    assert unavailable['matchedSignals'] == 0 and unavailable['unmatchedSignals'] == 1
    assert unavailable['excessMeanReturn10'] is None


def test_control_weights_years_by_signal_count_and_preserves_missing_windows():
    dates = [f"{'2020' if i < 20 else '2021'}-01-{i % 20 + 1:02d}" for i in range(40)]
    meta = dict(schema_version=1, source='JoinQuant strategy backtest export', export_complete=True,
                errors=[], codes=['A', 'B'], as_of=dates[-1], expected_signal_dates=dates)
    signals = [dict(date=day, code=code, score='1', eligible='1', paused='0',
                    decision_price=str(100 if i < 20 else 200 + i),
                    rank='1' if (i in (1, 3) or i >= 20) == (code == 'B') else '2')
               for i, day in enumerate(dates) for code in ['A', 'B']]
    market = [{**row, 'close_raw': row['decision_price'], 'close_post': row['decision_price'],
               'volume': '1000', 'money': '100000'} for row in signals]
    result = compare(meta, signals, market)
    b = next(row for row in result['summary'] if row['code'] == 'B' and row['control'] == 'all_days')
    years = [row for row in result['byYear'] if row['code'] == 'B' and row['control'] == 'all_days']
    assert len(years) == 2 and [row['signalCount'] for row in years] == [2, 1]
    assert [row['controlDays'] for row in years] == [20, 10]
    assert b['matchedControlMeanReturn10'] == pytest.approx(
        (2 * years[0]['controlMeanReturn10'] + years[1]['controlMeanReturn10']) / 3, abs=1e-12)
    assert b['sampleYears'] == 2
    assert b['minLeaveOneYearExcessMean'] == min(row['excessMeanReturn10'] for row in years)
    broken = [row for row in market if not (row['code'] == 'B' and row['date'] == dates[5])]
    changed = compare(meta, signals, broken)
    assert changed['method']['dailyStatusCounts']['missing_price'] > 0
    assert changed['method']['completeTransitions'] == result['method']['completeTransitions'] - 2


def test_exporter_preserves_strategy_definitions_with_original15_pool():
    original = ast.parse((FOLDER / 'unified_coverage.py').read_text(encoding='utf-8'))
    exporter = ast.parse((FOLDER / 'study_etf_fit_jq.py').read_text(encoding='utf-8'))

    def pool_assignment(tree):
        return next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == 'etf_pool'
                            for t in node.targets))

    original_pool, export_pool = pool_assignment(original), pool_assignment(exporter)
    assert len(ast.literal_eval(export_pool.value)) == 15
    assert ast.literal_eval(original_pool.value) == ast.literal_eval(export_pool.value)
    # 平台主入口与观测导出均冻结原15只；交易逻辑必须一致。
    assert ast.dump(original) == ast.dump(ast.Module(body=exporter.body[:len(original.body)], type_ignores=[]))


def test_joinquant_export_entrypoint_and_no_future_market_reads(monkeypatch):
    api = types.ModuleType('jqdata')
    api.g = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    spec = importlib.util.spec_from_file_location('fit_export_test', FOLDER / 'study_etf_fit_jq.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    outputs, schedules, trades = {}, [], []
    for name in ['set_benchmark', 'set_option', 'set_order_cost', 'set_slippage', 'record']:
        setattr(mod, name, lambda *args, **kwargs: None)
    mod.OrderCost = lambda **kwargs: kwargs
    mod.FixedSlippage = lambda value: value
    mod.run_daily = lambda fn, **kwargs: schedules.append((fn, kwargs))
    mod.log = types.SimpleNamespace(info=lambda *args: None, error=lambda *args: None)
    mod.write_file = lambda name, content, append=False: outputs.__setitem__(name, content)
    ctx = types.SimpleNamespace(current_dt=datetime(2020, 1, 2, 10, 40), portfolio=types.SimpleNamespace(
        starting_cash=100000, total_value=100000))
    mod.initialize(ctx)
    pool = mod.g.strategy.etf_pool
    mod.g.strategy.rank_and_filter = lambda: pool[:2]
    mod.g.strategy._cur = lambda: {c: types.SimpleNamespace(last_price=1.0, paused=False) for c in pool}
    mod.g.strategy.calculate_momentum_score = lambda code: .2
    mod.g.strategy.execute_trade = lambda: trades.append('original_trade')
    mod.trade_handler(ctx)
    assert trades == ['original_trade'] and len(mod.g.fit_signals) == 15
    assert any(fn is mod.trade_handler and kwargs['time'] == '10:40' for fn, kwargs in schedules)
    ctx.current_dt = datetime(2020, 1, 2, 23, 59)

    def price(code, start_date, end_date, fields, fq, **kwargs):
        assert pd.Timestamp(end_date).date() <= ctx.current_dt.date()
        assert pd.Timestamp(start_date).date() < date(2020, 1, 2)
        dates = pd.bdate_range(start_date, end_date)
        values = {field: [1.0] * len(dates) for field in fields}
        if 'paused' in values:
            values['paused'] = [0] * len(dates)
        return pd.DataFrame(values, index=dates)

    mod.get_price = price
    mod.get_trade_days = lambda **kwargs: [date(2020, 1, 2)]
    mod._fit_export(ctx)
    metadata = json.loads(outputs['fit_metadata.json'])
    assert metadata['export_complete'] is True
    assert metadata['signal_rows'] == 15
    assert set(outputs) == {'fit_signals.csv', 'fit_market.csv', 'fit_metadata.json'}

    def unavailable(*args, **kwargs):
        raise RuntimeError('missing adjusted data')

    mod.get_price = unavailable
    mod._fit_export(ctx)
    assert json.loads(outputs['fit_metadata.json'])['export_complete'] is False
