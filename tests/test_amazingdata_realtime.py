"""AD 近期快照、报价映射与统一路由回退；SDK 边界替换，不联网。"""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider.fetchers import amazingdata_fetcher
from data_provider.fetchers.amazingdata import market
from data_provider.manager import DataFetcherManager, _FALLBACK_REALTIME_PRIORITY, realtime_quotes
from data_provider.types import UnifiedRealtimeQuote
from src.config import Config, DEFAULT_REALTIME_PRIORITY


@pytest.fixture
def snapshot(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 5, 10, 0, tzinfo=market._SHANGHAI)

    monkeypatch.setattr(market, 'datetime', Clock)
    frame = pd.DataFrame({
        'trade_time': pd.to_datetime(['2026-11-05 09:59:59', '2026-11-05 09:58:00']),
        'last': [3.3, 3.1], 'close': [0., 0.], 'pre_close': [3., 3.],
        'open': [3.1, 3.1], 'high': [3.4, 3.2], 'low': [2.9, 2.9],
        'volume': [10000., 9000.], 'amount': [33000., 27900.],
    })
    state = SimpleNamespace(frame=frame, calls=[], failure=None, shape='nested')

    def query(codes, **kwargs):
        state.calls.append((codes, kwargs))
        if state.failure is not None:
            raise state.failure
        if state.shape == 'table':
            return state.frame.assign(MARKET_CODE=codes[0])
        result = {codes[0]: state.frame}
        return {20261105: result} if state.shape == 'nested' else result

    monkeypatch.setattr(market.client, 'get_market_data', lambda: SimpleNamespace(query_snapshot=query))
    monkeypatch.setattr(amazingdata_fetcher, 'tgw_configured', lambda: True)
    return state


@pytest.mark.parametrize('code,tgw', [
    ('sh510300', '510300.SH'), ('159915', '159915.SZ'),
    ('530100', '530100.SH'), ('600519', '600519.SH'), ('000001', '000001.SZ'),
])
def test_latest_exchange_time_and_last_price_mapping(snapshot, code, tgw):
    quote = amazingdata_fetcher.AmazingDataFetcher().get_realtime_quote(code)
    assert quote.code == tgw.split('.')[0]
    assert quote.source == 'amazingdata'
    assert quote.price == 3.3  # close=0 不能当成盘中价格，也不能取返回表的最后一行。
    assert quote.change_amount == pytest.approx(.3)
    assert quote.change_pct == pytest.approx(10.)
    assert quote.amplitude == pytest.approx(100 / 6)
    assert quote.amount == 33000
    assert quote.volume is None  # AD 文档未明确成交量单位，不猜测「股/手」。
    assert snapshot.calls == [([tgw], dict(begin_date=20261105, end_date=20261105,
                                         begin_time=95500000, end_time=100000000))]


@pytest.mark.parametrize('shape', ['nested', 'flat', 'table'])
def test_sdk_return_shapes_and_datetime_index(snapshot, shape):
    snapshot.shape = shape
    snapshot.frame = snapshot.frame.set_index('trade_time')
    assert market.get_latest_snapshot('510300')['last'] == 3.3


@pytest.mark.parametrize('times', [
    ['2026-11-04 09:59:59', '2026-11-04 09:58:00'],
    ['2026-11-05 09:54:59', '2026-11-05 09:50:00'],
    ['2026-11-05 10:00:01', '2026-11-05 10:01:00'],
    ['bad', None],
])
def test_stale_future_or_unverifiable_snapshots_are_rejected(snapshot, times):
    snapshot.frame['trade_time'] = times
    assert market.get_latest_snapshot('510300') is None


def test_utc_exchange_times_are_converted_to_shanghai(snapshot):
    snapshot.frame['trade_time'] = pd.to_datetime(['2026-11-05 01:59:59Z', '2026-11-05 01:58:00Z'])
    assert market.get_latest_snapshot('510300')['last'] == 3.3


@pytest.mark.parametrize('now,trade_time,begin,end', [
    ('2026-11-05 12:00', '2026-11-05 11:30', 112500000, 113000000),
    ('2026-11-05 20:00', '2026-11-05 15:00', 145500000, 150000000),
    ('2026-11-05 00:01', '2026-11-05 00:00:30', 0, 100000),
])
def test_query_window_respects_lunch_close_and_day_boundary(snapshot, now, trade_time, begin, end):
    snapshot.frame = snapshot.frame.iloc[:1].copy()
    snapshot.frame['trade_time'] = pd.to_datetime([trade_time])
    assert market.get_latest_snapshot('510300', now=now) is not None
    assert snapshot.calls[0][1] == dict(begin_date=20261105, end_date=20261105,
                                      begin_time=begin, end_time=end)


