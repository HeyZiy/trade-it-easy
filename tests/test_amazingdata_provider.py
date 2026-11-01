"""公共 AmazingData 查询入口：离线验证会话共享、分批和价格口径。"""

import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider.fetchers import amazingdata_fetcher as fetcher_module
from data_provider.fetchers.amazingdata import market
from data_provider.fetchers.amazingdata.client import client
from data_provider.fetchers.amazingdata._utils import date_to_int
from data_provider.types import DataFetchError


@pytest.fixture
def sdk(monkeypatch):
    calls = []
    frame = pd.DataFrame({
        'kline_time': pd.to_datetime(['2026-10-30', '2026-10-29']),
        'close': [5.0, 10.0], 'volume': [200.0, 100.0],
    })

    class Market:
        def __init__(self, calendar):
            calls.append(('market', calendar))

        def query_kline(self, codes, **kwargs):
            calls.append(('kline', codes, kwargs))
            return {code: frame.copy() for code in codes}

    class Base:
        def get_calendar(self, data_type='str', market='SH'):
            return ['20261029', '20261030']

    def login(**kwargs):
        calls.append(('login',))

    monkeypatch.setitem(sys.modules, 'AmazingData', SimpleNamespace(
        login=login, BaseData=Base, InfoData=lambda: object(), MarketData=Market))
    monkeypatch.setitem(sys.modules, 'AmazingData.utils.constant', SimpleNamespace(
        Period=SimpleNamespace(day=SimpleNamespace(value='day'))))
    for name in ('_TGW_USERNAME', '_TGW_PASSWORD', '_TGW_HOST', '_TGW_PORT'):
        monkeypatch.setenv(name.lstrip('_'), '1')
    cls = client
    for name, value in (('_login_attempted', False), ('_login_success', False),
                        ('_market_data', None), ('_calendar', None),
                        ('_base_data', None), ('_info_data', None)):
        monkeypatch.setattr(cls, name, value)
    return calls, frame


def test_stock_index_and_batch_queries_share_one_session(sdk):
    calls, frame = sdk
    provider = market
    assert not calls
    fetcher = fetcher_module.AmazingDataFetcher()
    stock = fetcher._fetch_raw_data('000001', '2026-10-29', '2026-10-30')
    index = provider.get_benchmark('000001.SH', 20261029, 20261030)
    index_daily = fetcher.get_index_daily('sh000001')
    batch = market.fetch_kline(['000001.SZ', '600519'], '2026-10-29', '2026-10-30')
    assert sum(call[0] == 'login' for call in calls) == 1
    assert sum(call[0] == 'market' for call in calls) == 1
    queries = [call for call in calls if call[0] == 'kline']
    assert [call[1] for call in queries] == [
        ['000001.SZ'], ['000001.SH'], ['000001.SH'],
        ['000001.SZ', '600519.SH']]
    assert all(call[2] == dict(begin_date=20261029, end_date=20261030,
                              period='day') for call in (queries[0], queries[1], queries[3]))
    pd.testing.assert_frame_equal(stock, frame)
    pd.testing.assert_frame_equal(batch['000001'], frame)
    assert index['close'].tolist() == [10.0, 5.0]  # 排序，但不复权或填补价格。
    assert index_daily['close'].tolist() == [10.0, 5.0]


def test_batches_preserve_raw_frames_and_omit_unrequested_data(sdk, monkeypatch):
    calls, frame = sdk
    cls = client
    sdk_market = cls.get_market_data()

    def query(codes, **kwargs):
        calls.append(('batch', codes))
        return {**{c: frame for c in codes}, 'unexpected.SH': frame}

    monkeypatch.setattr(sdk_market, 'query_kline', query)
    codes = [f'{600000 + i}.SH' for i in range(205)]
    raw = market.get_raw_kline(codes, 20261029, 20261030)
    assert [len(call[1]) for call in calls if call[0] == 'batch'] == [200, 5]
    assert list(raw) == codes
    assert all(value is frame for value in raw.values())


@pytest.mark.parametrize('failure', [RuntimeError('offline'), SystemExit(0)])
def test_failed_query_raises_data_error_instead_of_partial_result(sdk, monkeypatch, failure):
    sdk_market = client.get_market_data()
    count = 0

    def query(codes, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise failure
        return {codes[0]: sdk[1]}

    monkeypatch.setattr(sdk_market, 'query_kline', query)
    with pytest.raises(DataFetchError) as error:
        market.fetch_kline([str(600000 + i) for i in range(201)],
                         '2026-10-29', '2026-10-30')
    assert error.value.__cause__ is failure


def test_matrix_layout_and_empty_requests(sdk):
    provider = market
    assert provider.get_raw_kline([], 20261029, 20261030) == {}
    assert not sdk[0]
    matrix = provider.get_kline(['600519.SH'], 20261029, 20261030,
                                fields=['close', 'volume'])
    assert matrix.columns.names == ['field', 'code']
    assert matrix[('close', '600519.SH')].tolist() == [10.0, 5.0]
    assert matrix[('volume', '600519.SH')].tolist() == [100.0, 200.0]


@pytest.mark.parametrize('value', [20261030, '20261030', '2026-10-30',
                                  '2026/10/30', pd.Timestamp('2026-10-30')])
def test_date_formats(value):
    assert date_to_int(value) == 20261030


def test_invalid_dates_fail_before_login(sdk):
    with pytest.raises(ValueError):
        market.get_raw_kline(['600519.SH'], 20261030, 20261029)
    with pytest.raises(ValueError):
        date_to_int('2026-02-30')
    assert not sdk[0]
