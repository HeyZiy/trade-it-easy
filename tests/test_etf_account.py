import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from uuid import uuid4

import pytest

from src.etf_account import account_data, collect_close, initialize, load, record_event, revision, dismiss


@pytest.fixture
def account(tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 7))
    path = tmp_path / 'data' / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2, strategy='core')],
               targets={'588000': .4}, budgets={'core':1}, cash_allocations={'core':1000})
    return path


def event(path, side, **fields):
    return dict(request_id=str(uuid4()), revision=revision(load(path)),
                date='2025-01-06', side=side, strategy='core') | fields


def test_initial_snapshot_and_no_overwrite(account):
    view = account_data(account)
    assert view['equity'] == 3000
    assert view['cash'] == 1000
    assert view['performance']['nav'] == 1
    assert view['positions'][0]['count'] == 1000
    assert view['positions'][0]['cost_price'] is None
    before = account.read_bytes()
    with pytest.raises(ValueError, match='禁止覆盖'):
        initialize(account, asof='2025-01-04', cash=1000,
                   positions=[dict(code='588000', name='ETF', count=1000, price=2)])
    assert account.read_bytes() == before


def test_collection_gaps_missing_prices_and_never_backfills_before_initial(account):
    collect_close(account, '2025-01-03', lambda *_: pytest.fail('不得倒填期初前行情'))
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    view = account_data(account)
    assert view['performance']['equity'] == 4000
    assert view['performance']['nav'] == pytest.approx(4 / 3)
    collect_close(account, '2025-01-07', lambda *_: {})
    view = account_data(account)
    assert view['performance']['date'] == '2025-01-06'
    assert view['history'][-1]['nav'] is None
    assert view['positions'][0]['close'] == 3
    assert view['warnings']


def test_core_replaces_simulation_without_adding_account(account):
    from dashboard import dashboard_data
    data = dashboard_data(account.parent.parent)
    assert len(data['accounts']) == 1
    assert data['accounts'][0]['id'] == 'etf'
    assert data['strategies'][0]['account_id'] == 'etf'
    assert data['accounts'][0]['equity'] == 3000
    assert all('kind' not in row for row in data['accounts'])


def test_missing_initialization_never_uses_old_core_ledger(tmp_path, monkeypatch):
    from dashboard import dashboard_data
    from src.etf import allocation
    from src.trade_ledger.ledger import TradeRecord, append_trade
    path = tmp_path / 'data' / 'etf_account.json'
    monkeypatch.setattr('src.etf_account.ACCOUNT_PATH', path)
    old = path.with_name('trade_ledger.jsonl')
    append_trade(TradeRecord('2025-01-02', '510300', '旧持仓', 'buy', 100, 3), old)
    before = old.read_bytes()
    view = dashboard_data(tmp_path)['accounts'][0]
    assert view['error'] == 'ETF账户尚未初始化'
    assert not view['positions']
    with pytest.raises(FileNotFoundError, match='尚未初始化'):
        allocation._raw_snapshot()
    assert old.read_bytes() == before


def test_legacy_core_writer_cannot_create_simulated_fills(account, monkeypatch):
    from src.trade_ledger.ledger import LedgerError, TradeRecord, append_trade
    monkeypatch.setattr('src.etf_account.ACCOUNT_PATH', account)
    before = account.read_bytes()
    with pytest.raises(LedgerError, match='网页确认成交'):
        append_trade(TradeRecord('2025-01-06', '588000', 'ETF', 'buy', 100, 2))
    assert account.read_bytes() == before


def test_suggestions_do_not_trade_and_partial_fill_recalculates(account):
    view = account_data(account)
    assert view['strategies'][0]['suggestions'][0]['side'] == 'sell'
    assert view['strategies'][0]['suggestions'][0]['qty'] == 400
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=2, fee=1))
    view = account_data(account)
    assert view['positions'][0]['count'] == 900
    assert view['cash'] == 1199
    assert view['equity'] == 2999
    assert view['strategies'][0]['suggestions'][0]['qty'] == 300
    assert len(view['trades']) == 1


def test_buy_fee_dividend_and_cashflows_are_accounted(account):
    record_event(account, event(account, 'buy', code='588000', name='ETF', qty=100, price=2, fee=5))
    record_event(account, event(account, 'dividend', amount=10))
    record_event(account, event(account, 'deposit', amount=500))
    record_event(account, event(account, 'withdraw', amount=100))
    view = account_data(account)
    assert view['positions'][0]['count'] == 1100
    assert view['cash'] == 1205
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 2})
    assert account_data(account)['performance']['nav'] == pytest.approx(3005 / 3000)


