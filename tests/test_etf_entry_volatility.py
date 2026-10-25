"""Entry diagnosis must use past adjusted prices and actual net segment P/L."""
import json
import subprocess
from pathlib import Path

import pytest


MODULE = (Path(__file__).resolve().parents[1]
          / 'research/studies/etf_rotation/study_entry_volatility.mjs')


def run_js(body):
    imports = (f'import {{featuresBeforeEntry, volatilityBand, reconstructMixed, summarize}} '
               f'from {json.dumps(MODULE.as_uri())};\n'
               'import assert from "node:assert/strict";\n')
    result = subprocess.run(['node', '--input-type=module', '-'], input=imports + body,
                            text=True, encoding='utf-8', capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


WORLD = '''
const calendar = Array.from({length:30}, (_,i) => new Date(Date.UTC(2020,0,i+1)).toISOString().slice(0,10));
let price = 100;
const market = new Map(calendar.map((date,i) => {
  if (i) price *= Math.exp(i%2 ? .02 : -.01);
  return [`${date}|A`, {close:String(price), event_close:String(price*3), volume:'1000', paused:'0', quality:'ok'}];
}));
const entry = calendar[25];
'''


def test_volatility_uses_twenty_prior_returns_and_no_entry_or_future_close():
    run_js(WORLD + '''
const result=featuresBeforeEntry(calendar,market,'A',entry);
const expected=Math.sqrt(20*.015**2/19);
assert.ok(Math.abs(result.vol20-expected)<1e-14);
assert.equal(result.feature_end, calendar[24]);
for(const date of calendar.slice(25)) market.set(`${date}|A`,{close:'1',event_close:'999999',volume:'1000',paused:'0'});
assert.deepEqual(featuresBeforeEntry(calendar,market,'A',entry),result);
''')


def test_adjustment_base_and_raw_share_split_do_not_change_features():
    run_js(WORLD + '''
const original=featuresBeforeEntry(calendar,market,'A',entry);
const scaled=new Map([...market].map(([key,value],i)=>[key,{...value,
  close:String(Number(value.close)/(i>=10?6:1)),event_close:String(Number(value.event_close)*7)}]));
const changed=featuresBeforeEntry(calendar,scaled,'A',entry);
assert.ok(Math.abs(original.vol20-changed.vol20)<1e-14);
assert.ok(Math.abs(original.top3_positive_share24-changed.top3_positive_share24)<1e-12);
''')


@pytest.mark.parametrize('mutation', [
    "market.delete(`${calendar[10]}|A`);",
    "market.get(`${calendar[10]}|A`).volume='0';",
    "market.get(`${calendar[10]}|A`).paused='1';",
    "market.get(`${calendar[10]}|A`).event_close='';",
])
def test_missing_or_inactive_history_is_not_filled_or_compacted(mutation):
    run_js(WORLD + mutation + '''
assert.equal(featuresBeforeEntry(calendar,market,'A',entry).feature_status,'missing_or_inactive_history');
''')


def test_common_volatility_boundaries():
    run_js('''
assert.equal(volatilityBand(.009999),'below_1pct');
assert.equal(volatilityBand(.01),'1_to_2pct');
assert.equal(volatilityBand(.019999),'1_to_2pct');
assert.equal(volatilityBand(.02),'at_least_2pct');
''')


def test_switch_reconciles_incoming_commission_and_outgoing_net_profit():
    run_js('''
const p=(code,value,pnl,nav)=>({code,name:code,quantity:10000,cost:10,value,pnl,nav});
const days=[{date:'2020-01-01',cash:-20,positions:[p('A',103000,3000,102980)]},
 {date:'2020-01-02',cash:4939,positions:[p('B',99000,-1000,103939)]}];
const result=reconstructMixed(days);
assert.equal(result.rows[0].net_pnl,4959);
assert.equal(result.rows[0].closed,true);
assert.equal(result.rows[0].holding_days,1);
assert.equal(result.rows[1].net_pnl,-1020);
assert.equal(result.rows[1].closed,false);
assert.equal(result.checks.net_pnl_error,0);
''')


def test_multi_holding_boundary_is_marked_incomplete_and_reconciled():
    run_js('''
const p=(code,value,pnl,nav)=>({code,name:code,quantity:10000,cost:10,value,pnl,nav});
const result=reconstructMixed([
 {date:'2020-01-01',cash:-20,positions:[p('A',103000,3000,102980)]},
 {date:'2020-01-02',cash:0,positions:[p('A',50000,0,100000),p('B',50000,0,100000)]},
 {date:'2020-01-03',cash:0,positions:[p('B',101000,0,101000)]}]);
assert.deepEqual(result.checks.skipped_dates,['2020-01-02','2020-01-03']);
assert.equal(result.rows.every(x=>!x.complete_pnl),true);
assert.equal(result.checks.unallocated_pnl,-1980);
assert.equal(result.checks.net_pnl_error,0);
''')


def test_loss_coverage_normalizes_each_trade_instead_of_mixing_account_sizes():
    run_js('''
const base={code:'A',year:'2020',vol20:.015,top3_positive_share24:.4,
historical_drawdown24:.03,observed_mae:-.02,observed_max_drawdown:.03};
const result=summarize([{...base,net_return:.1,net_pnl:10,holding_days:12},
 {...base,net_return:-.05,net_pnl:-500,holding_days:2}]);
assert.equal(result.return_profit_factor,2);
assert.equal(result.long_winner_loss_coverage,2);
assert.equal(result.quick_loss_3d_rate,.5);
assert.equal(result.net_pnl_yuan,-490);
assert.ok(Math.abs(result.mean_return_excluding_top5_winners+.05)<1e-12);
''')
