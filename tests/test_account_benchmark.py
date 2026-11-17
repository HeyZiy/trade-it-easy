# -*- coding: utf-8 -*-
"""投资账户沪深300同期基准：采集计算、看板透出与历史回补。

基准起点锚定期初估值日（非交易日收敛到之前最近交易日，conftest 按周一~五回退）；
指数取数失败只留空基准，不得影响账户净值有效性。
"""

from datetime import date

import pytest

from src.etf_account import (
    account_data, backfill_benchmarks, collect_close, initialize, load,
)

INDEX_CLOSES = {"2025-01-03": 4000.0, "2025-01-06": 4040.0, "2025-01-07": 3960.0}


@pytest.fixture(autouse=True)
def account_day(monkeypatch):
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 7))


@pytest.fixture
def account(tmp_path):
    path = tmp_path / 'data' / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2, strategy='core')],
               targets={'588000': .4}, budgets={'core': 1}, cash_allocations={'core': 1000})
    return path


def index_feed(closes=None):
    closes = INDEX_CLOSES if closes is None else closes
    return lambda codes, day: {c: closes[day] for c in codes if day in closes}


def test_benchmark_ratio_and_frozen_baseline(account):
    state = collect_close(account, '2025-01-06', lambda *_: {'588000': 2},
                          index_prices=index_feed())
    # 期初 2025-01-04 为周六，基准起点收敛到 2025-01-03 收盘。
    assert state['benchmark'] == {'name': '沪深300', 'index': '000300',
                                  'baseline_date': '2025-01-03', 'baseline_price': 4000.0}
    assert state['records'][-1]['benchmark_nav'] == pytest.approx(4040 / 4000)
    # 起点冻结：之后基准价变化不重写已保存的起点。
    state = collect_close(account, '2025-01-07', lambda *_: {'588000': 2},
                          index_prices=index_feed({**INDEX_CLOSES, '2025-01-03': 3000.0}))
    assert state['benchmark']['baseline_price'] == 4000.0
    assert state['records'][-1]['benchmark_nav'] == pytest.approx(3960 / 4000)


def test_index_failure_keeps_account_nav_valid(account):
    state = collect_close(account, '2025-01-06', lambda *_: {'588000': 2},
                          index_prices=lambda *_: {})
    row = state['records'][-1]
    assert row['nav'] == pytest.approx(1.0) and not row['issues']
    assert row['benchmark_nav'] is None
    assert 'benchmark' not in state  # 起点未建立，下次采集重试


def test_no_index_feed_keeps_legacy_behavior(account):
    state = collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    assert state['records'][-1]['benchmark_nav'] is None
    assert state['records'][-1]['nav'] == pytest.approx(1.0)


def test_account_data_exposes_benchmark(account):
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2}, index_prices=index_feed())
    view = account_data(account)
    assert view['benchmark_name'] == '沪深300'
    history = {row['date']: row for row in view['history']}
    assert history['2025-01-06']['benchmark_nav'] == pytest.approx(1.01)
    assert history['2025-01-04']['benchmark_nav'] is None  # 期初记录待回补


def test_backfill_fills_history_without_recollecting(account):
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    collect_close(account, '2025-01-07', lambda *_: {'588000': 3})
    calls = []

    def feed(codes, day):
        calls.append(day)
        return index_feed()(codes, day)

    state = backfill_benchmarks(account, feed)
    rows = {r['date']: r for r in state['records']}
    assert rows['2025-01-04']['benchmark_nav'] == 1.0
    assert rows['2025-01-06']['benchmark_nav'] == pytest.approx(1.01)
    assert rows['2025-01-07']['benchmark_nav'] == pytest.approx(0.99)
    # 期初记录不取数；已补齐后再跑一遍不重复取数。
    assert '2025-01-04' not in calls
    calls.clear()
    backfill_benchmarks(account, feed)
    assert not calls
    view = account_data(account)
    assert all(row['benchmark_nav'] is not None for row in view['history'])


def test_backfill_keeps_missing_dates_for_retry(account):
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    collect_close(account, '2025-01-07', lambda *_: {'588000': 2})
    partial = {k: v for k, v in INDEX_CLOSES.items() if k != '2025-01-07'}
    state = backfill_benchmarks(account, index_feed(partial))
    rows = {r['date']: r for r in state['records']}
    assert rows['2025-01-06']['benchmark_nav'] == pytest.approx(1.01)
    assert rows['2025-01-07']['benchmark_nav'] is None
    state = backfill_benchmarks(account, index_feed())
    rows = {r['date']: r for r in state['records']}
    assert rows['2025-01-07']['benchmark_nav'] == pytest.approx(0.99)


def test_invalid_nav_record_still_gets_benchmark(account):
    collect_close(account, '2025-01-06', lambda *_: {})  # 缺持仓收盘价，nav 无效
    state = backfill_benchmarks(account, index_feed())
    row = next(r for r in state['records'] if r['date'] == '2025-01-06')
    assert row['nav'] is None and row['issues']
    assert row['benchmark_nav'] == pytest.approx(1.01)
    # 无效日期的基准不对外展示，与 nav 同一有效性口径。
    view = account_data(account)
    history = {r['date']: r for r in view['history']}
    assert history['2025-01-06']['benchmark_nav'] is None