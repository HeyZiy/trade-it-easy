"""共享数据能力的接口检查；SDK 边界全部替换，测试不登录真实服务。"""
import importlib
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from data_provider.fetchers.amazingdata import announcements, basics, fundamentals, industries, market
from data_provider.fetchers.amazingdata.client import AmazingDataClient, client
from data_provider.types import DataFetchError

client_module = importlib.import_module('data_provider.fetchers.amazingdata.client')


def test_credentials_choose_whole_group(monkeypatch):
    for prefix in ('TGW', 'AD'):
        for field in ('USERNAME', 'PASSWORD', 'HOST', 'PORT'):
            monkeypatch.delenv(f'{prefix}_{field}', raising=False)
    for field in ('USERNAME', 'PASSWORD', 'HOST', 'PORT'):
        monkeypatch.setenv(f'AD_{field}', '1')
    assert client_module.is_configured()
    assert client_module._credentials()['username'] == '1'
    monkeypatch.setenv('TGW_USERNAME', '2')
    assert not client_module.is_configured()  # 不将两套账号的字段混用。
    for field in ('PASSWORD', 'HOST', 'PORT'):
        monkeypatch.setenv(f'TGW_{field}', '2')
    assert set(client_module._credentials().values()) == {'2'}


@pytest.mark.parametrize('system_exit', [False, True])
def test_login_retries_and_failed_session_stays_failed(monkeypatch, system_exit):
    for field in ('USERNAME', 'PASSWORD', 'HOST', 'PORT'):
        monkeypatch.setenv(f'TGW_{field}', '1')
    monkeypatch.setattr(client_module, '_LOGIN_RETRIES', 2)
    monkeypatch.setattr(client_module, '_LOGIN_RETRY_DELAY', 0)
    calls = []

    def fail(**kwargs):
        calls.append(kwargs)
        if system_exit:
            raise SystemExit(0)
        raise ConnectionError('offline')

    monkeypatch.setitem(sys.modules, 'AmazingData', SimpleNamespace(login=fail))
    session = AmazingDataClient()
    with pytest.raises(DataFetchError):
        session.get_info_data()
    with pytest.raises(DataFetchError, match='已尝试失败'):
        session.get_base_data()
    assert len(calls) == 2


def test_current_lists_preserve_sdk_codes_for_all_asset_types(monkeypatch):
    class SDK:
        def get_code_list(self, security_type):
            return {
                'EXTRA_STOCK_A_SH_SZ': pd.DataFrame({'MARKET_CODE': ['000001.SZ']}),
                'EXTRA_ETF': ['513180.SH'], 'EXTRA_KZZ': {'113001.SH': 'bond'},
            }[security_type]

    monkeypatch.setattr(client, 'get_base_data', lambda: SDK())
    assert basics.get_stock_list() == ['000001.SZ']
    assert basics.get_fund_list() == ['513180.SH']
    assert basics.get_bond_list() == ['113001.SH']
    assert basics.fetch_code_list() == ['000001']


def test_market_value_aligns_changes_and_announcements_without_future_fill(monkeypatch):
    dates = pd.date_range('2026-01-01', periods=6)
    prices = pd.DataFrame({'600001.SH': 10.0, '600002.SH': 10.0}, index=dates)
    history = pd.DataFrame({
        'MARKET_CODE': ['600001.SH'] * 4,
        'CHANGE_DATE': ['20260102', '20260103', '20260101', '20260107'],
        'ANN_DATE': ['20260102', '20260104', '20260105', '20260107'],
        'FLOAT_A_SHARE': [100., 200., 50., 999.], 'TOT_SHARE': [300., 600., 150., 999.],
    })

    class SDK:
        def get_equity_structure(self, codes, **kwargs):
            assert codes == ['600001.SH', '600002.SH']
            assert kwargs == {'begin_date': 19900101, 'end_date': 20260106}
            return history.copy()

    monkeypatch.setattr(market, 'get_close_price', lambda *args: prices)
    monkeypatch.setattr(client, 'get_info_data', lambda: SDK())
    result = fundamentals.get_float_market_value(list(prices), 20260101, 20260106)
    assert pd.isna(result.iloc[0, 0])
    assert result['600001.SH'].iloc[1:].tolist() == [1e7, 1e7, 2e7, 2e7, 2e7]
    assert result['600002.SH'].isna().all()
    total = fundamentals.get_total_market_value(list(prices), 20260101, 20260106)
    assert total['600001.SH'].iloc[-1] == 6e7
    # 缺流通股本时保留缺失，不能拿总股本代替。
    history.drop(columns='FLOAT_A_SHARE', inplace=True)
    assert fundamentals.get_float_market_value(list(prices), 20260101, 20260106).isna().all().all()