def test_deposit_after_price_gain_preserves_nav(account):
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    record_event(account, event(account, 'deposit', amount=1000, date='2025-01-07'))
    collect_close(account, '2025-01-07', lambda codes, day: {codes[0]: 3})
    assert account_data(account)['performance']['nav'] == pytest.approx(4 / 3)


def test_retries_are_idempotent_and_conflicting_retry_is_rejected(account):
    payload = event(account, 'buy', code='588000', name='ETF', qty=100, price=2, fee=1)
    record_event(account, payload)
    record_event(account, payload)
    assert len(load(account)['events']) == 1
    with pytest.raises(ValueError, match='不一致'):
        record_event(account, {**payload, 'qty': 200})
    assert account_data(account)['cash'] == 799


@pytest.mark.parametrize('fields', [
    dict(side='buy', code='588000', name='ETF', qty=1000, price=2, fee=0),
    dict(side='sell', code='588000', name='ETF', qty=1100, price=2, fee=0),
    dict(side='buy', code='588000', name='ETF', qty=1, price=2, fee=0),
    dict(side='buy', code='588000', name='ETF', qty=100, price=float('nan'), fee=0),
    dict(side='withdraw', amount=1001),
    dict(side='deposit', amount=-1),
])
def test_invalid_event_never_changes_account(account, fields):
    before = account.read_bytes()
    with pytest.raises(ValueError):
        record_event(account, event(account, **fields))
    assert account.read_bytes() == before


def test_old_page_cannot_overwrite_new_account(account):
    old = event(account, 'deposit', amount=100)
    record_event(account, event(account, 'deposit', amount=200))
    with pytest.raises(ValueError, match='账户已更新'):
        record_event(account, old)
    assert account_data(account)['cash'] == 1200


def test_dismiss_changes_no_holdings_and_new_fill_creates_fresh_suggestion(account):
    view = account_data(account)
    dismiss(account, dict(revision=view['revision'], id=view['strategies'][0]['suggestions'][0]['id']))
    assert not account_data(account)['strategies'][0]['suggestions']
    assert account_data(account)['positions'][0]['count'] == 1000
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=2, fee=0))
    assert account_data(account)['strategies'][0]['suggestions']


def test_new_trade_invalidates_same_date_close_until_recollection(account):
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=3, fee=1))
    assert account_data(account)['history'][-1]['nav'] is None
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    assert account_data(account)['performance']['equity'] == 3999


def test_scheduled_report_recollects_changed_actual_history(account, monkeypatch):
    from datetime import date
    import portfolio_report as entry
    monkeypatch.setattr('src.etf_account.ACCOUNT_PATH', account)
    from types import SimpleNamespace
    monkeypatch.setattr('src.account_sources.all_sources', lambda: [SimpleNamespace(ledger_path=str(account), label='核心配置', benchmark={})])
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    monkeypatch.setattr(entry, 'closed_date', lambda now: days[-1])
    monkeypatch.setattr(entry, 'get_trading_dates', lambda start, end: [d for d in days if start <= d <= end])
    monkeypatch.setattr(entry.ClosingPrices, 'close_prices', lambda self, codes, day: {c: 3 for c in codes})
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=3, fee=1))
    report, errors = entry.run(backfill=True)
    assert errors == 0
    rows = load(account)['records']
    assert next(r for r in rows if r['date'] == '2025-01-06')['equity'] == 3999
    assert rows[-1]['equity'] == 3999
    assert 'ETF账户' in report


def test_core_cron_can_only_recommend_after_initialization(account, monkeypatch):
    from src.etf import allocation
    from src.etf.rebalancer import RebalancePlan
    monkeypatch.setattr('src.etf_account.ACCOUNT_PATH', account)
    before = account.read_bytes()
    output = allocation._recommendation_section({'plan': RebalancePlan([], 0, False, '', [])})
    assert '无调仓需求' in output
    assert account.read_bytes() == before


