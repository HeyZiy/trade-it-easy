"""持仓段研究、固定期限边界与聚宽只读导出的语义验证。"""
import ast
import importlib.util
import json
import subprocess
import sys
import types
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest


FOLDER = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation'


def run_js(body):
    imports = (f'import {{episodeMetrics,summarize}} from {json.dumps((FOLDER / "study_etf_fit.mjs").as_uri())};\n'
               f'import {{evaluate}} from {json.dumps((FOLDER / "study_etf_fit_forward.mjs").as_uri())};\n'
               'import assert from "node:assert/strict";\n')
    result = subprocess.run(['node', '--input-type=module', '-'], input=imports + body,
                            text=True, capture_output=True, encoding='utf-8')
    assert result.returncode == 0, result.stdout + result.stderr


def test_holding_metrics_and_open_segment_exclusion():
    run_js('''
const e=episodeMetrics({index:0,code:'A',name:'A',entry:'2020-01-01',exit:'2020-01-03',
closed:true,entryDayIndex:0,exitDayIndex:2,closeHoldingDays:2,entryNotional:100,
netPnl:-1,logContribution:0,path:[.02,.05,-.01].map(returnOnEntryNotional=>({returnOnEntryNotional}))});
assert.equal(e.returnOnEntryNotional,-.01);
assert.equal(e.observedMae,-.01);
assert.equal(e.observedMfe,.05);
assert.ok(Math.abs(e.observedMaxDrawdown-(1-.99/1.05))<1e-12);
assert.ok(Math.abs(e.observedPeakGiveback-.06)<1e-12);
const s=summarize([e,{...e,closed:false,returnOnEntryNotional:10}]);
assert.equal(s.closedEpisodes,1);assert.equal(s.openEpisodes,1);
assert.equal(s.meanReturn,-.01);assert.equal(s.quickLosingExitWithin3DaysRate,1);
assert.equal(summarize([{...e,closed:false}]).medianReturn,null);
''')


FORWARD_WORLD = '''
const dates=Array.from({length:37},(_,i)=>new Date(Date.UTC(2015,11,1+i)).toISOString().slice(0,10));
const calendar=dates.slice(25);
const meta={schema_version:1,source:'JoinQuant strategy backtest export',export_complete:true,
errors:[],codes:['A','B'],as_of:calendar.at(-1),expected_signal_dates:calendar};
const signals=calendar.flatMap((date,i)=>['A','B'].map(code=>({date,code,
decision_price:String(100+25+i+(25+i)%3*.2),eligible:'1',paused:'0',
rank:String(i===0?(code==='A'?1:2):(code==='B'?1:2))})));
const market=dates.flatMap((date,i)=>['A','B'].map(code=>({date,code,
close_raw:String(100+i+i%3*.2),close_post:String(2*(100+i+i%3*.2)),
volume:'10000',money:'1000000',paused:'0'})));
'''


def test_fixed_ten_days_continues_after_rank_exit_and_adjustment_is_consistent():
    run_js(FORWARD_WORLD + '''
const r=evaluate(meta,signals,market);
assert.equal(r.events.length,2);
const a=r.events[0], b=r.events[1];
assert.equal(a.observedRankDays,1);assert.equal(a.rankRunCensored,false);
assert.equal(a.forwardStatus,'complete');assert.equal(b.forwardStatus,'complete');
assert.equal(b.rankRunCensored,true);
const expected=(100+35+35%3*.2)/(100+25+25%3*.2)-1;
assert.ok(Math.abs(a.return10-expected)<1e-12);
assert.ok(a.volatility20>0);
const split=market.map(x=>({...x,close_raw:x.date>=calendar[5]?String(Number(x.close_raw)/2):x.close_raw}));
assert.equal(evaluate(meta,signals,split).events[0].return10,a.return10);
''')


@pytest.mark.parametrize('mutation,expected', [
    ("const changed=market.filter(x=>!(x.code==='A'&&x.date===calendar[2]));", 'missing_price'),
    ("const changed=market.map(x=>x.code==='A'&&x.date===calendar[2]?{...x,paused:'1'}:x);", 'paused_or_zero_activity'),
])
def test_forward_window_does_not_fill_missing_or_paused_days(mutation, expected):
    run_js(FORWARD_WORLD + mutation + f"assert.equal(evaluate(meta,signals,changed).events[0].forwardStatus,'{expected}');")


