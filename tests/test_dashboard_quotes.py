"""主动行情刷新只影响展示估值，保留台账、资金流及收盘曲线口径。"""

from datetime import datetime, timedelta
import base64
from http.server import ThreadingHTTPServer
import json
import threading
import time
import urllib.error
import urllib.request
from uuid import uuid4

import pytest

import dashboard
from src.dashboard_quotes import QuoteRefresh, SHANGHAI
from src.etf_account import account_data, collect_close, initialize, record_event


def basic(user, password):
    return 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()


@pytest.fixture
def account(tmp_path, monkeypatch):
    today = datetime.now(SHANGHAI).date()
    monkeypatch.setattr('src.etf_account._today', lambda: today)
    path = tmp_path / 'data' / 'etf_account.json'
    initialize(path, asof=(today - timedelta(days=3)).isoformat(), cash=1000,
               positions=[dict(code='588000', count=1000, price=2, strategy='core'),
                          dict(code='510300', count=100, price=3, strategy='rotation')],
               targets={'588000': .5}, budgets={'core': .8, 'rotation': .2},
               cash_allocations={'core': 1000})
    return path, today.isoformat()


def quotes_for(path, day, prices):
    return {'revision': account_data(path)['quote_revision'], 'day': day, 'running': False,
            'quotes': {code: {'price': price, 'fetched_at': day + 'T10:00:00+08:00'}
                       for code, price in prices.items()},
            'missing': ['510300'], 'fetched_at': day + 'T10:00:00+08:00', 'error': None}


