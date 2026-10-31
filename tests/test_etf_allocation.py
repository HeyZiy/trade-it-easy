# -*- coding: utf-8 -*-
"""周度批次闭环（src/etf/allocation）：单动词 run 的装配/渲染/记账口径。
全部假数据（*_raw seam 注入），不访问网络或真实台账。"""
from types import SimpleNamespace

import pandas as pd
import pytest

from src.etf import allocation
from src.etf.rebalancer import RebalanceOrder, RebalancePlan
from src.etf.config import example_baseline
CORE_BASELINE = example_baseline()


def _order(action="sell", code="510300", name="沪深300ETF"):
    return RebalanceOrder(code=code, name=name, action=action, amount=10_000.0,
                          quantity=2000, current_pct=0.4, target_pct=0.3,
                          reason="超配 10%")


def _alloc(orders=None):
    plan = RebalancePlan(orders=orders if orders is not None else [_order()],
                         total_deviation=0.1, should=True, reason="再平衡",
                         odd_lots=[])
    return {
        "cash": 5_000.0,
        "total_assets": 100_000.0,
        "positions": [{"code": "510300", "current_price": 5.0}],
        "prices": {"510300": 5.0},
        "core_positions": [],
        "target": {},
        "plan": plan,
    }


def _patch_render_seams(monkeypatch):
    monkeypatch.setattr(allocation, "_raw_market_pe",
                        lambda: {"pe": 14.5, "pe_pct": 40.0})
    monkeypatch.setattr(allocation, "_raw_treasury_yield_y10", lambda: 2.0)
    monkeypatch.setattr(allocation, "_raw_erp_percentile",
                        lambda: {"erp_pct": 55.0})
    monkeypatch.setattr(allocation, "_raw_valuation_rows", lambda etfs: [])

    def _no_bars(code, end):
        raise RuntimeError("行情不可用")

    monkeypatch.setattr(allocation, "_raw_trend_bars_ad", lambda codes, end: {})
    monkeypatch.setattr(allocation, "_raw_trend_bars", _no_bars)


@pytest.fixture(autouse=True)
def baseline_for_report_tests(monkeypatch):
    monkeypatch.setattr(allocation, "_baseline", lambda: CORE_BASELINE)


def test_report_never_records_fills(monkeypatch, tmp_path):
    import src.etf_account as core
    path = tmp_path / "etf_account.json"
    core.initialize(path, asof="2025-01-02", cash=1000,
                    positions=[dict(code="510300", name="ETF", count=100, price=3)])
    before = path.read_bytes()
    monkeypatch.setattr(core, "ACCOUNT_PATH", path)
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: _alloc())
    _patch_render_seams(monkeypatch)
    report = allocation.run()
    assert "## 本次建议" in report
    assert "约 2000份" in report
    assert path.read_bytes() == before


# ── run：单动词闭环（假数据装配）──

def test_run_renders_full_report_without_orders(monkeypatch):
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: _alloc(orders=[]))
    _patch_render_seams(monkeypatch)

    report = allocation.run()

    assert report.startswith("# ETF 周度观察")
    assert "无调仓需求，保持当前持仓。" in report
    assert "## 策略参考" in report
    assert "近 5 年 **40%** 分位" in report
    assert "## 市场宏观（未完善）" in report
    assert "建议调仓指令" not in report and "新钱投放" not in report  # 精简版式：指令只进表格
    assert "免责声明" in report


def test_run_with_orders_renders_orders_only_in_recommendation(monkeypatch):
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: _alloc())
    _patch_render_seams(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("报告生成不应触达 execute_batch")

    monkeypatch.setattr("src.trade_ledger.execute_batch", _boom)
    report = allocation.run()
    assert "SELL 沪深300ETF(510300) 约 2000份" in report
    assert report.count("SELL 沪深300ETF(510300)") == 1  # 指令只在本次建议出现，表格只看建议列


def test_ledger_unreadable_degrades_report(monkeypatch):
    def _boom():
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(allocation, "_raw_snapshot", _boom)
    _patch_render_seams(monkeypatch)

    report = allocation.run()
    assert "成交台账不可用，跳过持仓对照" in report
    assert "持仓数据不可用，暂不生成建议。" in report


# ── WeekContext 装配：单点取数 + 分层失败口径 ──

def test_week_context_assembles_shared_facts_once(monkeypatch):
    calls = []

    def _pe():
        calls.append("pe")
        return {"pe": 14.5, "pe_pct": 40.0}

    monkeypatch.setattr(allocation, "_raw_market_pe", _pe)
    monkeypatch.setattr(allocation, "_raw_treasury_yield_y10", lambda: 2.0)
    monkeypatch.setattr(allocation, "_raw_erp_percentile", lambda: {"erp_pct": 55.0})
    monkeypatch.setattr(allocation, "_raw_valuation_rows", lambda etfs: [])
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: {"ok": 1})

    def _no_bars(code, end):
        raise RuntimeError("行情不可用")

    monkeypatch.setattr(allocation, "_raw_trend_bars_ad", lambda codes, end: {})
    monkeypatch.setattr(allocation, "_raw_trend_bars", _no_bars)

    ctx = allocation._build_week_context()
    assert ctx.market_pe == {"pe": 14.5, "pe_pct": 40.0}
    assert ctx.y10 == 2.0 and ctx.erp_pct == 55.0
    assert ctx.alloc == {"ok": 1}
    assert calls == ["pe"]  # 各节共用同一估值口径，单点取数
    assert all(v == {"state": None, "error": "行情不可用"} for v in ctx.trends.values())


def test_erp_failure_tolerated_but_market_pe_failure_propagates(monkeypatch):
    def _boom():
        raise RuntimeError("down")

    # ERP 可缺（展示层独立参照，失败置 None）；市场级 PE 不兜底（fail-closed，同数据暂停口径）
    monkeypatch.setattr(allocation, "_raw_erp_percentile", _boom)
    monkeypatch.setattr(allocation, "_raw_market_pe", lambda: {"pe": 14.5, "pe_pct": 40.0})
    monkeypatch.setattr(allocation, "_raw_treasury_yield_y10", lambda: 2.0)
    monkeypatch.setattr(allocation, "_raw_valuation_rows", lambda etfs: [])
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: None)

    def _no_bars(code, end):
        raise RuntimeError("down")

    monkeypatch.setattr(allocation, "_raw_trend_bars_ad", lambda codes, end: {})
    monkeypatch.setattr(allocation, "_raw_trend_bars", _no_bars)
    ctx = allocation._build_week_context()
    assert ctx.erp_pct is None

    monkeypatch.setattr(allocation, "_raw_market_pe", _boom)
    with pytest.raises(RuntimeError):
        allocation._build_week_context()


# ── _fetch_missing_prices：日线优先 + 失败收拢一批实时兜底 ──

def test_missing_prices_daily_first_realtime_only_for_missed(monkeypatch):
    def _daily(code):
        return pd.DataFrame({"close": [4.95]}) if code == "510300" else None

    monkeypatch.setattr(allocation, "_raw_daily_close", _daily)
    seen = []

    def _fake_rt(codes):
        seen.append(list(codes))
        return {"510500": 6.1}, {}

    monkeypatch.setattr(allocation, "_raw_realtime_quotes", _fake_rt)
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
        return {"510300": 4.9}, {}

    monkeypatch.setattr(allocation, "_raw_realtime_quotes", _fake_rt)
    prices = allocation._fetch_missing_prices([SimpleNamespace(code="510300")], [])

    assert prices == {"510300": 4.9}
    assert seen == [["510300"]]  # 全部日线失败 → 一批实时兜底
