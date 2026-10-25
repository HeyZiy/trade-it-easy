"""质量池的故障恢复/未知数据/行业约束；合成数据不代表策略收益。"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

import quality_pool as qp
from src.quality_pool import config as pool_config
from src.quality_pool import feeds, screener, signal, state, execution
from test_quality_pool_state import (
    env, _patch_signal_data, _patch_execute_data, _seed_positions, _ledger_snapshot,
)


def test_missing_values_defer_holding_without_rebalancing_and_recover_next_day(env):
    _patch_signal_data(env)
    _seed_positions([('600001', 25000, 10.0)])
    original = feeds.fetch_fundamentals

    def missing(codes, asof, raw_closes=None):
        frame = original(codes, asof, raw_closes)
        frame.loc['600001', 'pe_ttm'] = np.nan
        return frame

    env.setattr(feeds, 'fetch_fundamentals', missing)
    signal.run_signal(dt.date(2026, 10, 2))
    plan = state.load_state(pool_config.STATE_PATH)['pending_plan']
    assert '600001' in plan['selected'] and '600001' in plan['deferred']
    assert '600001' not in plan['exit_reasons']
    _patch_execute_data(env, {'600001': 10., '600002': 10.})
    qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts()['600001'] == 25000
    _patch_signal_data(env)
    signal.run_signal(dt.date(2026, 10, 5))
    saved = state.load_state(pool_config.STATE_PATH)
    assert saved['deferred_codes'] == [] and saved['pending_plan'] is None


def test_deferred_quality_failure_only_adds_exit_without_new_round(env):
    _patch_signal_data(env)
    _seed_positions([('600001', 100, 10.)])
    saved = state.load_state(pool_config.STATE_PATH)
    saved.update(next_signal_date='2026-10-29', deferred_codes=['600001'])
    state.save_state(saved, pool_config.STATE_PATH)
    original = feeds.fetch_fundamentals

    def low_roe(codes, asof, raw_closes=None):
        frame = original(codes, asof, raw_closes)
        frame.loc['600001', 'roe_ttm_pct'] = 5.
        return frame

    env.setattr(feeds, 'fetch_fundamentals', low_roe)
    signal.run_signal(dt.date(2026, 10, 2))
    saved = state.load_state(pool_config.STATE_PATH)
    assert saved['exit_queue'] == {'600001': 'out_of_pool'}
    assert saved['pending_plan'] is None and saved['next_signal_date'] == '2026-10-29'


def test_suspended_quality_holding_is_retained_and_execution_blocked(env):
    _patch_signal_data(env)
    _seed_positions([('600001', 100, 10.)])
    original = feeds.fetch_screening_status

    def suspended(codes, day):
        frame = original(codes, day)
        frame.loc['600001', 'is_suspended'] = True
        return frame

    env.setattr(feeds, 'fetch_screening_status', suspended)
    signal.run_signal(dt.date(2026, 10, 2))
    plan = state.load_state(pool_config.STATE_PATH)['pending_plan']
    assert '600001' in plan['kept'] and plan['exit_reasons'] == {}
    _patch_execute_data(env, {'600001': 10., '600002': 10.}, suspended={'600001'})
    qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts()['600001'] == 100
    assert state.load_state(pool_config.STATE_PATH)['exit_queue'] == {}


def test_old_holding_missing_momentum_does_not_pause_round(env):
    _patch_signal_data(env)
    _seed_positions([('600001', 100, 10.)])
    original = feeds.fetch_closes

    def missing(codes, asof, rows=253):
        frame = original(codes, asof, rows)
        frame.iloc[0, frame.columns.get_loc('600001')] = np.nan
        return frame

    env.setattr(feeds, 'fetch_closes', missing)
    signal.run_signal(dt.date(2026, 10, 2))
    plan = state.load_state(pool_config.STATE_PATH)['pending_plan']
    assert plan is not None and '600001' in plan['kept'] and '600002' in plan['selected']


def test_known_quality_failure_wins_over_missing_valuation():
    code = '600001'
    audit = {code: {}}
    fund = pd.DataFrame({'roe_ttm_pct': [5.], 'np_yoy_pct': [20.],
                         'pe_ttm': [np.nan], 'pb': [2.]}, index=[code])
    status = pd.DataFrame({'is_st': [False]}, index=[code])
    screener.build_pool({code}, fund, status, pd.DataFrame({code: [10.] * 253}), audit=audit)
    assert audit[code]['pool_state'] == 'failed' and audit[code]['pool_reason'] == 'roe_low'


def test_future_adjustment_factor_cannot_backfill_earlier_prices():
    index = pd.date_range('2026-09-28', periods=3)
    factors = pd.DataFrame({'600001.SH': [1.]}, index=index[-1:])
    assert feeds._factor_series(factors, '600001', index) is None


def test_unknown_status_flag_remains_unknown(monkeypatch):
    monkeypatch.setattr(feeds, '_raw_status', lambda codes, day: pd.DataFrame(
        {'MARKET_CODE': ['600001.SH'], 'IS_ST_SEC': [np.nan], 'IS_SUSP_SEC': [np.nan]}))
    frame = feeds.fetch_screening_status(['600001'], '2026-10-01')
    assert pd.isna(frame.at['600001', 'is_st']) and pd.isna(frame.at['600001', 'is_suspended'])


def test_industry_membership_respects_cutoff_and_ambiguous_membership(monkeypatch):
    monkeypatch.setattr(feeds, '_raw_industry_base', lambda: pd.DataFrame(
        {'INDEX_CODE': ['A', 'B', 'C'], 'LEVEL_TYPE': [1, 1, 2]}))
    frames = {
        'A': pd.DataFrame({'CON_CODE': ['600001.SH', '600002.SH', '600003.SH'],
                           'INDATE': ['2020-01-01'] * 3,
                           'OUTDATE': ['2026-10-01', None, None]}),
        'B': pd.DataFrame({'CON_CODE': ['600001.SH', '600003.SH'],
                           'INDATE': ['2026-10-01', '2020-01-01'], 'OUTDATE': [None, None]}),
    }
    monkeypatch.setattr(feeds, '_raw_industry_members', lambda c: frames[c])
    assert feeds.fetch_industries(['600001', '600002', '600003'], '2026-09-30') == {
        '600001': 'A', '600002': 'A'}
    assert feeds.fetch_industries(['600001'], '2026-10-01') == {'600001': 'B'}


def test_blocked_exit_still_counts_toward_industry_limit():
    snap = execution.ExecSnapshot('2026-10-02', {'600001': 100, '600002': 100},
        {'600001': 100, '600002': 100}, 100000., 102000.,
        {c: 10. for c in ['600001', '600002', '600003']}, {}, {'600001': True}, {}, {})
    plan = execution.plan_round(['600002', '600003'], {'600001': 'out_of_pool'},
        10000., snap, industries={c: 'A' for c in ['600001', '600002', '600003']})
    assert not any(t.side == 'buy' and t.code == '600003' for t in plan.trades)
    assert any(b.code == '600003' and b.blocked_by == 'industry_cap' for b in plan.blocked)


def test_same_day_repeated_signal_and_execution_do_not_rebuy(env):
    _patch_signal_data(env)
    signal.run_signal(dt.date(2026, 10, 2))
    _patch_execute_data(env, {'600001': 10., '600002': 10.})
    qp.run_execute(dt.date(2026, 10, 2))
    before = _ledger_snapshot().held_counts()
    signal.run_signal(dt.date(2026, 10, 2), retry=True)
    qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts() == before
