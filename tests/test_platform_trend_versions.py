# -*- coding: utf-8 -*-
"""Offline regressions for the standalone JoinQuant trend v4.1 gate_on/gate_off and v5 industry scripts."""

import importlib.util
import sys
import types
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'research' / 'studies' / 'trend'
SCRIPT_PATHS = {
    'trend_v4_1_gate_on': SCRIPT_DIR / 'trend_v4_1_gate_on.py',
    'trend_v4_1_gate_off': SCRIPT_DIR / 'trend_v4_1_gate_off.py',
    'trend_v4': SCRIPT_DIR / 'trend_v4.py',
    'trend_v5_industry': SCRIPT_DIR / 'trend_v5_industry.py',
}
CODE = '000001.XSHE'


def _load_script(monkeypatch, name):
    jq = types.ModuleType('jqdata')
    jq.g = types.SimpleNamespace()
    jq.log = types.SimpleNamespace(
        info=lambda message: None,
        warning=lambda message: None,
        set_level=lambda *args: None,
    )
    for api in ('set_option', 'set_benchmark', 'set_order_cost', 'run_daily'):
        setattr(jq, api, lambda *args, **kwargs: None)
    jq.OrderCost = lambda **kwargs: types.SimpleNamespace(**kwargs)
    monkeypatch.setitem(sys.modules, 'jqdata', jq)
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATHS[name])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.initialize(None)
    return mod


@pytest.fixture(params=['trend_v4_1_gate_on', 'trend_v4_1_gate_off', 'trend_v5_industry'])
def trend(request, monkeypatch):
    return _load_script(monkeypatch, request.param)


def _bars(rising=True):
    closes = [7.0 + 0.05 * i for i in range(61)]
    if not rising:
        closes.reverse()
    return {
        'open': list(closes),
        'high': [value + 0.1 for value in closes],
        'low': [value - 0.1 for value in closes],
        'close': closes,
        # A 100x T-1/T-2 volume ratio must not alter actual T-1 turnover.
        'volume': [1.0] * 60 + [100.0],
    }


def _trade_world(monkeypatch, mod, *, price=14.0, high_limit=100.0,
                 rising=True, turnover=10.0, stub_gate=True):
    context = types.SimpleNamespace(
        current_dt=datetime(2026, 6, 10, 14, 55),
        previous_date=datetime(2026, 6, 9).date(),
        portfolio=types.SimpleNamespace(
            positions={}, total_value=1_000_000.0, available_cash=1_000_000.0,
        ),
    )
    orders, sells, screens = [], [], []
    market = {CODE: types.SimpleNamespace(
        last_price=price, high_limit=high_limit, low_limit=0.0, paused=False,
    )}
    bars = _bars(rising)
    monkeypatch.setattr(mod, 'get_current_data', lambda: market, raising=False)
    monkeypatch.setattr(mod, 'mainboard_universe', lambda date: [(CODE, 'Test')])
    monkeypatch.setattr(mod, 'get_security_name', lambda code: 'Test')
    monkeypatch.setattr(mod, 'fetch_bars', lambda codes: {CODE: bars})

    def screen(codes, prev_day):
        screens.append(prev_day)
        return [(CODE, turnover)]

    monkeypatch.setattr(mod, 'screen_universe', screen)
    monkeypatch.setattr(mod, 'order', lambda code, qty: orders.append((code, qty)),
                        raising=False)
    monkeypatch.setattr(mod, 'order_target', lambda code, qty: sells.append((code, qty)),
                        raising=False)
    if hasattr(mod, 'gate_open_today'):
        if stub_gate:
            monkeypatch.setattr(mod, 'gate_open_today', lambda: True)
    else:
        mod.g.day = 1  # Next call is day 2, between scheduled rebuilds.
        mod.g.up_inds = {'UP'}
        mod.g.ind_map = {CODE: 'UP'}
    return context, orders, sells, screens


def test_previous_turnover_and_current_price_execution(trend, monkeypatch):
    context, orders, _, screens = _trade_world(monkeypatch, trend)
    trend.trade(context)

    # The 10% position cap binds: 100,000 / current 14, rounded to 100 shares.
    assert orders == [(CODE, 7100)]
    assert screens == [context.previous_date]
    assert trend.g.entry_peak[CODE] == 14.0
    assert trend.g.entry_date[CODE] == '2026-06-10'


def test_current_price_at_upper_limit_cannot_buy(trend, monkeypatch):
    context, orders, _, _ = _trade_world(monkeypatch, trend, high_limit=14.0)
    trend.trade(context)
    assert orders == []
    assert CODE not in trend.g.entry_peak


def test_current_price_pullback_keeps_previous_close_signal(trend, monkeypatch):
    context, orders, _, _ = _trade_world(monkeypatch, trend, price=9.4)
    trend.trade(context)
    # T-1 closes remain structurally up even when current price is below their MA20.
    assert orders == [(CODE, 10600)]
    assert trend.g.entry_peak[CODE] == 9.4


