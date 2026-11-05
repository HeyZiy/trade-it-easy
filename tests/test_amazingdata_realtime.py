"""AD 批量现价、时间窗口和按代码回退，SDK 边界使用假数据。"""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider.fetchers import amazingdata_fetcher
from data_provider.fetchers.amazingdata import market
from data_provider import realtime_prices
from data_provider.manager import DataFetcherManager, _FALLBACK_REALTIME_PRIORITY
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
        'last': [3.3, 3.1], 'close': [0., 0.],
    })
    state = SimpleNamespace(frame=frame, calls=[], failure=None, shape='nested', missing=set())

    def query(codes, **kwargs):
        state.calls.append((codes, kwargs))
        if state.failure is not None:
            raise state.failure
        if state.shape == 'table':
            return state.frame.assign(MARKET_CODE=codes[0])
        result = {code: state.frame for code in codes if code not in state.missing}
        return {20261105: result} if state.shape == 'nested' else result

    monkeypatch.setattr(market.client, 'get_market_data', lambda: SimpleNamespace(query_snapshot=query))
    monkeypatch.setattr(amazingdata_fetcher, 'tgw_configured', lambda: True)
    return state


@pytest.mark.parametrize('code,tgw', [
    ('sh510300', '510300.SH'), ('159915', '159915.SZ'),
    ('530100', '530100.SH'), ('600519', '600519.SH'), ('000001', '000001.SZ'),
])
def test_latest_exchange_time_and_last_price_mapping(snapshot, code, tgw):
    prices = amazingdata_fetcher.AmazingDataFetcher().get_realtime_prices([code])
    assert prices == {tgw.split('.')[0]: 3.3}  # 取最新 `last`，与快照行顺序无关。
    assert snapshot.calls == [([tgw], dict(begin_date=20261105, end_date=20261105,
                                         begin_time=95500000, end_time=100000000))]


@pytest.mark.parametrize('shape', ['nested', 'flat', 'table'])
def test_sdk_return_shapes_and_datetime_index(snapshot, shape):
    snapshot.shape = shape
    snapshot.frame = snapshot.frame.set_index('trade_time')
    assert market.get_latest_prices(['510300']) == {'510300': 3.3}


@pytest.mark.parametrize('times', [
    ['2026-11-04 09:59:59', '2026-11-04 09:58:00'],
    ['2026-11-05 09:54:59', '2026-11-05 09:50:00'],
    ['2026-11-05 10:00:01', '2026-11-05 10:01:00'],
    ['bad', None],
])
def test_stale_future_or_unverifiable_snapshots_are_rejected(snapshot, times):
    snapshot.frame['trade_time'] = times
    assert market.get_latest_prices(['510300']) == {}


def test_utc_exchange_times_are_converted_to_shanghai(snapshot):
    snapshot.frame['trade_time'] = pd.to_datetime(['2026-11-05 01:59:59Z', '2026-11-05 01:58:00Z'])
    assert market.get_latest_prices(['510300']) == {'510300': 3.3}


@pytest.mark.parametrize('now,trade_time,begin,end', [
    ('2026-11-05 12:00', '2026-11-05 11:30', 112500000, 113000000),
    ('2026-11-05 20:00', '2026-11-05 15:00', 145500000, 150000000),
    ('2026-11-05 00:01', '2026-11-05 00:00:30', 0, 100000),
])
def test_query_window_respects_lunch_close_and_day_boundary(snapshot, now, trade_time, begin, end):
    snapshot.frame = snapshot.frame.iloc[:1].copy()
    snapshot.frame['trade_time'] = pd.to_datetime([trade_time])
    assert market.get_latest_prices(['510300'], now=now) == {'510300': 3.3}
    assert snapshot.calls[0][1] == dict(begin_date=20261105, end_date=20261105,
                                      begin_time=begin, end_time=end)


@pytest.mark.parametrize('last', [0, -1, float('nan'), float('inf'), None, 'bad'])
def test_invalid_latest_price_never_uses_close_or_an_older_row(snapshot, last):
    snapshot.frame['last'] = pd.Series([last, 3.1], dtype=object)
    snapshot.frame['close'] = 9.9
    assert amazingdata_fetcher.AmazingDataFetcher().get_realtime_prices(['510300']) == {}


@pytest.mark.parametrize('case', ['empty', 'no_time', 'unsupported'])
def test_absent_snapshots_and_unsupported_codes(snapshot, case):
    if case == 'empty':
        snapshot.frame = pd.DataFrame()
    elif case == 'no_time':
        snapshot.frame = snapshot.frame.drop(columns='trade_time')
    assert market.get_latest_prices(['920001' if case == 'unsupported' else '510300']) == {}
    if case == 'unsupported':
        assert not snapshot.calls


class Tencent:
    priority = 0
    REALTIME_VARIANTS = {'tencent': {}}

    def __init__(self):
        self.calls = []

    def get_realtime_prices(self, codes):
        self.calls.append(codes)
        return dict.fromkeys(codes, 9.)


def manager(monkeypatch, fetchers, priority=DEFAULT_REALTIME_PRIORITY):
    monkeypatch.setattr('src.config.get_config', lambda: SimpleNamespace(
        enable_realtime_quote=True, realtime_source_priority=priority))
    return DataFetcherManager(fetchers=fetchers)


def test_ad_queries_one_batch_and_only_unpriced_symbols_fall_back(snapshot, monkeypatch):
    tencent = Tencent()
    snapshot.missing.add('159915.SZ')
    fm = manager(monkeypatch, [tencent, amazingdata_fetcher.AmazingDataFetcher()])
    monkeypatch.setattr('data_provider.manager.get_fetcher', lambda: fm)
    assert realtime_prices(['510300', '159915', '600519']) == {'510300': 3.3, '159915': 9., '600519': 3.3}
    assert [codes for codes, _ in snapshot.calls] == [['510300.SH', '159915.SZ', '600519.SH']]
    assert tencent.calls == [['159915']]
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
    assert realtime_prices(['510300'], manager=fm) == {'510300': 9.}


def test_ad_without_credentials_is_optional_without_unknown_source_warning(monkeypatch, caplog):
    monkeypatch.setattr(amazingdata_fetcher, 'tgw_configured', lambda: False)
    fm = manager(monkeypatch, [Tencent()], priority='amazingdata,tencent')
    assert realtime_prices(['510300'], manager=fm) == {'510300': 9.}
    assert '未声明的源名' not in caplog.text


def test_explicit_realtime_priority_is_preserved(snapshot, monkeypatch):
    fm = manager(monkeypatch, [amazingdata_fetcher.AmazingDataFetcher(), Tencent()],
                 priority='tencent,amazingdata')
    assert realtime_prices(['510300'], manager=fm) == {'510300': 9.}
    assert not snapshot.calls


def test_config_default_uses_ad_first(monkeypatch):
    monkeypatch.delenv('REALTIME_SOURCE_PRIORITY', raising=False)
    monkeypatch.setattr('src.config.setup_env', lambda: None)
    assert Config._load_from_env().realtime_source_priority.split(',')[0] == 'amazingdata'