def test_write_api_requires_auth_and_custom_header(account, monkeypatch):
    import base64
    import dashboard
    monkeypatch.setattr(dashboard, 'ROOT', account.parent.parent)
    monkeypatch.setattr(dashboard, 'AUTH_DELAY', 0)
    monkeypatch.setenv('TRADE_DASHBOARD_PASSWORD', 'test-password')
    server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/api/etf/event'
    body = json.dumps(event(account, 'deposit', amount=100)).encode()
    headers = {'Content-Type': 'application/json'}
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers))
        assert denied.value.code == 401
        headers['Authorization'] = 'Basic ' + base64.b64encode(b'trader:test-password').decode()
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers))
        assert denied.value.code == 403
        headers['X-Trade-Request'] = 'dashboard'
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers)) as response:
            assert response.status == 200
        assert account_data(account)['cash'] == 1100
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_gap_amount_is_separate_from_rounded_trade_value(account):
    record_event(account, event(account, 'deposit', amount=10))
    item = account_data(account)['strategies'][0]['suggestions'][0]
    assert item['gap_amount'] == 796
    assert item['qty'] == 300
    assert item['amount'] == 600
    assert item['price_date'] == '2025-01-04'


def test_stale_or_incomplete_prices_hide_reference_shares(account, monkeypatch):
    from datetime import date
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 20))
    item = account_data(account)['strategies'][0]['suggestions'][0]
    assert item['gap_amount'] == 800
    assert item['qty'] is None and item['amount'] is None
    assert '超过7天' in item['price_issue']
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 7))
    collect_close(account, '2025-01-06', lambda *_: {})
    item = account_data(account)['strategies'][0]['suggestions'][0]
    assert item['qty'] is None
    assert '不完整' in item['price_issue']


@pytest.mark.parametrize('targets,expected', [
    ({'588000': .1, '510300': .9}, '需先卖出回款'),
    ({'510300': .9}, '现金不足'),
])
def test_buy_suggestions_do_not_treat_projected_sales_as_actual_cash(account, targets, expected):
    path = account.parent / 'funding.json'
    initialize(path, asof='2025-01-04', cash=0,
               positions=[dict(code='588000', name='ETF1', count=1000, price=2, strategy='core'),
                          dict(code='510300', name='ETF2', count=100, price=2, strategy='core')], targets=targets, budgets={'core':1})
    view = account_data(path)
    buy = next(r for r in view['strategies'][0]['suggestions'] if r['side'] == 'buy')
    assert expected in buy['cash_note']
    assert view['cash'] == 0
    assert not view['trades']

def test_unknown_budgets_and_positions_are_not_assigned_automatically(tmp_path):
    path = tmp_path / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2)], targets={'588000': .4})
    view = account_data(path)
    assert view['equity'] == 3000 and view['cash'] == 1000
    assert view['strategy_positions'][0]['strategy'] == 'unassigned'
    assert all(s['budget'] is None and s['buying_power'] is None and not s['positions'] and not s['suggestions']
               for s in view['strategies'])


def test_budgets_share_one_cash_pool_and_same_etf_is_aggregated(account):
    from src.etf_account import set_budgets
    set_budgets(account, dict(revision=revision(load(account)), budgets={'core': .8, 'rotation': .2}))
    view = account_data(account)
    assert [s['buying_power'] for s in view['strategies']] == [400, 0]
    record_event(account, event(account, 'cash_assign', cash_allocations={'core':400,'rotation':600}))
    view = account_data(account)
    assert [s['buying_power'] for s in view['strategies']] == [400, 600]
    assert sum(s['buying_power'] for s in view['strategies']) == view['cash']
    record_event(account, event(account, 'buy', code='588000', name='ETF', qty=100, price=2, fee=1))
    record_event(account, event(account, 'buy', strategy='rotation', code='588000', name='ETF', qty=100, price=2, fee=0))
    view = account_data(account)
    assert view['cash'] == 599
    assert view['equity'] == 2999
    assert len(view['positions']) == 1
    assert view['positions'][0]['count'] == 1200
    assert [s['positions'][0]['count'] for s in view['strategies']] == [1100, 100]
    assert sum(s['buying_power'] for s in view['strategies']) <= view['cash']


def test_assignment_changes_only_strategy_shares_not_cash_assets_or_nav(account):
    before = account_data(account)
    record_event(account, event(account, 'assign', from_strategy='core', strategy='rotation', code='588000', qty=100))
    after = account_data(account)
    assert after['cash'] == before['cash']
    assert after['equity'] == before['equity']
    assert after['positions'][0]['count'] == before['positions'][0]['count']
    assert after['history'] == before['history']
    assert [s['positions'][0]['count'] for s in after['strategies']] == [900, 100]
    preserved = account.read_bytes()
    with pytest.raises(ValueError, match='超过实际持仓'):
        record_event(account, event(account, 'sell', strategy='rotation', code='588000', name='ETF', qty=200, price=2, fee=0))
    assert account.read_bytes() == preserved