@pytest.mark.parametrize('asset_type', ['stock', 'fund', 'bond'])
@pytest.mark.parametrize('local', [False, True])
def test_announcement_list_routes_asset_and_cache_mode(monkeypatch, tmp_path, asset_type, local):
    result = pd.DataFrame({'TITLE': ['test']})

    class SDK:
        def __getattr__(self, name):
            assert name == f'get_announcement_{asset_type}_list'

            def request(**kwargs):
                assert kwargs['code_list'] == ['600001.SH']
                assert kwargs['is_local'] is local
                assert ('begin_date' in kwargs) is not local
                assert ('end_date' in kwargs) is not local
                return result
            return request

    monkeypatch.setattr(client, 'get_info_data', lambda: SDK())
    assert announcements.get_announcement_list(
        asset_type, ['600001.SH'], 20260101, 20260102,
        local_path=str(tmp_path), is_local=local) is result


def test_announcement_tool_preserves_tag_filter_and_download(monkeypatch, tmp_path):
    from research.tools.announcement_analysis import search_announcements, download_pdfs

    frame = pd.DataFrame({'SOURCE_ID': ['a', 'b'], 'TAG_ID': ['10301', '10408'],
                          'TITLE': ['report', 'buyback'],
                          'PUBLISH_TIME': ['2026-01-02', '2026-01-01']})
    pdf = tmp_path / 'a.pdf'
    pdf.write_bytes(b'pdf')

    class SDK:
        def get_announcement_stock_list(self, **kwargs):
            return frame

        def get_announcement_stock(self, selected, **kwargs):
            assert selected['SOURCE_ID'].tolist() == ['a']
            return {'a': str(pdf)}, selected

    monkeypatch.setattr(client, 'get_info_data', lambda: SDK())
    selected = search_announcements(['600001.SH'], 20260101, 20260102,
                                    tag_ids=['10301'], local_path=str(tmp_path))
    assert selected['SOURCE_ID'].tolist() == ['a']
    assert download_pdfs(selected, 20260101, 20260102, local_path=str(tmp_path)) == {'a': str(pdf)}


def test_industry_dates_and_new_stock_requests_do_not_use_stale_dummy_cache(monkeypatch):
    base = pd.DataFrame({'INDEX_CODE': ['A', 'B'], 'LEVEL_TYPE': [1, 1],
                          'LEVEL1_NAME': ['first', 'second']})
    members = {
        'A': pd.DataFrame({'CON_CODE': ['600001.SH', '600002.SH'],
                           'INDATE': ['20200101', '20200101'],
                           'OUTDATE': ['20260103', None]}),
        'B': pd.DataFrame({'CON_CODE': ['600001.SH'], 'INDATE': ['20260103'], 'OUTDATE': [None]}),
    }
    monkeypatch.setattr(industries, 'fetch_industry_base', lambda: base)
    monkeypatch.setattr(industries, 'fetch_industry_members', lambda code: {code: members[code]})
    assert industries.get_stock_industry_map(['600001.SH'], 20260102) == {'600001.SH': 'A'}
    result = industries.get_industry_dummies(['600001.SH', '600002.SH', '600003.SH'], 20260104)
    assert result.loc['600001.SH', 'B'] == 1
    assert result.loc['600002.SH', 'A'] == 1
    assert result.loc['600003.SH'].isna().all()