@pytest.mark.parametrize('price', [float('nan'), float('inf'), 0.0, -1.0])
def test_invalid_current_price_cannot_buy(trend, monkeypatch, price):
    context, orders, _, _ = _trade_world(monkeypatch, trend, price=price)
    trend.trade(context)
    assert orders == []


def test_current_jump_does_not_turn_downtrend_into_signal(trend, monkeypatch):
    context, orders, _, _ = _trade_world(monkeypatch, trend, price=20.0, rising=False)
    trend.trade(context)
    assert orders == []


def test_v4_1_closed_gate_still_sells(monkeypatch):
    mod = _load_script(monkeypatch, 'trend_v4_1_gate_on')
    context, orders, sells, screens = _trade_world(monkeypatch, mod, price=8.0)
    context.portfolio.positions[CODE] = types.SimpleNamespace(total_amount=1000)
    mod.g.entry_date[CODE] = '2026-06-01'
    mod.g.entry_peak[CODE] = 10.0
    monkeypatch.setattr(mod, 'gate_open_today', lambda: False)
    mod.trade(context)
    assert sells == [(CODE, 0)]
    assert orders == []
    assert screens == []


def test_v4_1_retains_original_factor_ranking(monkeypatch):
    baseline = _load_script(monkeypatch, 'trend_v4')
    corrected = _load_script(monkeypatch, 'trend_v4_1_gate_on')
    for rising in (True, False):
        assert corrected.candidate_metrics(_bars(rising)) == baseline.candidate_metrics(_bars(rising))
    candidates = [('A', -0.01, 2.0), ('B', -0.1, 8.0), ('C', -0.02, 3.0)]
    assert corrected.rank_scores(candidates) == baseline.rank_scores(candidates)
    assert corrected.rank_scores(candidates)[0][1] == 'A'


def test_gate_off_buys_when_original_market_gate_would_block(monkeypatch):
    baseline = _load_script(monkeypatch, 'trend_v4_1_gate_on')
    context, orders, _, screens = _trade_world(monkeypatch, baseline, stub_gate=False)
    baseline.get_current_data()[baseline.GATE_INDEX] = types.SimpleNamespace(last_price=27.4)
    monkeypatch.setattr(baseline, 'attribute_history',
                        lambda *args, **kwargs: pd.DataFrame({'close': [30 - .1 * i for i in range(25)]}),
                        raising=False)
    baseline.trade(context)
    assert orders == [] and screens == []

    off = _load_script(monkeypatch, 'trend_v4_1_gate_off')
    context, orders, _, screens = _trade_world(monkeypatch, off, stub_gate=False)

    def forbidden_market_history(*args, **kwargs):
        pytest.fail('gate_off must not request the market gate index')

    monkeypatch.setattr(off, 'attribute_history', forbidden_market_history, raising=False)
    assert off.USE_MARKET_GATE is False
    off.trade(context)
    assert orders == [(CODE, 7100)]
    assert screens == [context.previous_date]


def test_gate_off_switch_can_restore_original_closed_gate(monkeypatch):
    off = _load_script(monkeypatch, 'trend_v4_1_gate_off')
    context, orders, _, screens = _trade_world(monkeypatch, off, stub_gate=False)
    off.USE_MARKET_GATE = True
    off.get_current_data()[off.GATE_INDEX] = types.SimpleNamespace(last_price=27.4)
    monkeypatch.setattr(off, 'attribute_history',
                        lambda *args, **kwargs: pd.DataFrame({'close': [30 - .1 * i for i in range(25)]}),
                        raising=False)
    off.trade(context)
    assert orders == [] and screens == []


def test_gate_off_still_sells_and_prevents_same_day_rebuy(monkeypatch):
    off = _load_script(monkeypatch, 'trend_v4_1_gate_off')
    context, orders, sells, screens = _trade_world(monkeypatch, off, price=9.4, stub_gate=False)
    context.portfolio.positions[CODE] = types.SimpleNamespace(total_amount=1000)
    off.g.entry_date[CODE], off.g.entry_peak[CODE] = '2026-06-01', 12.0

    def sell(code, amount):
        sells.append((code, amount))
        context.portfolio.positions.pop(code)

    monkeypatch.setattr(off, 'order_target', sell)
    off.trade(context)
    assert screens == [context.previous_date]
    assert sells == [(CODE, 0)]
    assert CODE not in context.portfolio.positions
    assert CODE in off.g.sold_today
    assert orders == []


