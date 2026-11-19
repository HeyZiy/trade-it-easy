# -*- coding: utf-8 -*-
"""bars.get_etf_daily 统一入口测试 — 假新浪源（形状归一化/码族拒绝/符号路由）。"""

import pandas as pd
import pytest

import akshare as ak
from data_provider.bars import get_etf_daily, get_etf_daily_em
from data_provider.codes import is_etf_code, is_sh_lof_code, market_suffix


def test_shanghai_lof_uses_its_own_eastmoney_history_endpoint(monkeypatch):
    calls = []

    def lof_history(**kwargs):
        calls.append((kwargs['symbol'], kwargs['adjust']))
        price = 2.0 if not kwargs['adjust'] else 4.0
        return pd.DataFrame({'日期': ['2026-09-02'], '开盘': [price], '最高': [price],
                             '最低': [price], '收盘': [price], '成交量': [1], '成交额': [200]})

    monkeypatch.setattr(ak, 'fund_lof_hist_em', lof_history)
    assert market_suffix('501018') == 'SH' and is_sh_lof_code('501018.SH')
    assert not is_etf_code('501018')
    result = get_etf_daily_em('501018.SH', '2026-09-02')
    assert calls == [('501018', ''), ('501018', 'hfq')]
    assert (result['close'].iloc[0], result['hfq_close'].iloc[0]) == (2.0, 4.0)


def _raw(closes, symbol="sh510300"):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-09-01", periods=n).strftime("%Y-%m-%d"),
        "open": closes, "high": [c * 1.02 for c in closes],
        "low": [c * 0.98 for c in closes], "close": closes,
        "volume": [10000] * n, "amount": [c * 10000 for c in closes],
    })


@pytest.fixture
def capture(monkeypatch):
    """假新浪源：返回注入的行情，并捕获调用时的 symbol。"""
    state = {"symbol": None, "raw": _raw([10.0, 10.1])}

    def fake(symbol):
        state["symbol"] = symbol
        return state["raw"]

    monkeypatch.setattr(ak, "fund_etf_hist_sina", fake)
    return state


def test_shape_normalized(capture):
    df = get_etf_daily("510300")
    assert list(df.columns) == ["date", "open", "high", "low", "close",
                                "volume", "amount"]
    assert df["date"].is_monotonic_increasing
    assert df["date"].iloc[0] == "2026-09-01" and df["amount"].notna().all()


def test_non_etf_rejected(capture):
    assert get_etf_daily("600519") is None  # 个股走 DataFetcherManager


def test_symbol_routes_by_market_suffix(capture):
    get_etf_daily("530100")   # 2025 新沪码族
    assert capture["symbol"] == "sh530100"
    get_etf_daily("159915")
    assert capture["symbol"] == "sz159915"
    get_etf_daily("sh558001")  # 显式前缀优先
    assert capture["symbol"] == "sh558001"
    get_etf_daily("159915.SZ")
    assert capture["symbol"] == "sz159915"


def test_raw_close_untouched(capture):
    """原始价原样返回（现货价口径）：份额折算跳变不在本适配器处理。"""
    closes = [10.0, 10.1, 10.2, 10.1, 10.0, 5.0, 5.05, 5.1]
    capture["raw"] = _raw(closes)
    df = get_etf_daily("510300")
    assert df["close"].tolist() == closes


def test_empty_source_returns_none(capture):
    capture["raw"] = pd.DataFrame()
    assert get_etf_daily("510300") is None


def test_eastmoney_hfq_only_uses_single_normalized_request(monkeypatch):
    calls = []

    def fetch(**kwargs):
        calls.append((kwargs["symbol"], kwargs["adjust"]))
        return pd.DataFrame({"日期": ["2026-09-02", "2026-09-01"], "开盘": [2, 1],
                             "最高": [2, 1], "最低": [2, 1], "收盘": [2, 1]})

    monkeypatch.setattr(ak, "fund_etf_hist_em", fetch)
    result = get_etf_daily_em("510300.SH", "2026-09-02", adjust="hfq")
    assert calls == [("510300", "hfq")]
    assert result["date"].tolist() == ["2026-09-01", "2026-09-02"]
    assert result["hfq_close"].tolist() == [1, 2]
    assert "close" not in result
