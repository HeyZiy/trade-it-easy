"""历史状态用于 T−1 预筛；当天执行必须确认新鲜交易状态。"""
import datetime as dt

import pandas as pd
import pytest

from src.quality_pool import config, feeds, signal, state
from test_quality_pool_state import env, _patch_signal_data, _seed_positions


@pytest.mark.parametrize('deferred', [False, True])
def test_signal_status_uses_previous_trading_day(env, deferred):
    _patch_signal_data(env)
    if deferred:
        _seed_positions([('600001', 100, 10.)])
        st = state.load_state(config.STATE_PATH)
        st.update(next_signal_date='2026-10-20', deferred_codes=['600001'])
        state.save_state(st, config.STATE_PATH)
    original = feeds.fetch_screening_status
    dates = []

    def historical_only(codes, day):
        dates.append(day)
        return original(codes, day) if day == '2026-10-02' else pd.DataFrame()

    env.setattr(feeds, 'fetch_screening_status', historical_only)
    report = signal.run_signal(dt.date(2026, 10, 5))
    assert dates == ['2026-10-02']
    assert '本轮暂停' not in report
    if deferred:
        assert state.load_state(config.STATE_PATH)['deferred_codes'] == []
    else:
        assert state.load_state(config.STATE_PATH)['pending_plan'] is not None


def test_execution_status_does_not_use_history_table(monkeypatch):
    monkeypatch.setattr(feeds, '_raw_status', lambda *a: pytest.fail('历史表不能确认当天状态'))
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-15 09:31:00'))
    info = pd.DataFrame({'security_status': ['0', '4']}, index=['600001.SH', '000001.SZ'])
    snapshots = {
        c: pd.DataFrame({'trade_time': ['2026-10-15 09:30:59'],
                         'trading_phase_code': [phase],
                         'high_limited': [11.], 'low_limited': [9.]})
        for c, phase in [('600001', 'T111    '), ('000001', 'T0      ')]
    }
    monkeypatch.setattr(feeds, '_raw_execution_data', lambda *a: (info, snapshots))
    result = feeds.fetch_execution_status(['600001', '000001'], '2026-10-15')
    assert not result.at['600001', 'is_st']
    assert result.at['000001', 'is_st']
    assert not result['is_suspended'].any()
    assert result.at['600001', 'high_limit'] == 11.


@pytest.mark.parametrize('phase,stamp', [
    ('P011    ', '2026-10-15 09:30:59'),
    ('T111    ', '2026-10-14 09:30:59'),
    ('T111    ', '2026-10-15 09:20:00'),
    ('T111    ', '2026-10-15 09:32:00'),
    ('?', '2026-10-15 09:30:59'),
])
def test_execution_unknown_stale_or_paused_never_passes(monkeypatch, phase, stamp):
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-15 09:31:00'))
    info = pd.DataFrame({'security_status': ['0']}, index=['600001.SH'])
    raw = pd.DataFrame({'trade_time': [stamp], 'trading_phase_code': [phase],
                        'high_limited': [11.], 'low_limited': [9.]})
    monkeypatch.setattr(feeds, '_raw_execution_data', lambda *a: (info, {'600001': raw}))
    result = feeds.fetch_execution_status(['600001'], '2026-10-15')
    assert result.empty or pd.isna(result.at['600001', 'is_suspended']) or result.at['600001', 'is_suspended']


@pytest.mark.parametrize('flag', [None, '', 'unknown', '0001000000000000'])
def test_unknown_risk_warning_never_means_normal(flag):
    assert pd.isna(feeds._risk_warning(flag))


@pytest.mark.parametrize('flag', ['4', '2,4', [2, 4]])
def test_combined_risk_warning_flags(flag):
    assert feeds._risk_warning(flag)


def test_execution_status_failure_returns_no_tradable_rows(monkeypatch):
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-15 09:31:00'))

    def failed(*args):
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(feeds, '_raw_execution_data', failed)
    assert feeds.fetch_execution_status(['600001'], '2026-10-15').empty


def test_execution_cannot_replay_historical_date(monkeypatch):
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-15 09:31:00'))
    monkeypatch.setattr(feeds, '_raw_execution_data', lambda *a: pytest.fail('不应取今天数据重放昨天'))
    assert feeds.fetch_execution_status(['600001'], '2026-10-14').empty
