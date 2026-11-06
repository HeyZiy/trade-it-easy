"""现金便捷划转、历史更正及收益口径的回归账例。"""
from datetime import date
from uuid import uuid4

import pytest

from src import etf_account as account


@pytest.fixture
def path(tmp_path, monkeypatch):
    monkeypatch.setattr(account, "_today", lambda: date(2025, 1, 7))
    path = tmp_path / 'etf_account.json'
    account.initialize(path, asof='2025-01-02', cash=1000,
                       positions=[dict(code='588000', count=1000, price=2, strategy='core')],
                       cash_allocations={'core': 1000}, budgets={'core': 1}, targets={'588000': 1})
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 2})
    return path


def payload(path, side, **fields):
    return {'request_id': str(uuid4()), 'revision': account.revision(account.load(path)),
            'date': '2025-01-06', 'side': side, 'strategy': 'core', **fields}


def test_same_day_deposit_keeps_known_prices_and_profit(path):
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 3})
    before = account.account_data(path)
    account.record_event(path, payload(path, 'deposit', amount=100))
    after = account.account_data(path)
    assert after['equity'] == before['equity'] + 100
    assert after['pnl'] == before['pnl'] == 1000
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 3})
    assert account.account_data(path)['performance']['nav'] == pytest.approx(4 / 3)


def test_same_day_fee_change_does_not_erase_other_holdings_prices(path):
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 3})
    account.record_event(path, payload(path, 'dividend', amount=10))
    assert account.account_data(path)['pnl'] == 1010


def test_trade_price_does_not_revalue_old_shares_on_page(path):
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 3})
    account.record_event(path, payload(path, 'buy', qty=100, price=4, fee=1, code='588000'))
    view = account.account_data(path)
    assert view['positions'][0]['close'] == 3
    assert view['equity'] == 3899
    assert view['pnl'] == 899
    assert view['valuation']['date'] == '2025-01-06'


def test_new_code_without_same_day_price_is_pending_not_valued_at_trade(path):
    account.record_event(path, payload(path, 'buy', qty=100, price=3, fee=1, code='510300'))
    view = account.account_data(path)
    assert view['equity'] is None and view['pnl'] is None
    assert view['valuation']['missing'] == ['510300']
    assert next(r for r in view['positions'] if r['code'] == '510300')['close'] is None
    assert view['cash'] == 699
    assert '待估值' in account.render_summary(path)


def test_cash_transfer_is_internal_and_hits_both_strategy_histories(path):
    before = account.account_data(path)
    event = account.record_event(path, payload(path, 'cash_transfer', from_strategy='core',
                                              amount=300, **{'strategy': 'rotation'}))
    view = account.account_data(path)
    assert view['cash_by_strategy']['core'] == 700
    assert view['cash_by_strategy']['rotation'] == 300
    assert view['equity'] == before['equity'] and view['pnl'] == before['pnl']
    assert view['history'] == before['history']
    assert all(any(r['id'] == event['id'] for r in s['trades']) for s in view['strategies'][:2])


def edit_payload(path, event, **fields):
    values = {k: v for k, v in event.items() if k not in {'request', 'id', 'transfer_price', 'valuation_prices'}}
    return {**values, 'event_id': event['id'], 'request_id': str(uuid4()),
            'revision': account.revision(account.load(path)), **fields}


def test_edit_replays_history_keeps_audit_and_is_idempotent(path):
    original = account.record_event(path, payload(path, 'sell', code='588000', qty=100, price=3, fee=1))
    account.record_event(path, payload(path, 'dividend', amount=10))
    request = edit_payload(path, original, price=4, fee=2)
    result = account.edit_event(path, request)
    account.edit_event(path, request)
    state = account.load(path)
    assert result['id'] == original['id'] and result['price'] == 4
    assert len(state['events']) == 2 and len(state['event_edits']) == 1
    assert state['event_edits'][0]['before']['price'] == 3
    assert account.account_data(path)['cash'] == 1408


def test_edit_rejects_later_insufficient_cash_without_writing(path):
    funding = account.record_event(path, payload(path, 'deposit', amount=1000))
    account.record_event(path, payload(path, 'buy', code='588000', qty=500, price=3, fee=1))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='第2笔'):
        account.edit_event(path, edit_payload(path, funding, amount=1))
    assert path.read_bytes() == before


def test_edit_rejects_later_overselling_and_stale_revision(path):
    first = account.record_event(path, payload(path, 'sell', code='588000', qty=500, price=3, fee=1))
    old_request = edit_payload(path, first, qty=600)
    account.record_event(path, payload(path, 'sell', code='588000', qty=500, price=3, fee=1))
    with pytest.raises(ValueError, match='账户已更新'):
        account.edit_event(path, old_request)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='第2笔'):
        account.edit_event(path, edit_payload(path, first, qty=600))
    assert path.read_bytes() == before


