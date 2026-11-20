# -*- coding: utf-8 -*-
"""策略收益曲线：份额核算剔除本金划转，质量动量基准锚定策略首个有效净值日。

钉死两条口径：
1. 现金划转/持仓划转/现金分配/入出金不改变策略净值（只调份额）；
2. 划转更正只刷新策略曲线（strategy_revision 含划转），账户曲线有效性不变。
"""

from datetime import date
from uuid import uuid4

import pytest

from src import account_valuation
from src.etf_account import (
    account_data, backfill_benchmarks, collect_close, initialize, load, record_event, revision,
)

INDEX_CLOSES = {'2025-01-03': 5000.0, '2025-01-06': 5050.0, '2025-01-07': 4950.0}


@pytest.fixture(autouse=True)
def account_day(monkeypatch):
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 7))


@pytest.fixture
def account(tmp_path):
    path = tmp_path / 'data' / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2, strategy='core')],
               targets={'588000': .4}, budgets={'core': .6, 'quality': .4},
               cash_allocations={'core': 600, 'quality': 400})
    return path


def event(path, side, **fields):
    return dict(request_id=str(uuid4()), revision=revision(load(path)),
                date='2025-01-06', side=side) | fields


def index_feed(codes, day):
    return {c: INDEX_CLOSES[day] for c in codes if day in INDEX_CLOSES}


def histories(view):
    return {s['id']: {row['date']: row for row in s['history']} for s in view['strategies']}


def test_initial_navs_and_benchmark_names(account):
    view = account_data(account)
    rows = histories(view)
    assert rows['core']['2025-01-04']['nav'] == pytest.approx(1.0)
    assert rows['quality']['2025-01-04']['nav'] == pytest.approx(1.0)
    assert rows['rotation']['2025-01-04']['nav'] is None  # 未入金的策略不画曲线
    names = {s['id']: s['benchmark_name'] for s in view['strategies']}
    assert names == {'core': None, 'rotation': None, 'quality': '中证800', 'bond': None}


def test_transfers_and_allocation_do_not_move_navs(account):
    record_event(account, event(account, 'cash_transfer', from_strategy='core', strategy='rotation', amount=300))
    record_event(account, event(account, 'buy', code='510300', qty=100, price=3, fee=0, strategy='rotation'))
    record_event(account, event(account, 'assign', from_strategy='core', strategy='quality', code='588000', qty=100))
    record_event(account, event(account, 'cash_assign', cash_allocations={'core': 100, 'quality': 100, 'rotation': 100}))
    state = collect_close(account, '2025-01-06', lambda *_: {'588000': 2, '510300': 3})
    navs = state['records'][-1]['strategy_navs']
    assert navs['core'] == pytest.approx(1.0)
    assert navs['quality'] == pytest.approx(1.0)
    assert navs['rotation'] == pytest.approx(1.0)
    assert state['records'][-1]['nav'] == pytest.approx(1.0)


def test_deposit_withdraw_keep_nav_dividend_raises_it(account):
    record_event(account, event(account, 'deposit', amount=100, strategy='quality'))
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    record_event(account, event(account, 'dividend', amount=50, strategy='quality', date='2025-01-07'))
    record_event(account, event(account, 'withdraw', amount=50, strategy='quality', date='2025-01-07'))
    state = collect_close(account, '2025-01-07', lambda *_: {'588000': 2})
    rows = histories(account_data(account))
    assert rows['quality']['2025-01-06']['nav'] == pytest.approx(1.0)
    assert rows['quality']['2025-01-07']['nav'] == pytest.approx(1.1)
    assert state['records'][-1]['strategy_navs']['quality'] == pytest.approx(1.1)


def test_transfer_into_bond_starts_strategy_nav_without_moving_account_nav(account):
    before = collect_close(account, '2025-01-06', lambda *_: {'588000': 2})['records'][-1]
    record_event(account, event(account, 'cash_transfer', from_strategy='core', strategy='bond', amount=200))
    after = collect_close(account, '2025-01-06', lambda *_: {'588000': 2})['records'][-1]
    assert after['equity'] == before['equity']
    assert after['nav'] == before['nav']
    assert after['cash_by_strategy']['core'] == 400
    assert after['cash_by_strategy']['bond'] == 200
    assert after['strategy_navs']['core'] == before['strategy_navs']['core']
    assert before['strategy_navs']['bond'] is None and after['strategy_navs']['bond'] == 1.0