def test_v5_rebuilds_on_first_sixth_and_eleventh_trading_calls(monkeypatch):
    mod = _load_script(monkeypatch, 'trend_v5_industry')
    context, _, _, _ = _trade_world(monkeypatch, mod)
    mod.g.day = 0
    rebuild_days = []

    def rebuild(date):
        rebuild_days.append(mod.g.day)
        return True

    monkeypatch.setattr(mod, 'rebuild_industry_trend', rebuild)
    for i in range(11):
        context.current_dt = datetime(2026, 6, 1, 14, 55) + timedelta(days=i)
        mod.trade(context)
    assert mod.REBUILD_EVERY == 5
    assert rebuild_days == [1, 6, 11]


def test_v5_failed_rebuild_retries_on_next_trading_call(monkeypatch):
    mod = _load_script(monkeypatch, 'trend_v5_industry')
    context, _, _, _ = _trade_world(monkeypatch, mod)
    mod.g.day = 0
    rebuild_days = []

    def rebuild(date):
        rebuild_days.append(mod.g.day)
        return len(rebuild_days) > 1

    monkeypatch.setattr(mod, 'rebuild_industry_trend', rebuild)
    mod.trade(context)
    assert mod.g.ind_rebuild_pending is True
    context.current_dt += timedelta(days=1)
    mod.trade(context)
    assert rebuild_days == [1, 2]
    assert mod.g.ind_rebuild_pending is False


def _industry_world(monkeypatch, mod):
    up_members = ['00000%d.XSHE' % i for i in range(1, 6)]
    down_members = [up_members[0]] + ['60000%d.XSHG' % i for i in range(1, 5)]
    members = {
        'UP': up_members + [up_members[0], '300001.XSHE'],
        'DOWN': down_members,
        'SMALL': ['600100.XSHG'],
    }
    requests = {'industries': [], 'members': [], 'history': []}

    def industries(*, name, date):
        requests['industries'].append((name, date))
        return pd.DataFrame({'name': list(members)}, index=list(members))

    def stocks(industry, *, date):
        requests['members'].append((industry, date))
        return members[industry]

    def history(count, unit, field, *, security_list):
        requests['history'].append((count, unit, field, list(security_list)))
        return pd.DataFrame({
            code: [100.0 + (1.0 if code in up_members else -0.5) * i
                   for i in range(count)]
            for code in security_list
        })

    monkeypatch.setattr(mod, 'get_industries', industries, raising=False)
    monkeypatch.setattr(mod, 'get_industry_stocks', stocks, raising=False)
    monkeypatch.setattr(mod, 'history', history, raising=False)
    return members, requests


def test_v5_industry_map_and_trend_share_one_pit_snapshot(monkeypatch):
    mod = _load_script(monkeypatch, 'trend_v5_industry')
    members, requests = _industry_world(monkeypatch, mod)
    monkeypatch.setattr(mod, 'CHUNK', 4)
    assert mod.rebuild_industry_trend('2026-06-09') is True

    assert requests['industries'] == [('sw_l1', '2026-06-09')]
    assert requests['members'] == [(ind, '2026-06-09') for ind in members]
    pulled = [code for call in requests['history'] for code in call[3]]
    expected = set(members['UP'] + members['DOWN']) - {'300001.XSHE'}
    assert Counter(pulled) == Counter({code: 1 for code in expected})
    assert len(requests['history']) == 3
    assert all(call[:3] == (45, '1d', 'close') for call in requests['history'])
    assert mod.g.up_inds == {'UP'}
    assert mod.g.ind_map['600100.XSHG'] == 'SMALL'
    assert set(mod.g.ind_map) == expected | {'600100.XSHG'}


@pytest.mark.parametrize('failure', [
    'industries', 'members', 'history', 'empty_history', 'short_history',
])
def test_v5_failed_industry_snapshot_keeps_previous_complete_state(monkeypatch, failure):
    mod = _load_script(monkeypatch, 'trend_v5_industry')
    _industry_world(monkeypatch, mod)
    monkeypatch.setattr(mod, 'CHUNK', 4)
    old_up, old_map = {'OLD'}, {'600999.XSHG': 'OLD'}
    mod.g.up_inds, mod.g.ind_map = old_up, old_map
    api = {'industries': 'get_industries', 'members': 'get_industry_stocks',
           'history': 'history', 'empty_history': 'history',
           'short_history': 'history'}[failure]
    original = getattr(mod, api)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        if failure == 'short_history':
            return original(*args, **kwargs).iloc[:10]
        # Fail after an initial successful member/price request: partial data
        # must never replace just one half of the last complete snapshot.
        if failure != 'industries' and len(calls) == 1:
            return original(*args, **kwargs)
        if failure == 'empty_history':
            return pd.DataFrame()
        raise RuntimeError('simulated request failure')

    monkeypatch.setattr(mod, api, fail)
    assert mod.rebuild_industry_trend('2026-06-09') is False
    assert mod.g.up_inds is old_up
    assert mod.g.ind_map is old_map
    if failure == 'industries':
        assert len(calls) == 1  # No fallback to current, undated industry data.
        assert calls[0][1]['date'] == '2026-06-09'