@pytest.mark.parametrize('last', [0, -1, float('nan'), float('inf'), None, 'bad'])
def test_invalid_latest_price_never_uses_close_or_an_older_row(snapshot, last):
    snapshot.frame['last'] = pd.Series([last, 3.1], dtype=object)
    snapshot.frame['close'] = 9.9
    assert amazingdata_fetcher.AmazingDataFetcher().get_realtime_quote('510300') is None


@pytest.mark.parametrize('case', ['empty', 'no_time', 'unsupported'])
def test_absent_snapshots_and_unsupported_codes(snapshot, case):
    if case == 'empty':
        snapshot.frame = pd.DataFrame()
    elif case == 'no_time':
        snapshot.frame = snapshot.frame.drop(columns='trade_time')
    assert market.get_latest_snapshot('920001' if case == 'unsupported' else '510300') is None
    if case == 'unsupported':
        assert not snapshot.calls


class Tencent:
    priority = 0
    REALTIME_VARIANTS = {'tencent': {}}

    def get_realtime_quote(self, code):
        return UnifiedRealtimeQuote(code=code, source='tencent', price=9.,
                                    volume_ratio=1., turnover_rate=2., pe_ratio=3., pb_ratio=4.,
                                    total_mv=5., circ_mv=6., amplitude=7.)


def manager(monkeypatch, fetchers, priority=DEFAULT_REALTIME_PRIORITY):
    monkeypatch.setattr('src.config.get_config', lambda: SimpleNamespace(
        enable_realtime_quote=True, realtime_source_priority=priority))
    return DataFetcherManager(fetchers=fetchers)


def test_ad_primary_survives_supplement_and_reaches_batch_consumer(snapshot, monkeypatch):
    fm = manager(monkeypatch, [Tencent(), amazingdata_fetcher.AmazingDataFetcher()])
    quote = fm.get_realtime_quote('510300')
    assert quote.source == 'amazingdata'
    assert quote.price == 3.3
    assert quote.volume_ratio == 1.  # 补字段不能覆盖 AD 最新价。
    monkeypatch.setattr('data_provider.manager.get_fetcher', lambda: fm)
    assert realtime_quotes(['510300'])[0] == {'510300': 3.3}
    assert _FALLBACK_REALTIME_PRIORITY == DEFAULT_REALTIME_PRIORITY


@pytest.mark.parametrize('failure', [RuntimeError('offline'), SystemExit(0), None, 'stale'])
def test_ad_failure_empty_or_stale_data_falls_back(snapshot, monkeypatch, failure):
    if failure == 'stale':
        snapshot.frame['trade_time'] = '2026-11-04 15:00'
    elif failure is None:
        snapshot.frame = pd.DataFrame()
    else:
        snapshot.failure = failure
    fm = manager(monkeypatch, [amazingdata_fetcher.AmazingDataFetcher(), Tencent()])
    assert fm.get_realtime_quote('510300').source == 'tencent'


def test_ad_without_credentials_is_optional_without_unknown_source_warning(monkeypatch, caplog):
    monkeypatch.setattr(amazingdata_fetcher, 'tgw_configured', lambda: False)
    fm = manager(monkeypatch, [Tencent()], priority='amazingdata,tencent')
    assert fm.get_realtime_quote('510300').price == 9.
    assert '未声明的源名' not in caplog.text


def test_explicit_realtime_priority_is_preserved(snapshot, monkeypatch):
    fm = manager(monkeypatch, [amazingdata_fetcher.AmazingDataFetcher(), Tencent()],
                 priority='tencent,amazingdata')
    assert fm.get_realtime_quote('510300').source == 'tencent'
    assert not snapshot.calls


def test_config_default_uses_ad_first(monkeypatch):
    monkeypatch.delenv('REALTIME_SOURCE_PRIORITY', raising=False)
    monkeypatch.setattr('src.config.setup_env', lambda: None)
    assert Config._load_from_env().realtime_source_priority.split(',')[0] == 'amazingdata'
