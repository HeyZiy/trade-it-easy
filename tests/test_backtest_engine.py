# -*- coding: utf-8 -*-
"""本地日线账户引擎的行为边界测试：成本、T+1、整手、净值与回合配对。"""
import numpy as np
import pandas as pd
import pytest

from research.tools.backtest.engine import (Account, annual_returns, metrics,
                                            round_trip_stats)


def test_buy_applies_slippage_and_min_commission():
    acct = Account(100_000.0, commission=0.0001, min_commission=5.0, slippage=0.002)
    assert acct.buy("2024-01-02", "510300", 1000, 4.0)
    fill = 4.002
    fee = max(1000 * fill * 0.0001, 5.0)  # 0.4002 < 5 → 最低佣金
    assert fee == 5.0
    assert acct.cash == pytest.approx(100_000 - 1000 * fill - fee)
    assert acct.shares("510300") == 1000
    assert acct.ledger[0]["price"] == pytest.approx(fill)


def test_buy_rejects_when_cash_insufficient():
    acct = Account(1000.0)
    assert not acct.buy("2024-01-02", "510300", 1000, 4.0)
    assert acct.cash == 1000.0
    assert acct.positions == {}


def test_closeable_is_t_plus_one():
    acct = Account(100_000.0)
    acct.buy("2024-01-02", "510300", 1000, 4.0)
    assert acct.closeable("510300", "2024-01-02") == 0
    assert acct.closeable("510300", "2024-01-03") == 1000


def test_sell_applies_slippage_and_removes_position():
    acct = Account(100_000.0)
    acct.buy("2024-01-02", "510300", 1000, 4.0)
    cash_after_buy = acct.cash
    assert acct.sell("2024-01-03", "510300", 1000, 4.5)
    fill = 4.498
    fee = max(1000 * fill * 0.0001, 5.0)
    assert acct.cash == pytest.approx(cash_after_buy + 1000 * fill - fee)
    assert acct.shares("510300") == 0
    assert "510300" not in acct.positions


def test_sell_rejects_over_share():
    acct = Account(100_000.0)
    acct.buy("2024-01-02", "510300", 1000, 4.0)
    assert not acct.sell("2024-01-03", "510300", 1500, 4.5)
    assert acct.shares("510300") == 1000


def test_value_and_exposure():
    acct = Account(100_000.0)
    acct.buy("2024-01-02", "510300", 1000, 4.0)
    prices = {"510300": 5.0}
    assert acct.exposure(prices) == pytest.approx(5000.0)
    assert acct.value(prices) == pytest.approx(acct.cash + 5000.0)


def test_metrics_flat_nav_zero_vol():
    idx = pd.bdate_range("2024-01-01", periods=244)
    nav = pd.Series(1_000_000.0, index=idx)
    expo = pd.Series(0.0, index=idx)
    met = metrics(nav, expo, 0, 1_000_000.0)
    assert met["年化收益%"] == pytest.approx(0.0, abs=1e-9)
    assert met["最大回撤%"] == pytest.approx(0.0, abs=1e-9)
    assert met["期末终值(万)"] == pytest.approx(100.0)
    assert np.isnan(met["Sharpe"])


def test_round_trip_fifo_pairs_cost_inclusive():
    acct = Account(100_000.0, commission=0.0, min_commission=0.0, slippage=0.0)
    acct.buy("2024-01-02", "A", 1000, 4.0)
    acct.buy("2024-01-03", "A", 1000, 5.0)
    acct.sell("2024-01-04", "A", 1500, 6.0)
    rt = round_trip_stats(acct.ledger)
    assert rt["完整回合数"] == 2  # 1000@4 与 500@5 配 1500@6
    assert rt["回合胜率%"] == pytest.approx(100.0)
    assert rt["期末未平批次"] == 1  # 剩 500@5


def test_annual_returns_first_year_from_capital():
    idx = pd.DatetimeIndex(["2023-12-29", "2024-12-31"])
    nav = pd.Series([1_100_000.0, 1_210_000.0], index=idx)
    r = annual_returns(nav, 1_000_000.0)
    assert r.loc[2023] == pytest.approx(0.10)
    assert r.loc[2024] == pytest.approx(0.10)