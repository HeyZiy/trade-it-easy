"""Correlation diagnosis uses aligned past returns and symmetric future pricing."""
import json
import subprocess
from pathlib import Path

import pytest


MODULE = (Path(__file__).resolve().parents[1]
          / 'research/studies/etf_rotation/study_switch_correlation.mjs')


def run_js(body):
    imports = (f'import {{pearson, correlationBefore, forwardPair, reentryWithin}} '
               f'from {json.dumps(MODULE.as_uri())};\n'
               'import assert from "node:assert/strict";\n')
    result = subprocess.run(['node', '--input-type=module', '-'], input=imports + body,
                            text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


WORLD = '''
const calendar=Array.from({length:150},(_,i)=>new Date(Date.UTC(2020,0,i+1)).toISOString().slice(0,10));
const market=new Map(); let a=100,b=200;
for(const [i,date] of calendar.entries()) {
 if(i){const r=.002+.01*Math.sin(i);a*=Math.exp(r);b*=Math.exp(2*r+.003);}
 for(const [code,price] of [['A',a],['B',b]]) market.set(`${date}|${code}`,
  {close:String(price),event_close:String(price*3),volume:'1000',paused:'0',quality:'ok'});
}
const entry=calendar[125];
'''


def test_pearson_is_of_returns_and_constant_series_are_unknown():
    run_js('''
assert.ok(Math.abs(pearson([1,2,4,3],[3,5,9,7])-1)<1e-14);
assert.ok(Math.abs(pearson([1,2,4,3],[-1,-2,-4,-3])+1)<1e-14);
assert.equal(pearson([1,1,1],[2,3,4]),null);
assert.equal(pearson([1,2,3],[2,NaN,4]),null);
''')


def test_past_correlation_excludes_switch_day_and_future_prices():
    run_js(WORLD + '''
const result=correlationBefore(calendar,market,'A','B',entry,120);
assert.equal(result.correlation_status,'complete');
assert.ok(Math.abs(result.rho-1)<1e-12);
assert.equal(result.history_start,calendar[4]);
assert.equal(result.history_end,calendar[124]);
for(const date of calendar.slice(125)) market.set(`${date}|A`,{close:'1',event_close:'999999',volume:'0'});
assert.deepEqual(correlationBefore(calendar,market,'A','B',entry,120),result);
assert.equal(correlationBefore(calendar,market,'A','B',calendar[120],120).correlation_status,'insufficient_history');
''')


@pytest.mark.parametrize('mutation', [
    "market.delete(`${calendar[20]}|A`);",
    "market.get(`${calendar[20]}|B`).volume='0';",
    "market.get(`${calendar[20]}|A`).paused='1';",
    "market.get(`${calendar[20]}|A`).event_close='';",
])
def test_missing_or_inactive_history_is_not_skipped(mutation):
    run_js(WORLD + mutation + '''
assert.equal(correlationBefore(calendar,market,'A','B',entry,120).correlation_status,'missing_or_inactive_history');
''')


def test_adjustment_scale_and_raw_share_split_do_not_change_correlation():
    run_js(WORLD + '''
const old=correlationBefore(calendar,market,'A','B',entry,120);
for(const [key,row] of market){row.close=String(Number(row.close)/6);row.event_close=String(Number(row.event_close)*11);}
const changed=correlationBefore(calendar,market,'A','B',entry,120);
assert.ok(Math.abs(old.rho-changed.rho)<1e-12);
''')


def test_fixed_forward_pair_uses_same_close_and_counts_switch_cost_once():
    run_js('''
const calendar=Array.from({length:12},(_,i)=>`2020-01-${String(i+1).padStart(2,'0')}`);
const market=new Map(calendar.flatMap((date,i)=>[['A',1.01],['B',1.02]].map(([code,growth])=>[
 `${date}|${code}`,{close:String(100*growth**i),event_close:String(700*growth**i),volume:'1000',paused:'0'}])));
const r=forwardPair(calendar,market,'A','B',calendar[1],5);
assert.equal(r.forward_status,'complete');
assert.ok(Math.abs(r.old_return-(1.01**5-1))<1e-12);
assert.ok(Math.abs(r.new_return-(1.02**5-1))<1e-12);
assert.ok(Math.abs(r.after_switch_cost_advantage-(.9998/1.0002*1.02**5-1.01**5))<1e-12);
assert.equal(forwardPair(calendar,market,'A','B',calendar[9],5).forward_status,'right_censored');
market.delete(`${calendar[3]}|A`);
assert.equal(forwardPair(calendar,market,'A','B',calendar[1],5).forward_status,'missing_or_inactive_path');
''')


def test_returning_to_old_etf_includes_horizon_boundary_and_censors_unknown():
    run_js('''
const days=Array.from({length:12},()=>({positions:[{code:'B'}]}));
days[0].positions=[{code:'A'}];
days[6].positions=[{code:'A'},{code:'C'}];
assert.equal(reentryWithin(days,1,'A',5),true);
assert.equal(reentryWithin(days,1,'A',4),false);
assert.equal(reentryWithin(days,8,'A',5),null);
''')