def test_edit_invalidates_nav_and_old_plans_but_preserves_close_price(path):
    first = account.record_event(path, payload(path, 'sell', code='588000', qty=100, price=3, fee=1))
    account.collect_close(path, '2025-01-06', lambda *_: {'588000': 3})
    state = account.load(path)
    state['rotation_signal'] = {'date': '2025-01-06'}
    account._save(path, state)
    account.edit_event(path, edit_payload(path, first, fee=2))
    view = account.account_data(path)
    assert view['history'][-1]['nav'] is None
    assert view['positions'][0]['close'] == 3
    assert view['valuation']['date'] == '2025-01-06'
    assert '更正' in account.load(path)['rotation_signal']['issues']


def test_edit_context_uses_balance_before_original_sale(path):
    first = account.record_event(path, payload(path, 'sell', code='588000', qty=1000, price=3, fee=1))
    context = account.event_context(path, {'event_id': first['id'], 'revision': account.revision(account.load(path))})
    assert context['strategy_positions'][0]['count'] == 1000
    assert context['cash'] == 1000


def test_all_history_can_be_edited_beyond_twenty_rows(path):
    for _ in range(22):
        account.record_event(path, payload(path, 'deposit', amount=1))
    assert len(account.account_data(path)['trades']) == 22


def test_cash_transfer_rejects_overdraft_and_same_strategy(path):
    before = path.read_bytes()
    for fields in ({'amount': 1001, 'strategy': 'quality'}, {'amount': 1, 'strategy': 'core'}):
        with pytest.raises(ValueError):
            account.record_event(path, payload(path, 'cash_transfer', from_strategy='core', **fields))
        assert path.read_bytes() == before


def test_edit_cash_transfer_and_allocation(path):
    first = account.record_event(path, payload(path, 'cash_transfer', from_strategy='core', strategy='quality', amount=200))
    account.edit_event(path, edit_payload(path, first, amount=300))
    assert account.account_data(path)['cash_by_strategy']['quality'] == 300
    allocation = account.record_event(path, payload(path, 'cash_assign', cash_allocations={'core': 500, 'quality': 500}))
    account.edit_event(path, edit_payload(path, allocation, cash_allocations={'core': 400, 'rotation': 100, 'quality': 500}))
    assert account.account_data(path)['cash_by_strategy']['rotation'] == 100


def test_edit_preserves_future_transfer_valuation_and_rejects_conflicting_retry(path):
    first = account.record_event(path, payload(path, 'deposit', amount=100))
    transfer = account.record_event(path, payload(path, 'assign', from_strategy='core', strategy='rotation',
                                                 code='588000', qty=100))
    request = edit_payload(path, first, amount=200)
    account.edit_event(path, request)
    assert account.load(path)['events'][1] == transfer
    with pytest.raises(ValueError, match='内容不一致'):
        account.edit_event(path, {**request, 'amount': 300})


def test_edit_replays_same_day_close_and_next_day_availability(path):
    purchase = account.record_event(path, payload(path, "buy", code="510300", qty=100, price=3, fee=0))
    transfer = account.record_event(path, payload(path, "assign", from_strategy="core",
                                                  strategy="rotation", code="510300", qty=100))
    assert transfer["transfer_price"] == 3
    account.collect_close(path, "2025-01-06", lambda *_: {"588000": 2, "510300": 4})
    sale = account.record_event(path, payload(path, "sell", date="2025-01-07", strategy="rotation",
                                              code="510300", qty=100, price=5, fee=0))
    account.edit_event(path, edit_payload(path, purchase, price=3.5))

    state = account.load(path)
    assert state["events"][1:] == [transfer, sale]
    assert state["events"][0]["id"] == purchase["id"]

    def position_before(event):
        view = account.event_context(path, {"event_id": event["id"], "revision": account.revision(state)})
        return next(row for row in view["strategy_positions"] if row["code"] == "510300")

    before_transfer = position_before(transfer)
    before_sale = position_before(sale)
    assert (before_transfer["avail_count"], before_transfer["close"]) == (0, 4)
    assert (before_sale["avail_count"], before_sale["close"]) == (100, 4)
    after = account.snapshot(state, "2025-01-07")
    assert after["cash_by_strategy"] == {"core": 650, "rotation": 500, "quality": 0, "unassigned": 0}
    assert after["strategy_pnl"] == {"core": -50, "rotation": 200, "quality": 0, "unassigned": 0}


def test_edit_cannot_cross_neighbor_dates(path):
    first = account.record_event(path, payload(path, 'deposit', amount=100))
    account.record_event(path, payload(path, 'dividend', amount=1, date='2025-01-07'))
    with pytest.raises(ValueError, match='相邻流水'):
        account.edit_event(path, edit_payload(path, first, date='2025-01-08'))