def test_budget_change_does_not_move_cash_positions_or_change_nav(account):
    from src.etf_account import set_budgets
    before = account_data(account)
    set_budgets(account, dict(revision=before['revision'], budgets={'core': .7, 'rotation': .3}))
    after = account_data(account)
    assert after['positions'] == before['positions']
    assert after['cash'] == before['cash']
    assert after['history'] == before['history']
    saved = account.read_bytes()
    with pytest.raises(ValueError, match='不能超过100%'):
        set_budgets(account, dict(revision=after['revision'], budgets={'core': .8, 'rotation': .3}))
    assert account.read_bytes() == saved


def test_observation_holdings_never_enter_etf_account_or_funding(account):
    from dashboard import dashboard_data
    from src.trade_ledger.ledger import TradeRecord, append_trade
    append_trade(TradeRecord('2025-01-02', '510300', '观察ETF', 'buy', 100000, 2, account='etf_rotation'),
                 account.with_name('trade_ledger_etf_rotation.jsonl'))
    view = dashboard_data(account.parent.parent)
    wallet = view['accounts'][0]
    rotation = next(s for s in view['strategies'] if s['id'] == 'rotation')
    assert len(view['accounts']) == 1 and wallet['id'] == 'etf'
    assert wallet['equity'] == 3000 and wallet['cash'] == 1000
    assert rotation['budget'] is None and not rotation['positions']
    assert rotation['observation']['positions'][0]['count'] == 100000
    assert {s['id'] for s in view['observations']} == {'satellite', 'quality'}

def test_cli_observation_routes_never_use_etf_account_file():
    from types import SimpleNamespace
    from book import _ledger_path
    from src.quality_pool.config import ACCOUNT_SOURCE as quality
    from src.etf.rotation import ACCOUNT_SOURCE as rotation
    assert str(_ledger_path(SimpleNamespace(path=None, account='quality_pool'))) == quality.ledger_path
    assert str(_ledger_path(SimpleNamespace(path=None, account='etf_rotation'))) == rotation.ledger_path

def test_split_odd_shares_does_not_create_rounding_profit(tmp_path):
    path = tmp_path / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=0,
               positions=[dict(code='588000', name='ETF', count=2, price=2.657, strategy='core')])
    before = account_data(path)
    record_event(path, event(path, 'assign', code='588000', qty=1, from_strategy='core', strategy='rotation'))
    after = account_data(path)
    assert after['equity'] == before['equity']

def test_strategy_pnl_keeps_past_gains_at_source_when_shares_move(account):
    collect_close(account, '2025-01-06', lambda codes, day: {c: 3 for c in codes})
    before = account_data(account)
    assert before['strategies'][0]['pnl'] == 1000
    record_event(account, event(account, 'assign', from_strategy='core', strategy='rotation', code='588000', qty=100))
    after = account_data(account)
    assert [s['pnl'] for s in after['strategies']] == [1000, 0]
    assert after['pnl'] == before['pnl'] == 1000
    collect_close(account, '2025-01-07', lambda codes, day: {c: 4 for c in codes})
    after = account_data(account)
    assert [s['pnl'] for s in after['strategies']] == [1900, 100]
    assert after['pnl'] == 2000


def test_strategy_pnl_accounts_for_trades_fees_dividends_and_ignores_funding(account):
    record_event(account, event(account, 'buy', code='588000', name='ETF', qty=100, price=2, fee=5))
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=200, price=3, fee=2))
    record_event(account, event(account, 'dividend', amount=10))
    record_event(account, event(account, 'cash_assign', cash_allocations={'core':1102,'rotation':301}))
    record_event(account, event(account, 'buy', strategy='rotation', code='588000', name='ETF', qty=100, price=3, fee=1))
    record_event(account, event(account, 'deposit', amount=500))
    record_event(account, event(account, 'withdraw', amount=100))
    view = account_data(account)
    assert [s['pnl'] for s in view['strategies']] == [1103, -1]
    assert view['pnl'] == 1102
    assert sum(s['pnl'] for s in view['strategies']) == view['pnl']
    assert view['cash'] == 1502
    record_event(account, event(account, 'dividend', strategy='unassigned', amount=7))
    view = account_data(account)
    assert [s['pnl'] for s in view['strategies']] == [1103, -1]
    assert view['strategy_pnl']['unassigned'] == 7
    assert view['pnl'] == 1109


