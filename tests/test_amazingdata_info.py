"""质量池 InfoData 请求不得进入全历史 HDF5 缓存模式。"""

import pytest
import pandas as pd
import os
from pathlib import Path

from data_provider.fetchers import amazingdata_info as info


def test_execution_data_uses_daily_info_and_bounded_market_query(monkeypatch):
    expected = pd.DataFrame({'security_status': ['0']}, index=['600001.SH'])

    class SDK:
        def get_code_info(self, security_type):
            assert security_type == 'EXTRA_STOCK_A_SH_SZ'
            return expected

        def query_snapshot(self, codes, **kwargs):
            assert codes == ['600001.SH']
            assert kwargs == dict(begin_date=20261015, end_date=20261015,
                                  begin_time=92900000, end_time=93100000)
            return {'600001.SH': pd.DataFrame({'last': [10.]})}

    monkeypatch.setattr(info.AmazingDataFetcher, 'get_base_data', lambda: SDK())
    monkeypatch.setattr(info.AmazingDataFetcher, '_market_data', SDK())
    monkeypatch.setattr(info, '_info', lambda: pytest.fail('不得查历史信息表'))
    daily, snapshots = info.fetch_execution_data(
        ['600001'], pd.Timestamp('2026-10-15 09:29'), pd.Timestamp('2026-10-15 09:31'))
    pd.testing.assert_frame_equal(daily, expected)
    assert snapshots['600001'].iloc[0]['last'] == 10.


def test_signal_universe_uses_exact_date_and_cleans_sdk_cache(monkeypatch, tmp_path):
    paths = []

    class SDK:
        def get_hist_code_list(self, security_type, start_date, end_date, local_path):
            assert security_type == 'EXTRA_STOCK_A_SH_SZ'
            assert start_date == end_date == 20261009
            cache = Path(local_path)
            paths.append(cache)
            (cache / 'history.pkl').write_bytes(b'sdk cache')
            return ['600519.SH', '000001.SZ', '300001.SZ', '688001.SH']

        def get_code_list(self, *args):
            pytest.fail('Historical signal must not query the current universe')

    monkeypatch.setattr(info.AmazingDataFetcher, 'get_base_data', lambda: SDK())
    monkeypatch.setenv('AMAZING_DATA_DIR', str(tmp_path))
    from src.quality_pool import feeds
    assert feeds.fetch_universe('2026-10-09') == {'600519', '000001'}
    assert paths and all(not p.exists() for p in paths)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('method,args,fetch', [
    ('get_industry_base_info', (), 'fetch_industry_base'),
    ('get_industry_constituent', ('801010.SI',), 'fetch_industry_members'),
])
def test_industry_sdk_cache_is_temporary(monkeypatch, tmp_path, method, args, fetch):
    request_paths = []
    expected = pd.DataFrame({'INDEX_CODE': ['801010.SI']})

    class SDK:
        def __getattr__(self, name):
            assert name == method

            def request(*codes, local_path, is_local):
                assert not is_local
                assert codes == ((['801010.SI'],) if args else ())
                assert local_path.endswith(os.sep)
                cache = Path(local_path)
                assert cache.resolve() != tmp_path.resolve()
                request_paths.append(cache)
                target = cache / 'infodata' / 'industry_constituent'
                target.mkdir(parents=True)
                (target / '801010.SI.h5').write_bytes(b'sdk cache')
                return expected.copy()
            return request

    monkeypatch.setattr(info, '_info', lambda: SDK())
    monkeypatch.setenv('AMAZING_DATA_DIR', str(tmp_path))
    result = getattr(info, fetch)(*args)
    pd.testing.assert_frame_equal(result, expected)
    assert request_paths and all(not p.exists() for p in request_paths)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('fetch,args,method', [
    (info.fetch_industry_base, (), 'get_industry_base_info'),
    (info.fetch_industry_members, ('801010.SI',), 'get_industry_constituent'),
    (info.fetch_code_list, ('EXTRA_STOCK_A_SH_SZ', '2026-10-09'), 'get_hist_code_list'),
])
def test_sdk_write_failure_cleans_cache_and_retry_uses_new_directory(
        monkeypatch, tmp_path, fetch, args, method):
    cache_root = tmp_path / 'sdk-cache'
    permanent = cache_root / 'infodata' / 'existing.h5'
    permanent.parent.mkdir(parents=True)
    permanent.write_bytes(b'permanent data')
    paths = []
    failure = OSError('SDK write failed')

    class SDK:
        def __getattr__(self, name):
            assert name == method

            def request(*codes, local_path, **kwargs):
                assert local_path.endswith(os.sep)
                if method == 'get_hist_code_list':
                    assert kwargs == {'start_date': 20261009, 'end_date': 20261009}
                else:
                    assert kwargs == {'is_local': False}
                cache = Path(local_path)
                assert cache.parent == cache_root
                paths.append(cache)
                (cache / 'partial.h5').write_bytes(b'partial download')
                if len(paths) == 1:
                    raise failure
                return ['600519.SH'] if method == 'get_hist_code_list' else pd.DataFrame()
            return request

    sdk = SDK()
    monkeypatch.setenv('AMAZING_DATA_DIR', str(cache_root))
    monkeypatch.setattr(info, '_info', lambda: sdk)
    monkeypatch.setattr(info.AmazingDataFetcher, 'get_base_data', lambda: sdk)
    with pytest.raises(info.DataFetchError, match='SDK write failed') as error:
        fetch(*args)
    assert error.value.__cause__ is failure
    assert not paths[0].exists()
    result = fetch(*args)
    if method == 'get_hist_code_list':
        assert result == ['600519']
    assert len(set(paths)) == 2
    assert all(not cache.exists() for cache in paths)
    assert permanent.read_bytes() == b'permanent data'
    assert list(cache_root.iterdir()) == [permanent.parent]