def wait_finished(cache, path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = cache.read(path)
        if not state.get('running'):
            return state
        time.sleep(.01)
    pytest.fail('后台取价未结束')


def test_quotes_revalue_account_and_strategies_without_changing_close_or_signals(account):
    path, day = account
    yesterday = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
    collect_close(path, yesterday, lambda *_: {'588000': 2.1, '510300': 3.1})
    before = account_data(path)
    original = path.read_bytes()
    view = account_data(path, live_quotes=quotes_for(path, day, {'588000': 3}))
    assert view['equity'] == 4310
    assert view['cash'] == before['cash'] == 1000
    assert view['pnl'] == 1010
    positions = {row['code']: row for row in view['positions']}
    assert positions['588000']['market_value'] == 3000
    assert not positions['588000']['quote_missing']
    assert positions['510300']['close'] == 3.1
    assert positions['510300']['quote_missing']
    assert view['strategies'][0]['pnl'] == 1000
    assert view['strategies'][1]['pnl'] == 10
    for key in ('revision', 'history', 'performance', 'valuation_date'):
        assert view[key] == before[key]
    assert [s['suggestions'] for s in view['strategies']] == [s['suggestions'] for s in before['strategies']]
    assert path.read_bytes() == original


def test_deposit_is_not_profit_and_old_quotes_expire_on_account_change(account):
    path, day = account
    old_quotes = quotes_for(path, day, {'588000': 3, '510300': 3})
    record_event(path, dict(request_id=str(uuid4()), revision=account_data(path)['revision'],
                            date=day, side='deposit', strategy='core', amount=500))
    stale = account_data(path, live_quotes=old_quotes)
    assert not stale['quote_refresh']['applicable']
    assert stale['equity'] == 3800
    fresh = account_data(path, live_quotes=quotes_for(path, day, {'588000': 3, '510300': 3}))
    assert fresh['cash'] == 1500
    assert fresh['pnl'] == 1000
    assert fresh['strategies'][0]['pnl'] == 1000


def test_old_day_and_invalid_prices_never_override_saved_estimates(account):
    path, day = account
    saved = account_data(path)
    stale = quotes_for(path, '2000-01-01', {'588000': 99})
    view = account_data(path, live_quotes=stale)
    assert not view['quote_refresh']['applicable']
    assert view['equity'] == saved['equity']
    invalid = quotes_for(path, day, {'588000': float('nan'), '510300': -1})
    view = account_data(path, live_quotes=invalid)
    assert view['equity'] == saved['equity']
    assert not view['quote_refresh']['estimated']
    assert all(row['quote_missing'] for row in view['positions'])


def test_new_close_invalidates_earlier_intraday_quotes(account):
    path, day = account
    cached = quotes_for(path, day, {'588000': 3, '510300': 4})
    before = account_data(path)
    collect_close(path, day, lambda *_: {'588000': 3.5, '510300': 4.5})
    view = account_data(path, live_quotes=cached)
    assert view['revision'] == before['revision']
    assert view['quote_revision'] != before['quote_revision']
    assert not view['quote_refresh']['applicable']
    assert view['equity'] == 4950
    assert view['positions'][0]['close'] == 3.5


def test_background_refresh_coalesces_requests_and_preserves_failed_old_quotes(account, monkeypatch):
    path, day = account
    cache = QuoteRefresh()
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch(codes):
        calls.append(codes)
        started.set()
        assert release.wait(5)
        return {'588000': 3, '510300': 4}, {}

    monkeypatch.setattr('data_provider.manager.realtime_quotes', fetch)
    rev = account_data(path)['quote_revision']
    cache.start(path, rev, ['588000', '510300', '588000'], day)
    assert started.wait(5)
    assert cache.read(path)['running']
    cache.start(path, rev, ['588000'], day)
    release.set()
    first = wait_finished(cache, path)
    assert calls == [['510300', '588000']]
    assert not first['missing']
    cache.start(path, rev, ['588000', '510300'], day)
    assert len(calls) == 1  # 完成后的短期重复点击也合并。

    cache._last_start -= 16
    monkeypatch.setattr('data_provider.manager.realtime_quotes', lambda _: ({'588000': 3.5}, {}))
    cache.start(path, rev, ['588000', '510300'], day)
    second = wait_finished(cache, path)
    assert second['quotes']['588000']['price'] == 3.5
    assert second['quotes']['510300'] == first['quotes']['510300']
    assert second['missing'] == ['510300']
    assert cache.read(path.with_name('other.json')) == {}


def test_fetch_failure_finishes_job_and_keeps_saved_valuation(account, monkeypatch):
    path, day = account

    def fail(_):
        raise RuntimeError('offline')

    monkeypatch.setattr('data_provider.manager.realtime_quotes', fail)
    cache = QuoteRefresh()
    cache.start(path, account_data(path)['quote_revision'], ['588000', '510300'], day)
    state = wait_finished(cache, path)
    assert state['error']
    assert state['missing'] == ['510300', '588000']
    assert not state['quotes']
    assert account_data(path, live_quotes=state)['equity'] == account_data(path)['equity']


def test_refresh_api_is_protected_and_overview_delivers_estimates(account, monkeypatch):
    path, day = account
    monkeypatch.setattr(dashboard, 'ROOT', path.parent.parent)
    monkeypatch.setattr(dashboard, 'QUOTES', QuoteRefresh())
    monkeypatch.setattr(dashboard, 'AUTH_DELAY', 0)
    monkeypatch.setenv('TRADE_DASHBOARD_PASSWORD', 'secret')
    monkeypatch.setattr('data_provider.manager.realtime_quotes', lambda _: ({'588000': 3}, {}))
    original = path.read_bytes()
    server = ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    headers = {'Content-Type': 'application/json'}
    try:
        for expected, additions in ((401, {}), (403, {'Authorization': basic('trader', 'secret')})):
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(urllib.request.Request(base + '/api/etf/refresh', data=b'{}',
                                                              headers=headers | additions))
            assert denied.value.code == expected
        headers.update(Authorization=basic('trader', 'secret'), **{'X-Trade-Request': 'dashboard'})
        with urllib.request.urlopen(urllib.request.Request(base + '/api/etf/refresh', data=b'{}', headers=headers)) as response:
            assert response.status == 202
        wait_finished(dashboard.QUOTES, path)
        with urllib.request.urlopen(urllib.request.Request(base + '/api/overview', headers=headers)) as response:
            result = json.loads(response.read())
        assert result['accounts'][0]['equity'] == 4300
        assert result['accounts'][0]['quote_refresh']['missing'] == ['510300']
        assert result['strategies'][0]['invested'] == 3000
        assert path.read_bytes() == original
    finally:
        server.shutdown()
        server.server_close()
