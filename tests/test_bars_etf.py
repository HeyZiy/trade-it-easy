# -*- coding: utf-8 -*-
"""bars.get_etf_daily 统一入口测试 — 假新浪源（形状归一化/码族拒绝/符号路由）。"""

import pandas as pd
import pytest

import akshare as ak
from data_provider.bars import get_etf_daily


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


def test_raw_close_untouched(capture):
    """原始价原样返回（现货价口径）：份额折算跳变不在本适配器处理。"""
    closes = [10.0, 10.1, 10.2, 10.1, 10.0, 5.0, 5.05, 5.1]
    capture["raw"] = _raw(closes)
    df = get_etf_daily("510300")
    assert df["close"].tolist() == closes


def test_empty_source_returns_none(capture):
    capture["raw"] = pd.DataFrame()
    assert get_etf_daily("510300") is None