def test_strategy_pnl_keeps_realized_profit_after_full_exit(account):
    record_event(account, event(account, 'cash_assign', cash_allocations={'core':799,'rotation':201}))
    record_event(account, event(account, 'buy', strategy='rotation', code='588000', name='ETF', qty=100, price=2, fee=1))
    record_event(account, event(account, 'sell', strategy='rotation', code='588000', name='ETF', qty=100, price=3, fee=2))
    view = account_data(account)
    rotation = view['strategies'][1]
    assert not rotation['positions']
    assert rotation['pnl'] == 97
    assert sum(s['pnl'] for s in view['strategies']) == view['pnl']

def test_unassigned_holding_stays_in_account_but_outside_core_strategy(tmp_path):
    path = tmp_path / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='510300', name='核心ETF', count=1000, price=2, strategy='core'),
                          dict(code='588000', name='未分配ETF', count=100, price=3, strategy='unassigned')],
               targets={'510300': .4}, budgets={'core': 1})
    collect_close(path, '2025-01-06', lambda codes, day: {'510300': 2, '588000': 4})
    view = account_data(path)
    core = view['strategies'][0]
    assert view['equity'] == 3400 and view['cash'] == 1000
    assert len(view['positions']) == 2
    assert [p['code'] for p in core['positions']] == ['510300']
    assert core['invested'] == 2000 and core['pnl'] == 0
    assert view['strategy_pnl']['unassigned'] == view['pnl'] == 100
    assert all(r['code'] != '588000' for r in core['suggestions'])

def test_cash_ownership_is_separate_from_targets_and_allocation_preserves_nav(account):
    from src.etf_account import set_budgets
    set_budgets(account, dict(revision=revision(load(account)), budgets={'core': .8, 'rotation': .2}))
    before = account_data(account)
    assert [s['actual_pct'] for s in before['strategies']] == [1, 0]
    payload = event(account, 'cash_assign', cash_allocations={'core':400,'rotation':600})
    record_event(account, payload)
    record_event(account, payload)
    after = account_data(account)
    assert after['cash_by_strategy'] == {'core':400,'rotation':600,'unassigned':0}
    assert [s['actual_pct'] for s in after['strategies']] == [.8, .2]
    assert after['equity'] == before['equity'] and after['pnl'] == before['pnl']
    assert after['history'] == before['history']
    saved = account.read_bytes()
    with pytest.raises(ValueError, match='分配金额超过'):
        record_event(account, event(account, 'cash_assign', cash_allocations={'core':600,'rotation':500}))
    assert account.read_bytes() == saved


def test_strategy_cannot_spend_another_strategy_cash(account):
    record_event(account, event(account, 'cash_assign', cash_allocations={'core':400,'rotation':600}))
    saved = account.read_bytes()
    with pytest.raises(ValueError, match='该策略现金不足'):
        record_event(account, event(account, 'buy', code='588000', name='ETF', qty=300, price=2, fee=0))
    assert account.read_bytes() == saved
    record_event(account, event(account, 'buy', code='588000', name='ETF', qty=100, price=2, fee=1))
    after = account_data(account)
    assert after['cash_by_strategy'] == {'core':199,'rotation':600,'unassigned':0}
    assert after['cash'] == 799


def test_internal_weights_scale_with_target_strategy_ratio(account):
    from src.etf_account import set_budgets
    set_budgets(account, dict(revision=revision(load(account)), budgets={'core': .5}))
    strategy = account_data(account)['strategies'][0]
    assert sum(strategy['target_weights'].values()) == pytest.approx(1)
    assert strategy['budget'] == 1500
    assert strategy['assets'] == 3000 and strategy['actual_pct'] == 1
    item = strategy['suggestions'][0]
    assert item['target_value'] == 600
    assert item['current_value'] == 2000
    assert item['gap_amount'] == 1400 and item['qty'] == 700


def test_external_flows_update_selected_cash_ownership_without_profit(account):
    record_event(account, event(account, 'deposit', strategy='rotation', amount=50))
    record_event(account, event(account, 'withdraw', strategy='core', amount=1000))
    after = account_data(account)
    assert after['cash_by_strategy'] == {'core':0,'rotation':50,'unassigned':0}
    assert after['cash'] == 50 and after['pnl'] == 0
    assert [s['pnl'] for s in after['strategies']] == [0,0]