def test_correction_api_context_and_edit_are_authenticated_and_idempotent(path, monkeypatch):
    import base64
    import json
    from http.server import ThreadingHTTPServer
    import threading
    import urllib.error
    import urllib.request
    import dashboard

    original = account.record_event(path, payload(path, 'deposit', amount=100))
    # Handler 的固定目录使用一份隔离副本。
    monkeypatch.setattr(dashboard, 'ROOT', path.parent)
    data = path.parent / 'data'
    data.mkdir(exist_ok=True)
    path.rename(data / account.FILENAME)
    from src import portfolio_config
    portfolio_config.path_for(path).rename(data / 'etf_config.json')
    api_path = data / account.FILENAME
    monkeypatch.setenv('TRADE_DASHBOARD_PASSWORD', 'test-password')
    monkeypatch.setattr(dashboard, 'AUTH_DELAY', 0)
    server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}'
    headers = {'Content-Type': 'application/json', 'X-Trade-Request': 'dashboard',
               'Authorization': 'Basic ' + base64.b64encode(b'trader:test-password').decode()}

    def post(route, value, request_headers=headers):
        request = urllib.request.Request(url + route, data=json.dumps(value).encode(), headers=request_headers)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            post('/api/etf/event/context', {}, {'Content-Type': 'application/json'})
        assert denied.value.code == 401
        context = post('/api/etf/event/context', {'event_id': original['id'], 'revision': account.revision(account.load(api_path))})
        assert context['cash'] == 1000
        plan = post('/api/etf/cash/rebalance', {'revision': account.revision(account.load(api_path))})
        assert plan['cash_allocations']['core'] == 1100
        change = edit_payload(api_path, original, amount=200)
        assert post('/api/etf/event/edit', change) == {'ok': True}
        assert post('/api/etf/event/edit', change) == {'ok': True}
        assert account.account_data(api_path)['cash'] == 1200
    finally:
        server.shutdown()
        server.server_close()


def rebalance(path, budgets):
    account.set_budgets(path, {'revision': account.revision(account.load(path)), 'budgets': budgets})
    before = path.read_bytes()
    plan = account.cash_rebalance(path, {'revision': account.revision(account.load(path))})
    assert path.read_bytes() == before
    return plan


def test_cash_rebalance_fills_all_strategy_gaps_and_changes_no_holdings(path):
    plan = rebalance(path, {'core': .8, 'rotation': .1, 'quality': .1})
    assert plan['cash_allocations'] == {'core': 400, 'rotation': 300, 'quality': 300}
    assert plan['shortfall'] == 0
    assert all(row['deviation'] == 0 for row in plan['rows'])
    before = account.account_data(path)
    account.record_event(path, payload(path, 'cash_assign', cash_allocations=plan['cash_allocations']))
    after = account.account_data(path)
    assert after['positions'] == before['positions']
    assert after['equity'] == before['equity'] and after['pnl'] == before['pnl']
    assert all(s['deviation'] == 0 for s in after['strategies'])


def test_cash_rebalance_cannot_sell_overweight_holdings_and_distributes_shortfall(path):
    plan = rebalance(path, {'core': .5, 'rotation': .3, 'quality': .2})
    assert plan['cash_allocations'] == {'core': 0, 'rotation': 600, 'quality': 400}
    assert plan['shortfall'] == 500
    assert plan['rows'][0]['holding_overweight']
    assert [r['deviation'] for r in plan['rows'][:3]] == [500, -300, -200]


def test_cash_rebalance_allocates_to_cents_without_losing_money(path):
    plan = rebalance(path, {'core': .5, 'rotation': 1 / 3, 'quality': 1 / 6})
    assert plan['cash_allocations'] == {'core': 0, 'rotation': 666.67, 'quality': 333.33}
    assert sum(round(r['after_cash'] * 100) for r in plan['rows']) == 100000


def test_cash_rebalance_preserves_cash_of_unconfigured_strategy(path):
    account.record_event(path, payload(path, 'cash_assign', cash_allocations={'core': 800, 'quality': 200}))
    plan = rebalance(path, {'core': .8, 'rotation': .1})
    assert plan['cash_allocations'] == {'core': 400, 'rotation': 300, 'quality': 200}
    assert plan['rows'][-1]['after_cash'] == 100


def test_cash_rebalance_requires_full_valuation_and_current_revision(path):
    stale = account.revision(account.load(path))
    account.record_event(path, payload(path, 'buy', code='510300', qty=100, price=3, fee=0))
    with pytest.raises(ValueError, match='账户已更新'):
        account.cash_rebalance(path, {'revision': stale})
    with pytest.raises(ValueError, match='待估值'):
        account.cash_rebalance(path, {'revision': account.revision(account.load(path))})
