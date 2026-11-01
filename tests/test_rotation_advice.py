"""轮动建议使用共享账户事实，绝不把独立模拟或建议当作成交。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier
from uuid import uuid4

import pytest

from src import etf_account as wallet
from src.etf.rotation import RotationRow
from src.etf_account import publish_rotation as publish


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 7))
    path = tmp_path / 'etf_account.json'
    wallet.initialize(path, asof='2025-01-03', cash=3000,
                      positions=[dict(code='588000', name='核心', count=1000, price=2, strategy='core')],
                      budgets={'core': .6, 'rotation': .4}, cash_allocations={'core': 1000, 'rotation': 2000})
    return path


def event(path, side, **fields):
    return wallet.record_event(path, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(path)),
                                         date='2025-01-07', side=side, strategy='rotation', **fields))


def update(path, *, code='510300', price=2, eligible=True, issue=None, dry_run=False):
    row = RotationRow(code, '目标ETF', '国内权益', price, price, 1 if eligible else -1, eligible, 1)
    prices = {code: price, '513100': 2}
    return publish('2025-01-07', [row], prices, {'realtime_missing': 0}, path=path,
                   error=issue, dry_run=dry_run)


def strategy(path):
    return next(row for row in wallet.account_data(path)['strategies'] if row['id'] == 'rotation')


def test_publish_changes_no_money_positions_nav_or_transactions(account):
    before = wallet.account_data(account)
    update(account)
    after = wallet.account_data(account)
    for key in ('cash', 'equity', 'positions', 'trades', 'history'):
        assert after[key] == before[key]
    assert after['revision'] != before['revision']  # 新信号也令旧页面失效。
    buy, = strategy(account)['suggestions']
    assert buy['side'] == 'buy' and buy['qty'] == 900
    assert buy['amount'] + 5 <= 2000
    assert not strategy(account)['positions']


def test_budget_and_cash_both_limit_buy(account):
    event(account, 'cash_assign', cash_allocations={'core': 0, 'rotation': 3000})
    update(account)
    assert strategy(account)['suggestions'][0]['amount'] + 5 <= 2000
    event(account, 'cash_assign', cash_allocations={'core': 2500, 'rotation': 500})
    assert strategy(account)['suggestions'][0]['amount'] + 5 <= 500


def test_switch_only_sells_own_shares_and_uses_net_proceeds(account):
    event(account, 'buy', code='513100', name='旧仓', qty=500, price=2, fee=5)
    # 先让买入份额跨日变为可卖。
    monkey_day = date(2025, 1, 8)
    from unittest.mock import patch
    with patch.object(wallet, '_today', lambda: monkey_day):
        row = RotationRow('510300', '目标', '股票', 2, 2, 1, True, 1)
        publish('2025-01-08', [row], {'510300': 2, '513100': 2}, {}, path=account)
        sell, buy = strategy(account)['suggestions']
    assert (sell['code'], sell['qty']) == ('513100', 500)
    assert buy['amount'] + 5 <= 995 + 1000 - 5
    assert buy['cash_note'] == '需先卖出回款'


def test_failure_supersedes_old_signal_and_dry_run_never_saves(account):
    update(account)
    before = account.read_bytes()
    update(account, issue='行情更新失败，暂停建议', dry_run=True)
    assert account.read_bytes() == before
    update(account, issue='行情更新失败，暂停建议')
    assert not strategy(account)['suggestions']
    assert strategy(account)['advice_status'] == '行情更新失败，暂停建议'


def test_stale_signal_cannot_generate_orders(account, monkeypatch):
    update(account)
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    assert not strategy(account)['suggestions']
    assert '过期' in strategy(account)['advice_status']


def test_no_candidate_sells_old_position_but_missing_price_blocks(account, monkeypatch):
    event(account, 'buy', code='513100', name='旧仓', qty=500, price=2, fee=5)
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('510300', '候选', '股票', 2, 2, -1, False, 0)
    publish('2025-01-08', [row], {'510300': 2, '513100': 2}, {}, path=account)
    sell, = strategy(account)['suggestions']
    assert sell['side'] == 'sell' and sell['qty'] == 500
    publish('2025-01-08', [row], {'510300': 2}, {}, path=account)
    assert not strategy(account)['suggestions']


def test_same_day_shares_do_not_fund_new_purchase(account):
    event(account, 'buy', code='513100', name='旧仓', qty=500, price=2, fee=5)
    update(account)
    assert not strategy(account)['suggestions']
    assert '当日不可卖' in strategy(account)['advice_status']


def test_dismiss_and_new_target(account):
    update(account)
    buy, = strategy(account)['suggestions']
    wallet.dismiss(account, dict(revision=wallet.revision(wallet.load(account)), id=buy['id']))
    assert not strategy(account)['suggestions']
    update(account, code='513500')
    assert strategy(account)['suggestions'][0]['code'] == '513500'


def test_partial_buy_only_suggests_remaining_and_carries_to_next_day(account, monkeypatch):
    update(account)
    event(account, 'buy', code='510300', name='目标ETF', qty=400, price=2, fee=5)
    buy, = strategy(account)['suggestions']
    assert buy['qty'] == 500
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('510300', '目标ETF', '股票', 2, 2, 1, True, 1)
    publish('2025-01-08', [row], {'510300': 2}, {}, path=account)
    assert strategy(account)['suggestions'][0]['qty'] == 500
    event(account, 'buy', code='510300', name='目标ETF', qty=500, price=2, fee=5)
    assert not strategy(account)['suggestions']


def test_repeated_publication_without_fill_does_not_duplicate_or_change_suggestions(account):
    update(account)
    before = strategy(account)['suggestions']
    update(account)
    assert strategy(account)['suggestions'] == before
    assert not wallet.load(account)['events']


def test_entry_uses_same_snapshot_for_independent_fills_and_account_advice(account, monkeypatch, tmp_path):
    import etf_rotation as entry
    from src.trade_ledger import derive, load_trades
    ledger = tmp_path / 'observation.jsonl'
    row = RotationRow('510300', '目标', '股票', 2, 2, 1, True, 1)
    diag = dict(n=1, pool=15, eligible=1, realtime_missing=0, prev_trading_day='2025-01-06')
    calls = []
    def quotes(**kwargs):
        calls.append(kwargs)
        return [row], {'510300': 2}, diag
    monkeypatch.setattr(entry, 'rotation_snapshot', quotes)
    monkeypatch.setattr(entry, 'LEDGER_PATH', ledger)
    monkeypatch.setattr(wallet, 'ACCOUNT_PATH', account)
    before = wallet.account_data(account)
    report = entry.run(trade_date='2025-01-07')
    assert len(calls) == 1
    assert '账户建议' in report and '约 900 份' in report
    assert strategy(account)['suggestions'][0]['qty'] == 900
    assert wallet.account_data(account)['positions'] == before['positions']
    assert wallet.account_data(account)['cash'] == before['cash']
    sim = derive(load_trades(ledger), '2025-01-07')
    assert sim.positions[0]['count'] > 900
    old_state, old_sim = account.read_bytes(), ledger.read_bytes()
    entry.run(dry_run=True, trade_date='2025-01-07')
    assert account.read_bytes() == old_state and ledger.read_bytes() == old_sim


def test_entry_fetch_exception_invalidates_old_advice(account, monkeypatch):
    import etf_rotation as entry
    monkeypatch.setattr(wallet, 'ACCOUNT_PATH', account)
    update(account)
    def fail(**kwargs):
        raise RuntimeError('upstream unavailable')
    monkeypatch.setattr(entry, 'rotation_snapshot', fail)
    with pytest.raises(RuntimeError, match='upstream'):
        entry.run(trade_date='2025-01-07')
    assert not strategy(account)['suggestions']
    assert strategy(account)['advice_status'] == '行情更新失败，暂停建议'


def test_partial_sell_and_new_target_fill_do_not_sell_target_again(account, monkeypatch):
    event(account, 'buy', code='513100', name='旧仓', qty=500, price=2, fee=5)
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('510300', '目标', '股票', 2, 2, 1, True, 1)
    publish('2025-01-08', [row], {'510300': 2, '513100': 2}, {}, path=account)
    # 成交日期设为1月8日，部分卖旧、部分买新。
    def fill(side, **fields):
        wallet.record_event(account, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                                          date='2025-01-08', side=side, strategy='rotation', **fields))
    fill('sell', code='513100', name='旧仓', qty=200, price=2, fee=5)
    fill('buy', code='510300', name='目标', qty=400, price=2, fee=5)
    sell, buy = strategy(account)['suggestions']
    assert sell['code'] == '513100' and sell['qty'] == 300
    assert buy['code'] == '510300' and buy['qty'] == 500


def test_out_of_order_run_cannot_replace_new_signal(account):
    update(account)
    before = account.read_bytes()
    result = publish('2025-01-06', [], {}, {}, path=account)
    assert '未覆盖' in result and account.read_bytes() == before


def test_cash_allocated_after_signal_still_tracks_partial_buy(account):
    event(account, 'cash_assign', cash_allocations={'core': 3000, 'rotation': 0})
    update(account)
    assert not strategy(account)['suggestions']
    event(account, 'cash_assign', cash_allocations={'core': 1000, 'rotation': 2000})
    assert strategy(account)['suggestions'][0]['qty'] == 900
    event(account, 'buy', code='510300', name='目标ETF', qty=400, price=2, fee=5)
    assert strategy(account)['suggestions'][0]['qty'] == 500


def test_cash_increase_before_any_fill_replans_full_purchase(account):
    event(account, 'cash_assign', cash_allocations={'core': 2500, 'rotation': 500})
    update(account)
    assert strategy(account)['suggestions'][0]['qty'] == 200
    event(account, 'cash_assign', cash_allocations={'core': 1000, 'rotation': 2000})
    assert strategy(account)['suggestions'][0]['qty'] == 900


def test_fill_retry_cash_increase_and_signal_recovery_keep_original_remaining(account, monkeypatch):
    event(account, 'cash_assign', cash_allocations={'core': 2500, 'rotation': 500})
    update(account)
    assert strategy(account)['suggestions'][0]['qty'] == 200
    payload = dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                   date='2025-01-07', side='buy', strategy='rotation', code='510300',
                   qty=100, price=2, fee=5)
    wallet.record_event(account, payload)
    event(account, 'cash_assign', cash_allocations={'core': 795, 'rotation': 2000})
    before_retry = account.read_bytes()
    wallet.record_event(account, payload)
    assert account.read_bytes() == before_retry
    assert strategy(account)['suggestions'][0]['qty'] == 100

    update(account, issue='行情更新失败，暂停建议')
    assert not strategy(account)['suggestions']
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('510300', '目标', '股票', 2, 2, 1, True, 1)
    publish('2025-01-08', [row], {'510300': 2}, {}, path=account)
    assert strategy(account)['suggestions'][0]['qty'] == 100
    wallet.record_event(account, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                                     date='2025-01-08', side='buy', strategy='rotation', code='510300',
                                     qty=100, price=2, fee=5))
    assert not strategy(account)['suggestions']
    assert strategy(account)['positions'][0]['count'] == 200


def test_new_target_after_partial_buy_starts_its_own_remaining_plan(account, monkeypatch):
    update(account)
    event(account, 'buy', code='510300', qty=400, price=2, fee=5)
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('513500', '新目标', '股票', 2, 2, 1, True, 1)
    publish('2025-01-08', [row], {'510300': 2, '513500': 2}, {}, path=account)
    sell, buy = strategy(account)['suggestions']
    assert (sell['code'], sell['qty']) == ('510300', 400)
    assert (buy['code'], buy['qty']) == ('513500', 900)
    for side, code, qty in [('sell', '510300', 400), ('buy', '513500', 300)]:
        wallet.record_event(account, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                                         date='2025-01-08', side=side, strategy='rotation', code=code,
                                         qty=qty, price=2, fee=5))
    publish('2025-01-08', [row], {'510300': 2, '513500': 2}, {}, path=account)
    remaining, = strategy(account)['suggestions']
    assert (remaining['code'], remaining['qty']) == ('513500', 600)
    assert all(p['code'] != '510300' for p in strategy(account)['positions'])


def test_dismissed_switch_stays_hidden_until_target_changes(account, monkeypatch):
    event(account, 'buy', code='513100', qty=500, price=2, fee=5)
    monkeypatch.setattr(wallet, '_today', lambda: date(2025, 1, 8))
    row = RotationRow('510300', '目标', '股票', 2, 2, 1, True, 1)
    prices = {'510300': 2, '513100': 2}
    publish('2025-01-08', [row], prices, {}, path=account)
    sell, buy = strategy(account)['suggestions']
    wallet.dismiss(account, dict(revision=wallet.revision(wallet.load(account)), id=sell['id']))
    publish('2025-01-08', [row], prices, {}, path=account)
    assert not strategy(account)['suggestions']
    assert strategy(account)['advice_status'] == '本轮建议已忽略'

    row.code = '513500'
    publish('2025-01-08', [row], {'513500': 2, '513100': 2}, {}, path=account)
    sell, buy = strategy(account)['suggestions']
    assert sell['code'] == '513100' and buy['code'] == '513500'


def test_failed_buy_does_not_fix_an_unexecuted_plan(account):
    event(account, 'cash_assign', cash_allocations={'core': 2500, 'rotation': 500})
    update(account)
    before = account.read_bytes()
    with pytest.raises(ValueError, match='现金不足'):
        event(account, 'buy', code='510300', qty=1000, price=2, fee=5)
    assert account.read_bytes() == before
    event(account, 'cash_assign', cash_allocations={'core': 1000, 'rotation': 2000})
    assert strategy(account)['suggestions'][0]['qty'] == 900


def test_signal_publication_and_first_fill_do_not_lose_each_other(account):
    update(account)
    payload = dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(account)),
                   date='2025-01-07', side='buy', strategy='rotation', code='510300',
                   qty=400, price=2, fee=5)
    start = Barrier(2)

    def publish_again():
        start.wait(timeout=5)
        return update(account)

    def fill():
        start.wait(timeout=5)
        return wallet.record_event(account, payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        publication, trade = pool.submit(publish_again), pool.submit(fill)
        publication.result(timeout=10)
        trade.result(timeout=10)
    remaining, = strategy(account)['suggestions']
    assert remaining['qty'] == 500
    assert len(wallet.load(account)['events']) == 1
    assert strategy(account)['positions'][0]['count'] == 400
