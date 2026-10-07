"""ROE研究池与原策略候选名单的一致性及共享行情查询验证。"""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'research/tools/single_factor'))
sys.path.insert(0, str(ROOT / 'research/studies/roe_quality'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from roe_pool import ROECandidatePool
from single_factor_test import Config, run_study
from test_platform_roe_rotation_revisions import load_world, fundamentals


SOURCE = ROOT / 'research/studies/roe_quality/roe_rotation_v1_1_2.py'


def world():
    codes = ['600%03d.XSHG' % i for i in range(12)] + ['300001.XSHE', '688001.XSHG']
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    rows.loc[rows.code == codes[1], 'roe'] = 2.0
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10 * (1 + (i + 1) * .0002) ** np.arange(100), index=cal)
              for i, c in enumerate(codes)}
    closes[codes[6]] = pd.Series(10 + np.tile([1, -1], 50), index=cal)
    closes[codes[7]].iloc[:60] = np.nan  # 不足61根，不应被接受
    # 最新信号日的异常收盘不能进入波动率或涨幅。
    closes[codes[0]].iloc[-1] = 1000
    stub, mod = load_world('roe_rotation_v1_1_2.py', rows, closes)
    val = stub.date_tables['valuation']
    val['market_cap'] = np.arange(len(codes)) + 10.0
    val.loc[val.code == codes[2], 'pe_ratio'] = -5.0  # 原版缺少正下界，此轮保持
    val.loc[val.code == codes[8], 'pb_ratio'] = 6.0
    stub.set_extras('is_st', {codes[3]: True})
    stub.money[codes[4]].iloc[-1] = 0
    stub.set_locked_shares(pd.DataFrame({'code': [codes[5]],
                                        'day': [stub.calendar[-1] + pd.Timedelta(days=10)]}))
    api = stub._module
    calls = []

    def get_price(securities, **kwargs):
        calls.append((list(securities), kwargs))
        field = kwargs['fields'][0]
        dates = stub.calendar[stub.calendar <= pd.Timestamp(kwargs['end_date']).normalize()]
        if 'start_date' in kwargs:
            dates = dates[dates >= pd.Timestamp(kwargs['start_date']).normalize()]
        else:
            dates = dates[-kwargs['count']:]
        rows = []
        for c in securities:
            prices = stub.closes[c] if c in stub.closes else pd.Series(100.0, index=stub.calendar)
            for day in dates:
                if field == 'paused':
                    value = int(stub.money[c].loc[day] <= 0)
                else:
                    value = prices.loc[day]
                rows.append({'code': c, 'time': day, field: value})
        return pd.DataFrame(rows)

    api.get_price = get_price
    api.get_industry = lambda securities, date: {
        c: {'sw_l1': {'industry_code': 'A' if i % 2 else 'B'}}
        for i, c in enumerate(securities)}
    return stub, mod, api, calls, codes


def test_provider_matches_original_ranked_pool_and_historical_dates():
    stub, mod, api, calls, codes = world()
    expected = mod.build_signal(stub.ctx)
    pool = ROECandidatePool(api, SOURCE)
    asof, signal = stub.calendar[-2], stub.calendar[-1]
    assert pool(api, asof, signal) == expected
    assert codes[2] in expected  # 没有暗中修正负PE，保持对照可比性
    for c in [codes[1], codes[3], codes[4], codes[5], codes[6], codes[7], codes[8], codes[-1], codes[-2]]:
        assert c not in expected
    assert pool.history_asof == asof
    assert pool.history_closes.index.max() == asof
    paused_calls = [k for _, k in calls if k['fields'] == ['paused']]
    history_calls = [k for _, k in calls if k['fields'] == ['close']]
    assert paused_calls[0]['end_date'] == signal
    assert history_calls[0]['end_date'] == asof
    assert history_calls[0]['count'] == 61
    assert pool.audit == []


def test_original_fail_open_is_retained_and_visible_in_audit():
    stub, mod, api, calls, codes = world()

    def failure(**kwargs):
        raise RuntimeError('simulated unavailable unlock data')

    api.get_locked_shares = failure
    pool = ROECandidatePool(api, SOURCE)
    with pytest.warns(RuntimeWarning, match='get_locked_shares'):
        ranked = pool(api, stub.calendar[-2], stub.calendar[-1])
    assert codes[5] in ranked
    assert pool.audit[0]['legacy_fail_open'] is True
    assert pool.audit[0]['operation'] == 'get_locked_shares'


def test_provider_checks_asof_and_refuses_history_from_future():
    stub, mod, api, calls, codes = world()
    pool = ROECandidatePool(api, SOURCE)
    with pytest.raises(ValueError, match='previous trading day'):
        pool(api, stub.calendar[-3], stub.calendar[-1])
    original = api.get_price

    def leak(codes, **kwargs):
        result = original(codes, **kwargs)
        if kwargs['fields'] == ['close']:
            result['time'] = stub.calendar[-1]
            result = result.drop_duplicates('code')
        return result

    api.get_price = leak
    with pytest.raises(ValueError, match='future prices'):
        pool(api, stub.calendar[-2], stub.calendar[-1])


def test_both_windows_share_same_pool_prices_controls_and_future_returns():
    stub, mod, api, calls, codes = world()
    pool = ROECandidatePool(api, SOURCE)
    # 仅一个完整20交易日区间；起点符合该次独立回测第一天的选股规则。
    cfg = Config(start=str(stub.calendar[70].date()), end=str(stub.calendar[99].date()),
                 factor='momentum', frequency='trading_days', rotate_every=20,
                 min_ic_stocks=3, groups=2, neutralize=True)
    tables, snapshots = run_study(cfg, api, pool, lookbacks=(1, 5))
    assert set(tables) == {'momentum_1_raw', 'momentum_1_neutral',
                           'momentum_5_raw', 'momentum_5_neutral'}
    assert len(tables['momentum_1_raw']) == 1
    assert tables['momentum_1_raw']['pool_n'].equals(tables['momentum_5_raw']['pool_n'])
    assert [k['count'] for _, k in calls if 'count' in k] == [61]
    # 只查询一次候选池入场/退出和一次基准入场/退出。
    assert sum(k['fields'] == ['open'] for _, k in calls) == 4
    a = snapshots[snapshots['mode'] == 'momentum_1_raw'].set_index('code')
    b = snapshots[snapshots['mode'] == 'momentum_5_raw'].set_index('code')
    pd.testing.assert_series_equal(a['future_return'], b['future_return'])
    assert not a['factor'].equals(b['factor'])