def test_export_censoring_calendar_and_future_boundaries():
    run_js(FORWARD_WORLD + '''
const modified=signals.map(x=>x.date>=calendar[5]?{...x,rank:x.code==='A'?'1':'2'}:x);
assert.equal(evaluate(meta,modified,market).events.at(-1).forwardStatus,'right_censored');
assert.throws(()=>evaluate({...meta,export_complete:false},signals,market),/Incomplete/);
assert.throws(()=>evaluate(meta,signals.slice(2),market),/calendar/);
assert.throws(()=>evaluate(meta,signals,[...market,{...market[0],date:'2099-01-01'}]),/boundary/);
assert.throws(()=>evaluate(meta,signals,[...market,market[0]]),/Duplicate/);
''')


def test_same_year_control_excludes_initial_state_and_matches_price_basis():
    run_js(FORWARD_WORLD + '''
const {compare}=await import('./research/studies/etf_rotation/study_etf_fit_control.mjs');
const withScores=signals.map(x=>({...x,score:'1'}));
const r=compare(meta,withScores,market);
assert.equal(r.method.excludedInitialStates,1);
assert.equal(r.method.completeTransitions,1);
assert.equal(r.method.dailyStatusCounts.complete,4);
const b=r.summary.find(x=>x.code==='B'&&x.control==='all_days');
assert.equal(b.matchedSignals,1);assert.equal(b.controlDays,2);
const a0=(100+35+35%3*.2)/(100+25+25%3*.2)-1;
const a1=(100+36+36%3*.2)/(100+26+26%3*.2)-1;
assert.ok(Math.abs(b.signalMeanReturn10-a1)<1e-12);
assert.ok(Math.abs(b.matchedControlMeanReturn10-(a0+a1)/2)<1e-12);
const non=r.summary.find(x=>x.code==='B'&&x.control==='not_rank1');
assert.equal(non.controlDays,1);
assert.ok(Math.abs(non.matchedControlMeanReturn10-a0)<1e-12);
assert.equal(r.overall[0].matchedSignals,1);
const missing=withScores.map(x=>x.code==='B'?{...x,score:''}:x);
const unavailable=compare(meta,missing,market).summary.find(x=>x.code==='B'&&x.control==='all_days');
assert.equal(unavailable.matchedSignals,0);assert.equal(unavailable.unmatchedSignals,1);
assert.equal(unavailable.excessMeanReturn10,null);
''')


def test_control_weights_years_by_signal_count_and_preserves_missing_windows():
    run_js('''
const {compare}=await import('./research/studies/etf_rotation/study_etf_fit_control.mjs');
const dates=Array.from({length:40},(_,i)=>`${i<20?'2020':'2021'}-01-${String(i%20+1).padStart(2,'0')}`);
const meta={schema_version:1,source:'JoinQuant strategy backtest export',export_complete:true,
errors:[],codes:['A','B'],as_of:dates.at(-1),expected_signal_dates:dates};
const signals=dates.flatMap((date,i)=>['A','B'].map(code=>({date,code,score:'1',eligible:'1',paused:'0',
decision_price:String(i<20?100:200+i),rank:String((i===1||i===3||i>=20)===(code==='B')?1:2)})));
const market=signals.map(x=>({...x,close_raw:x.decision_price,close_post:x.decision_price,
volume:'1000',money:'100000'}));
const r=compare(meta,signals,market);
const b=r.summary.find(x=>x.code==='B'&&x.control==='all_days');
const y=r.byYear.filter(x=>x.code==='B'&&x.control==='all_days');
assert.equal(y.length,2);assert.equal(y[0].signalCount,2);assert.equal(y[1].signalCount,1);
assert.equal(y[0].controlDays,20);assert.equal(y[1].controlDays,10);
assert.ok(Math.abs(b.matchedControlMeanReturn10-(2*y[0].controlMeanReturn10+y[1].controlMeanReturn10)/3)<1e-12);
assert.equal(b.sampleYears,2);
assert.equal(b.minLeaveOneYearExcessMean,Math.min(...y.map(x=>x.excessMeanReturn10)));
const broken=market.filter(x=>!(x.code==='B'&&x.date===dates[5]));
const changed=compare(meta,signals,broken);
assert.ok(changed.method.dailyStatusCounts.missing_price>0);
assert.equal(changed.method.completeTransitions,r.method.completeTransitions-2);
''')


def test_exporter_preserves_original_strategy_definitions():
    original = ast.parse((FOLDER / 'unified_coverage.py').read_text(encoding='utf-8'))
    exporter = ast.parse((FOLDER / 'study_etf_fit_jq.py').read_text(encoding='utf-8'))
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
