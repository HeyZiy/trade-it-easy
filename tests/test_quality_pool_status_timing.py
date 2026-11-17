"""当天信号使用风险警示证券信息；执行还需要新鲜交易状态。"""
import datetime as dt

import pandas as pd
import pytest

from data_provider.security_names import names_for
from src.quality_pool import config, feeds, signal, state
from test_quality_pool_state import env, _patch_signal_data


def test_signal_uses_today_st_without_intraday_quotes(env):
    _patch_signal_data(env)
    env.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-05 09:30:00'))
    env.setattr(feeds, 'fetch_screening_status', lambda *a: pytest.fail('当日不得使用昨收 ST'))
    env.setattr(feeds, '_raw_code_info', lambda: pd.DataFrame(
        {'security_status': ['4', '']}, index=['600001.SH', '600002.SH']))
    current = state.empty_state()
    signal.run_signal(dt.date(2026, 10, 5), state=current,
                      held_codes={"600001"}, state_writer=lambda _: None)
    plan = current["pending_plan"]
    assert plan['asof'] == '2026-10-02'
    assert plan['exit_reasons'] == {'600001': config.EXIT_OUT_OF_POOL}
    assert plan['selected'] == ['600002']


def test_execution_status_does_not_use_history_table(monkeypatch):
    monkeypatch.setattr(feeds, '_raw_status', lambda *a: pytest.fail('历史表不能确认当天状态'))
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-15 09:31:00'))
    info = pd.DataFrame({'security_status': ['0', '4'], 'symbol': ['示例证券', '平安银行']},
                        index=['600001.SH', '000001.SZ'])
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
    assert names_for(['600001', '000001']) == {'600001': '示例证券', '000001': '平安银行'}


def test_blank_risk_flag_still_yields_tradable_row(monkeypatch):
    """生产形态：正常股 security_status 为空串，当天快照仍须给出可交易行与涨跌停。"""
    monkeypatch.setattr(feeds, '_execution_now', lambda: pd.Timestamp('2026-10-30 09:31:00'))
    info = pd.DataFrame({'security_status': ['']}, index=['002293.SZ'])
    raw = pd.DataFrame({'trade_time': ['2026-10-30 09:30:59'],
                        'trading_phase_code': ['T0      '],
                        'high_limited': [13.04], 'low_limited': [10.67]})
    monkeypatch.setattr(feeds, '_raw_execution_data', lambda *a: (info, {'002293': raw}))
    result = feeds.fetch_execution_status(['002293'], '2026-10-30')
    assert pd.notna(result.at['002293', 'is_st']) and not result.at['002293', 'is_st']
    assert not result.at['002293', 'is_suspended']
    assert feeds.day_limits(['002293'], '2026-10-30', {}, status=result)['002293'] == (13.04, 10.67)


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


@pytest.mark.parametrize('flag', [None, 'unknown', '0001000000000000'])
def test_unknown_risk_warning_never_means_normal(flag):
    assert pd.isna(feeds._risk_warning(flag))


@pytest.mark.parametrize('flag', ['', '  ', [], '0'])
def test_blank_risk_warning_means_no_warning(flag):
    """SDK 对正常股返回空串（生产 5010/5223 行）；空串是无标志，不是未知。"""
    assert feeds._risk_warning(flag) is False


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
