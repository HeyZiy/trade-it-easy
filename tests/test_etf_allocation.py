"""市场观察周报及配置参考取价的测试。"""
from types import SimpleNamespace

import pandas as pd
import pytest

from src.etf import allocation


@pytest.fixture(autouse=True)
def quotes(monkeypatch):
    monkeypatch.setattr(allocation, "_raw_market_pe", lambda: {"pe": 14.5, "pe_pct": 40.0})
    monkeypatch.setattr(allocation, "_raw_treasury_yield_y10", lambda: 2.0)
    monkeypatch.setattr(allocation, "_raw_erp_percentile", lambda: {"erp_pct": 55.0})


def test_weekly_report_uses_market_data_only(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("市场观察周报不应读取账户、配置或单只标的行情")

    for name in ("_raw_snapshot", "_raw_valuation_rows", "_raw_trend_bars_ad",
                 "_raw_daily_close", "_raw_realtime_prices"):
        monkeypatch.setattr(allocation, name, unexpected)

    report = allocation.run()

    assert report.startswith("# 市场观察周报")
    assert [line for line in report.splitlines() if line.startswith("## ")] == ["## 市场观察"]
    assert "近 5 年 **40%** 分位" in report
    assert "国债 2.00%" in report and "**风险溢价**" in report and "55%" in report
    assert all(text not in report for text in ("本次建议", "策略参考", "持仓数据不可用", "BUY ", "SELL "))


def test_unavailable_market_pe_still_shows_treasury_yield(monkeypatch):
    monkeypatch.setattr(allocation, "_raw_market_pe", lambda: None)
    report = allocation.run()
    assert "**全市场 PE**：数据不可用" in report
    assert "**10 年期国债收益率**：2.00%" in report


def test_erp_failure_tolerated_but_market_pe_failure_propagates(monkeypatch):
    def unavailable():
        raise RuntimeError("down")

    monkeypatch.setattr(allocation, "_raw_erp_percentile", unavailable)
    report = allocation.run()
    assert "风险溢价" in report and "自身近 5 年" not in report
    monkeypatch.setattr(allocation, "_raw_market_pe", unavailable)
    with pytest.raises(RuntimeError, match="down"):
        allocation.run()


def test_missing_prices_daily_first_realtime_only_for_missed(monkeypatch):
    def _daily(code):
        return pd.DataFrame({"close": [4.95]}) if code == "510300" else None

    monkeypatch.setattr(allocation, "_raw_daily_close", _daily)
    seen = []

    def _fake_rt(codes):
        seen.append(list(codes))
        return {"510500": 6.1}

    monkeypatch.setattr(allocation, "_raw_realtime_prices", _fake_rt)
    baseline = [SimpleNamespace(code="510300"), SimpleNamespace(code="510500")]
    prices = allocation._fetch_missing_prices(baseline, [])

    assert prices == {"510300": 4.95, "510500": 6.1}
    assert seen == [["510500"]]  # 日线命中的不再触达实时；失败代码收拢一批


def test_missing_prices_batch_realtime_fallback(monkeypatch):
    def _boom(code):
        raise RuntimeError("日线不可用")

    monkeypatch.setattr(allocation, "_raw_daily_close", _boom)
    seen = []

    def _fake_rt(codes):
        seen.append(list(codes))
        return {"510300": 4.9}

    monkeypatch.setattr(allocation, "_raw_realtime_prices", _fake_rt)
    prices = allocation._fetch_missing_prices([SimpleNamespace(code="510300")], [])

    assert prices == {"510300": 4.9}
    assert seen == [["510300"]]  # 全部日线失败 → 一批实时兜底
