"""质量池 InfoData 请求不得进入全历史 HDF5 缓存模式。"""

import pytest
import pandas as pd

from data_provider.fetchers import amazingdata_info as info


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
                         "UNUSED": [999.0]})

    class SDK:
        def get_income(self, codes, **kwargs):
            return {codes[0]: wide}

    monkeypatch.setattr(info, "_info", lambda: SDK())
    result = info.fetch_income(["600001"], "2024-07-01", "2026-09-30")
    assert set(result["600001"].columns) == {
        "MARKET_CODE", "REPORTING_PERIOD", "NET_PRO_EXCL_MIN_INT_INC"}
    wide.loc[0, "NET_PRO_EXCL_MIN_INT_INC"] = 0
    assert result["600001"].loc[0, "NET_PRO_EXCL_MIN_INT_INC"] == 100


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
