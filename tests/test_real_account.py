import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from uuid import uuid4

import pytest

from src.real_account import account_data, collect_close, initialize, load, record_event, revision, dismiss


@pytest.fixture
def account(tmp_path, monkeypatch):
    from datetime import date
    monkeypatch.setattr('src.real_account._today', lambda: date(2025, 1, 7))
    path = tmp_path / 'data' / 'core_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2)],
               targets={'588000': .4})
    return path


def event(path, side, **fields):
    return dict(request_id=str(uuid4()), revision=revision(load(path)),
                date='2025-01-06', side=side) | fields


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
    assert len(data['accounts']) == 4
    assert data['accounts'][0]['id'] == 'core'
    assert data['strategies'][0]['kind'] == 'actual'
    assert data['accounts'][0]['equity'] == 3000
    assert all(row['kind'] == 'simulated' for row in data['accounts'][1:])


def test_legacy_core_writer_cannot_create_simulated_fills(account, monkeypatch):
    from src.trade_ledger.ledger import LedgerError, TradeRecord, append_trade
    monkeypatch.setattr('src.real_account.ACCOUNT_PATH', account)
    before = account.read_bytes()
    with pytest.raises(LedgerError, match='网页确认成交'):
        append_trade(TradeRecord('2025-01-06', '588000', 'ETF', 'buy', 100, 2))
    assert account.read_bytes() == before


def test_suggestions_do_not_trade_and_partial_fill_recalculates(account):
    view = account_data(account)
    assert view['suggestions'][0]['side'] == 'sell'
    assert view['suggestions'][0]['qty'] == 400
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=2, fee=1))
    view = account_data(account)
    assert view['positions'][0]['count'] == 900
    assert view['cash'] == 1199
    assert view['equity'] == 2999
    assert view['suggestions'][0]['qty'] == 300
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
    dismiss(account, dict(revision=view['revision'], id=view['suggestions'][0]['id']))
    assert not account_data(account)['suggestions']
    assert account_data(account)['positions'][0]['count'] == 1000
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=2, fee=0))
    assert account_data(account)['suggestions']


def test_new_trade_invalidates_same_date_close_until_recollection(account):
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    record_event(account, event(account, 'sell', code='588000', name='ETF', qty=100, price=3, fee=1))
    assert account_data(account)['history'][-1]['nav'] is None
    collect_close(account, '2025-01-06', lambda codes, day: {codes[0]: 3})
    assert account_data(account)['performance']['equity'] == 3999


def test_scheduled_report_recollects_changed_actual_history(account, monkeypatch):
    from datetime import date
    import portfolio_report as entry
    monkeypatch.setattr('src.real_account.ACCOUNT_PATH', account)
    monkeypatch.setattr('src.sim_accounts.all_accounts', lambda: [])
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
    assert '核心配置账户' in report


def test_core_cron_can_only_recommend_after_initialization(account, monkeypatch):
    from src.etf import allocation
    from src.etf.rebalancer import RebalancePlan
    monkeypatch.setattr('src.real_account.ACCOUNT_PATH', account)
    before = account.read_bytes()
    output = allocation._execute_batch({'plan': RebalancePlan([], 0, False, '', [])})
    assert '确认成交后更新' in output
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
    url = f'http://127.0.0.1:{server.server_port}/api/core/event'
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
        response = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers))
        assert response.status == 200
        assert account_data(account)['cash'] == 1100
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

def test_gap_amount_is_separate_from_rounded_trade_value(account):
    record_event(account, event(account, 'deposit', amount=10))
    item = account_data(account)['suggestions'][0]
    assert item['gap_amount'] == 796
    assert item['qty'] == 300
    assert item['amount'] == 600
    assert item['price_date'] == '2025-01-04'


def test_stale_or_incomplete_prices_hide_reference_shares(account, monkeypatch):
    from datetime import date
    monkeypatch.setattr('src.real_account._today', lambda: date(2025, 1, 20))
    item = account_data(account)['suggestions'][0]
    assert item['gap_amount'] == 800
    assert item['qty'] is None and item['amount'] is None
    assert '超过7天' in item['price_issue']
    monkeypatch.setattr('src.real_account._today', lambda: date(2025, 1, 7))
    collect_close(account, '2025-01-06', lambda *_: {})
    item = account_data(account)['suggestions'][0]
    assert item['qty'] is None
    assert '不完整' in item['price_issue']


@pytest.mark.parametrize('targets,expected', [
    ({'588000': .1, '510300': .9}, '需先卖出回款'),
    ({'510300': .9}, '现金不足'),
])
def test_buy_suggestions_do_not_treat_projected_sales_as_actual_cash(account, targets, expected):
    path = account.parent / 'funding.json'
    initialize(path, asof='2025-01-04', cash=0,
               positions=[dict(code='588000', name='ETF1', count=1000, price=2),
                          dict(code='510300', name='ETF2', count=100, price=2)], targets=targets)
    view = account_data(path)
    buy = next(r for r in view['suggestions'] if r['side'] == 'buy')
    assert expected in buy['cash_note']
    assert view['cash'] == 0
    assert not view['trades']