def test_quality_benchmark_anchors_at_first_valid_nav(account):
    state = collect_close(account, '2025-01-06', lambda *_: {'588000': 2}, index_prices=index_feed)
    frozen = state['strategy_benchmarks']['quality']
    assert frozen == {'name': '中证800', 'index': '000906',
                      'baseline_date': '2025-01-06', 'baseline_price': 5050.0}
    row = state['records'][-1]
    assert row['strategy_benchmark_navs'] == {'quality': pytest.approx(1.0)}
    state = collect_close(account, '2025-01-07', lambda *_: {'588000': 2}, index_prices=index_feed)
    assert state['records'][-1]['strategy_benchmark_navs']['quality'] == pytest.approx(4950 / 5050)
    rows = histories(account_data(account))
    assert rows['quality']['2025-01-07']['benchmark_nav'] == pytest.approx(4950 / 5050)
    assert rows['rotation']['2025-01-07']['benchmark_nav'] is None


def test_backfill_recomputes_old_records_and_anchors_at_initial(account):
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    collect_close(account, '2025-01-07', lambda *_: {'588000': 2})
    state = load(account)
    for record in state['records']:  # 模拟旧 schema：无策略成绩字段
        for key in ('strategy_navs', 'strategy_benchmark_navs', 'strategy_revision'):
            record.pop(key, None)
    from src.etf_account import _save
    _save(account, state)
    state = backfill_benchmarks(account, index_feed)
    rows = {r['date']: r for r in state['records']}
    # 期初就有质量策略现金 → 基准锚定期初日（周六收敛到周五收盘）。
    assert state['strategy_benchmarks']['quality']['baseline_date'] == '2025-01-03'
    assert rows['2025-01-04']['strategy_navs']['quality'] == pytest.approx(1.0)
    assert rows['2025-01-04']['strategy_benchmark_navs']['quality'] == pytest.approx(1.0)
    assert rows['2025-01-06']['strategy_benchmark_navs']['quality'] == pytest.approx(5050 / 5000)
    assert rows['2025-01-07']['strategy_benchmark_navs']['quality'] == pytest.approx(4950 / 5000)
    assert rows['2025-01-06']['benchmark_nav'] == pytest.approx(5050 / 5000)
    # 幂等：再跑一遍不重算。
    before = {r['date']: r.get('strategy_revision') for r in state['records']}
    state = backfill_benchmarks(account, index_feed)
    assert {r['date']: r.get('strategy_revision') for r in state['records']} == before


def test_transfer_edit_refreshes_strategy_curve_but_not_account_curve(account):
    collect_close(account, '2025-01-06', lambda *_: {'588000': 2})
    record_event(account, event(account, 'cash_transfer', from_strategy='core', strategy='quality', amount=200))
    view = account_data(account)
    assert view['history'][-1]['nav'] == pytest.approx(1.0)  # 账户成绩不受划转影响
    rows = histories(view)
    assert rows['quality']['2025-01-06']['nav'] is None  # 策略指纹已过期
    state = backfill_benchmarks(account, lambda *_: {})
    row = next(r for r in state['records'] if r['date'] == '2025-01-06')
    assert row['strategy_navs']['quality'] == pytest.approx(1.0)
    assert row['strategy_navs']['core'] == pytest.approx(1.0)
    rows = histories(account_data(account))
    assert rows['quality']['2025-01-06']['nav'] == pytest.approx(1.0)


def test_incomplete_record_gates_strategy_navs(account):
    state = collect_close(account, '2025-01-06', lambda *_: {})
    row = state['records'][-1]
    assert row['issues'] and row['nav'] is None
    assert row['strategy_navs'] == dict.fromkeys(account_valuation.portfolio_config.STRATEGY_NAMES)
    rows = histories(account_data(account))
    assert rows['core']['2025-01-06']['nav'] is None
