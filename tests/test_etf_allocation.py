# -*- coding: utf-8 -*-
"""周度批次闭环（src/etf/allocation）：单动词 run 的装配/渲染/记账口径。
全部假数据（*_raw seam 注入），不访问网络或真实台账。"""
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from src.etf import allocation
from src.etf.rebalancer import RebalanceOrder, RebalancePlan


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
        "rotation_mv": 0.0,
        "rotation_positions": [],
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
    monkeypatch.setattr(allocation, "render_summary", lambda *a, **kw: "**账户速览**（测试桩）")


# ── _execute_batch：dry-run 是唯一的记账闸门 ──

def test_dry_run_writes_no_ledger(tmp_path):
    ledger = tmp_path / "trade_ledger.jsonl"
    out = allocation._execute_batch(_alloc(), ledger_path=ledger, dry_run=True)

    assert "[DRY] SELL 沪深300ETF(510300) 2000股" in out
    assert "dry-run：以下指令未记账" in out
    assert not ledger.exists()


def test_non_dry_calls_execute_batch(monkeypatch):
    calls = []

    class _Res:
        abort = None
        outcomes = ()

    def _fake(*a, **kw):
        calls.append(kw)
        return _Res()

    monkeypatch.setattr("src.trade_ledger.execute_batch", _fake)
    allocation._execute_batch(_alloc(), ledger_path="unused.jsonl")

    assert len(calls) == 1 and calls[0]["account"] == "core"


def test_dry_run_short_circuits_before_execute(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("dry-run 不应触达 execute_batch")

    monkeypatch.setattr("src.trade_ledger.execute_batch", _boom)
    out = allocation._execute_batch(_alloc(), dry_run=True)
    assert "[DRY]" in out


# ── run：单动词闭环（假数据装配）──

def test_run_renders_full_report_without_orders(monkeypatch):
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: _alloc(orders=[]))
    _patch_render_seams(monkeypatch)

    report = allocation.run(dry_run=True)

    assert report.startswith("# ETF 周度观察")
    assert "（dry-run，未记账）" in report
    assert "无调仓指令，保持当前持仓。" in report
    assert "## 执行前持仓对照与调仓依据" in report
    assert "**PE 分位**: 40%" in report
    assert "近 5 年 **40%** 分位" in report          # 市场概览与新钱参考共用同一口径
    assert "## 各标的估值参考" in report
    assert "## 新钱投放参考" in report
    assert "免责声明" in report


def test_run_with_orders_dry_run_renders_dry_batch(monkeypatch):
    monkeypatch.setattr(allocation, "_compute_allocation", lambda: _alloc())
    _patch_render_seams(monkeypatch)

    def _boom(*a, **kw):
        raise AssertionError("dry-run 不应触达 execute_batch")

    monkeypatch.setattr("src.trade_ledger.execute_batch", _boom)
    report = allocation.run(dry_run=True)
    assert "[DRY] SELL 沪深300ETF(510300) 2000股" in report


def test_ledger_unreadable_degrades_report(monkeypatch):
    def _boom():
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(allocation, "_raw_snapshot", _boom)
    _patch_render_seams(monkeypatch)

    report = allocation.run(dry_run=True)
    assert "成交台账不可用，跳过持仓对照" in report
    assert "持仓数据不可用，本次未执行。" in report


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