def test_nested_sdk_requests_cannot_delete_each_others_cache(monkeypatch, tmp_path):
    paths = []
    expected = pd.DataFrame({'INDEX_CODE': ['801010.SI']})

    class SDK:
        def get_industry_base_info(self, local_path, is_local):
            outer = Path(local_path)
            paths.append(outer)
            assert outer.exists()
            pd.testing.assert_frame_equal(info.fetch_industry_members('801010.SI'), expected)
            assert outer.exists()
            assert not paths[1].exists()
            return expected

        def get_industry_constituent(self, codes, local_path, is_local):
            inner = Path(local_path)
            paths.append(inner)
            assert inner != paths[0]
            assert inner.exists() and paths[0].exists()
            return expected

    monkeypatch.setenv('AMAZING_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(info, '_info', lambda: SDK())
    pd.testing.assert_frame_equal(info.fetch_industry_base(), expected)
    assert len(paths) == 2 and all(not path.exists() for path in paths)
    assert list(tmp_path.iterdir()) == []


def test_temporary_cache_uses_current_config_and_creates_missing_root(monkeypatch, tmp_path):
    paths = []

    class SDK:
        def get_industry_base_info(self, local_path, is_local):
            paths.append(Path(local_path))
            assert paths[-1].exists()
            return pd.DataFrame()

    monkeypatch.setattr(info, '_info', lambda: SDK())
    for name in ('first', 'second'):
        root = tmp_path / name
        monkeypatch.setenv('AMAZING_DATA_DIR', str(root))
        info.fetch_industry_base()
        assert paths[-1].parent == root
        assert list(root.iterdir()) == []


def test_current_code_list_does_not_create_sdk_cache(monkeypatch, tmp_path):
    root = tmp_path / 'unused'

    class SDK:
        def get_code_list(self, security_type):
            assert security_type == 'EXTRA_STOCK_A_SH_SZ'
            return ['600519.SH', '000001.SZ']

    monkeypatch.setenv('AMAZING_DATA_DIR', str(root))
    monkeypatch.setattr(info.AmazingDataFetcher, 'get_base_data', lambda: SDK())
    assert info.fetch_code_list() == ['600519', '000001']
    assert not root.exists()


@pytest.mark.parametrize("fetch,args", [
    (info.fetch_income, ("2024-07-01", "2026-09-30")),
    (info.fetch_balance_sheets, ("2024-07-01", "2026-09-30")),
    (info.fetch_equity_structure, ("1990-01-01", "2026-09-30")),
    (info.fetch_status, ("2026-10-08",)),
    (info.fetch_restricted, ("2026-10-08", "2027-01-06")),
])
def test_info_requests_never_write_full_history_cache(monkeypatch, fetch, args):
    class OnlineOnly:
        def __getattr__(self, method):
            def request(codes, **kwargs):
                assert "local_path" not in kwargs, (
                    f"{method} 仍进入全历史 HDF5 缓存模式")
                assert "is_local" not in kwargs
                assert isinstance(kwargs["begin_date"], int)
                assert isinstance(kwargs["end_date"], int)
                return pd.DataFrame()
            return request

    monkeypatch.setattr(info, "_info", lambda: OnlineOnly())
    fetch(["600001"], *args)


@pytest.mark.parametrize("as_dict", [False, True])
def test_date_requests_normalize_sdk_shapes_and_trim_columns(monkeypatch, as_dict):
    calls = []

    class SDK:
        def get_history_stock_status(self, codes, **kwargs):
            calls.append((codes, kwargs))
            frames = {c: pd.DataFrame({"MARKET_CODE": [c],
                                       "TRADE_DATE": ["2026-10-08"],
                                       "IS_ST_SEC": ["1"],
                                       "UNUSED": ["large payload"]}) for c in codes}
            return frames if as_dict else pd.concat(frames.values(), ignore_index=True)

    monkeypatch.setattr(info, "_info", lambda: SDK())
    codes = [f"{600001 + i}" for i in range(70)]
    result = info.fetch_status(codes, "2026-10-08")
    assert len(result) == len(codes)
    assert "UNUSED" not in result
    assert all(len(batch) <= 32 for batch, _ in calls)
    assert all(kwargs == {"begin_date": 20261008, "end_date": 20261008}
               for _, kwargs in calls)


def test_income_copies_only_needed_fields(monkeypatch):
    wide = pd.DataFrame({"REPORTING_PERIOD": ["20260630"],
                         "NET_PRO_EXCL_MIN_INT_INC": [100.0],
                         "NET_PRO_INCL_MIN_INT_INC": [120.0],
                         "UNUSED": [999.0]})

    class SDK:
        def get_income(self, codes, **kwargs):
            return {codes[0]: wide}

    monkeypatch.setattr(info, "_info", lambda: SDK())
    result = info.fetch_income(["600001"], "2024-07-01", "2026-09-30")
    assert set(result["600001"].columns) == {
        "MARKET_CODE", "REPORTING_PERIOD", "NET_PRO_EXCL_MIN_INT_INC",
        "NET_PRO_INCL_MIN_INT_INC"}
    wide.loc[0, "NET_PRO_EXCL_MIN_INT_INC"] = 0
    assert result["600001"].loc[0, "NET_PRO_EXCL_MIN_INT_INC"] == 100
    assert result["600001"].loc[0, "NET_PRO_INCL_MIN_INT_INC"] == 120


def test_restricted_dict_is_not_silently_dropped(monkeypatch):
    class SDK:
        def get_equity_restricted(self, codes, **kwargs):
            assert kwargs == {"begin_date": 20261008, "end_date": 20270106}
            return {codes[0]: pd.DataFrame({"LIST_DATE": ["2026-10-10"]})}

    monkeypatch.setattr(info, "_info", lambda: SDK())
    result = info.fetch_restricted(["600001"], "2026-10-08", "2027-01-06")
    assert result["MARKET_CODE"].tolist() == ["600001.SH"]


def test_server_out_of_window_status_cannot_pollute_today(monkeypatch):
    class SDK:
        def get_history_stock_status(self, codes, **kwargs):
            return {codes[0]: pd.DataFrame({
                "TRADE_DATE": ["2013-01-01", "2026-10-08", "2026-10-09"],
                "IS_ST_SEC": ["1", "0", "1"],
            })}

    monkeypatch.setattr(info, "_info", lambda: SDK())
    result = info.fetch_status(["600001"], "2026-10-08")
    assert result["TRADE_DATE"].tolist() == ["2026-10-08"]
    assert result["IS_ST_SEC"].tolist() == ["0"]


def test_equity_history_returns_only_latest_asof_record(monkeypatch):
    class SDK:
        def get_equity_structure(self, codes, **kwargs):
            return {codes[0]: pd.DataFrame({
                "CHANGE_DATE": ["2020-01-01", "2027-01-01", "2010-01-01"],
                "TOT_SHARE": [200.0, 300.0, 100.0],
            })}

    monkeypatch.setattr(info, "_info", lambda: SDK())
    result = info.fetch_equity_structure(["600001"], "1990-01-01", "2026-09-30")
    assert result["600001"]["TOT_SHARE"].tolist() == [200.0]


def test_installed_sdk_dates_reach_download_without_cache_mode(monkeypatch):
    """离线调用真实 SDK 的方法，只替换下载边界，不登录、不访问服务器。"""
    ad = pytest.importorskip("AmazingData")
    calls = []

    class Download:
        def __init__(self, path):
            pass

        def __getattr__(self, name):
            def request(codes, **kwargs):
                assert set(kwargs) == {"begin_date", "end_date"}
                calls.append(name)
                return pd.DataFrame()
            return request

    sdk = ad.InfoData.__new__(ad.InfoData)
    # 每个真实方法的 globals 指向同一 SDK 模块。
    monkeypatch.setitem(ad.InfoData.get_income.__globals__, "DownloadInfoData", Download)
    monkeypatch.setattr(info, "_info", lambda: sdk)
    info.fetch_income(["600001"], "2024-07-01", "2026-09-30")
    info.fetch_balance_sheets(["600001"], "2024-07-01", "2026-09-30")
    info.fetch_equity_structure(["600001"], "1990-01-01", "2026-09-30")
    info.fetch_status(["600001"], "2026-10-08")
    info.fetch_restricted(["600001"], "2026-10-08", "2027-01-06")
    assert len(calls) == 5


def test_factor_result_is_windowed_even_though_sdk_requires_cache(monkeypatch, tmp_path):
    dates = pd.date_range("2020-01-01", "2026-10-08", freq="B")

    class Base:
        def get_adj_factor(self, codes, **kwargs):
            return pd.DataFrame({c: 1.0 for c in codes}, index=dates)

    monkeypatch.setattr(info.AmazingDataFetcher, "get_base_data", lambda: Base())
    monkeypatch.setattr(info, "AMAZINGDATA_CACHE_DIR", str(tmp_path))
    result = info.fetch_adj_factors(["600001"], "2026-01-01", "2026-09-30")
    assert (result.index < pd.Timestamp("2026-01-01")).sum() == 1
    assert result.index.max() <= pd.Timestamp("2026-09-30")
