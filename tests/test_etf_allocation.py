# -*- coding: utf-8 -*-
"""周度批次的完整报告：真实临时账户与配置，外部行情使用假数据。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src import etf_account as wallet, portfolio_config
from src.etf import allocation, amazing_factors as factors


@pytest.fixture
def account(tmp_path, monkeypatch):
    path = tmp_path / "weekly.json"
    monkeypatch.setattr(wallet, "ACCOUNT_PATH", path)
    monkeypatch.setattr(wallet, "_today", lambda: date(2025, 1, 7))
    wallet.initialize(
        path, asof="2025-01-07", cash=5000,
        positions=[dict(code="515180", count=2000, price=5, strategy="core")],
        targets={"515180": .5}, budgets={"core": 1}, cash_allocations={"core": 5000},
    )
    current = portfolio_config.data(path)
    config = deepcopy(current["config"])
    config["core"][0]["tracked_index"] = "000913"
    portfolio_config.update(path, {"config": config, "revision": current["revision"]})
    return path


@pytest.fixture(autouse=True)
def quotes(monkeypatch):
    monkeypatch.setattr(allocation, "_raw_market_pe", lambda: {"pe": 14.5, "pe_pct": 40.0})
    monkeypatch.setattr(allocation, "_raw_treasury_yield_y10", lambda: 2.0)
    monkeypatch.setattr(allocation, "_raw_erp_percentile", lambda: {"erp_pct": 55.0})
    monkeypatch.setattr(allocation, "_raw_trend_bars_ad", lambda codes, end: {})

    def no_bars(code, end):
        raise RuntimeError("行情不可用")

    monkeypatch.setattr(allocation, "_raw_trend_bars", no_bars)
    monkeypatch.setattr(allocation, "_raw_daily_close", lambda code: pd.DataFrame({"close": [5.0]}))
    monkeypatch.setattr(allocation, "_raw_realtime_quotes", lambda codes: ({}, {}))
    monkeypatch.setattr(factors, "_fetch_csindex_indicator", lambda code: pd.DataFrame([{
        "index_code": code, "index_name": f"指数{code}", "date": "2025-01-07", "pe": 8., "dy": 5.,
    }]))
    factors._csindex_cache.clear()
    yield
    factors._csindex_cache.clear()


def test_report_reads_inputs_once_and_never_records_fills(account, monkeypatch):
    config_path = portfolio_config.path_for(account)
    before = account.read_bytes(), config_path.read_bytes()
    reads = {account: 0, config_path: 0}
    read_text = Path.read_text

    def counted_read(path, *args, **kwargs):
        if path in reads:
            reads[path] += 1
        return read_text(path, *args, **kwargs)

    def unexpected_trade(*args, **kwargs):
        raise AssertionError("报告生成不应触达 execute_batch")

    monkeypatch.setattr(Path, "read_text", counted_read)
    monkeypatch.setattr("src.trade_ledger.execute_batch", unexpected_trade)
    report = allocation.run()

    assert report.startswith("# 多资产配置周度观察")
    assert "515180(515180) 约 500份" in report
    assert report.count("SELL 515180(515180)") == 1
    assert "| 515180（515180） | 50.0% | 66.7% | -16.7% |" in report
    assert "指数000913 / PE 8.0 / 股息 5.0% / 利差 +3.0pt（偏低）" in report
    assert "近 5 年 **40%** 分位" in report and "免责声明" in report
    assert "建议调仓指令" not in report and "新钱投放" not in report
    assert reads == {account: 1, config_path: 1}
    assert (account.read_bytes(), config_path.read_bytes()) == before


def test_run_renders_full_report_without_orders(account):
    current = portfolio_config.data(account)
    current["config"]["core"][0]["weight"] = 2 / 3
    current["config"]["core"][1]["weight"] = 1 / 3
    portfolio_config.update(account, current)

    report = allocation.run()

    assert "无调仓需求，保持当前持仓。" in report
    assert "## 策略参考" in report and "## 市场宏观（未完善）" in report


def test_edits_and_fills_during_quotes_take_effect_on_next_report(account, monkeypatch):
    trend_codes, indices = [], []

    def trends(codes, end):
        trend_codes.append(codes)
        return {}

    fetch_index = factors._fetch_csindex_indicator
    monkeypatch.setattr(factors, "_fetch_csindex_indicator",
                        lambda code: indices.append(code) or fetch_index(code))
    monkeypatch.setattr(allocation, "_raw_trend_bars_ad", trends)

    def edit_and_sell():
        current = portfolio_config.data(account)
        row = current["config"]["core"][0]
        row.update(code="510300", weight=.2, tracked_index="000300", dividend_style=False)
        current["config"]["core"][1]["weight"] = .8
        current["config"]["budgets"]["core"] = .5
        portfolio_config.update(account, current)
        wallet.record_event(account, {
            "request_id": "partial-sell-1", "revision": wallet.revision(wallet.load(account)),
            "date": "2025-01-07", "side": "sell", "strategy": "core", "code": "515180",
            "qty": 1000, "price": 5, "fee": 0,
        })

    # 网页写入必须能在行情查询期间完成；若仍持有账户锁，此处会超时。
    with ThreadPoolExecutor(max_workers=1) as writer:
        def market_pe():
            writer.submit(edit_and_sell).result(timeout=5)
            return {"pe": 14.5, "pe_pct": 40.0}

        monkeypatch.setattr(allocation, "_raw_market_pe", market_pe)
        first = allocation.run()

    assert "SELL 515180(515180) 约 500份" in first
    assert "| 515180（515180） | 50.0% | 66.7% | -16.7% |" in first
    assert "510300" not in first
    assert "指数000913" in first and "利差 +3.0pt（偏低）" in first
    assert trend_codes == [["515180"]] and indices == ["000913"]

    monkeypatch.setattr(allocation, "_raw_market_pe", lambda: {"pe": 14.5, "pe_pct": 40.0})
    second = allocation.run()

    assert "BUY 510300(510300) 约 300份" in second
    assert "| 510300（510300） | 20.0% | 0.0% | +20.0% |" in second
    assert "基准外持仓：515180(515180)" in second
    assert "指数000300" in second and "利差 +3.0pt" not in second
    assert trend_codes == [["515180"], ["510300"]]
    assert indices == ["000913", "000300"]
    actual = wallet.snapshot(wallet.load(account), "2025-01-07")
    assert actual["positions"][0]["count"] == 1000 and actual["cash"] == 10000


def test_invalid_edit_during_run_does_not_affect_captured_config(account, monkeypatch):
    def corrupt_config():
        portfolio_config.path_for(account).write_text("{", encoding="utf-8")
        return {"pe": 14.5, "pe_pct": 40.0}

    monkeypatch.setattr(allocation, "_raw_market_pe", corrupt_config)
    report = allocation.run()
    assert "SELL 515180(515180) 约 500份" in report
    assert "指数000913" in report and "利差 +3.0pt" in report
    with pytest.raises(ValueError, match="无法读取账户配置"):
        allocation.run()


def test_account_writer_waits_for_input_read_then_finishes_before_quotes(account, monkeypatch):
    read_text = Path.read_text
    triggered = False
    pending = None

    def deposit():
        wallet.record_event(account, {
            "request_id": "concurrent-deposit", "revision": wallet.revision(wallet.load(account)),
            "date": "2025-01-07", "side": "deposit", "strategy": "core", "amount": 1000,
        })

    with ThreadPoolExecutor(max_workers=1) as writer:
        def read_while_writer_starts(path, *args, **kwargs):
            nonlocal triggered, pending
            if path == account and not triggered:
                triggered = True
                pending = writer.submit(deposit)
                # 已读取配置，账户文件仍未读取；网页记账此时必须等待。
                with pytest.raises(TimeoutError):
                    pending.result(timeout=.2)
            return read_text(path, *args, **kwargs)

        def market_pe():
            # 开始取行情前读取锁已经释放，网页记账可以完成。
            pending.result(timeout=5)
            return {"pe": 14.5, "pe_pct": 40.0}

        monkeypatch.setattr(Path, "read_text", read_while_writer_starts)
        monkeypatch.setattr(allocation, "_raw_market_pe", market_pe)
        report = allocation.run()

    assert "SELL 515180(515180) 约 500份" in report
    assert wallet.snapshot(wallet.load(account), "2025-01-07")["cash"] == 6000


@pytest.mark.parametrize("failure", ["missing", "malformed", "invalid"])
def test_invalid_starting_config_stops_before_market_queries(account, monkeypatch, failure):
    path = portfolio_config.path_for(account)
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("{" if failure == "malformed" else "{}", encoding="utf-8")

    def unexpected_query():
        raise AssertionError("配置无效时不应开始取行情")

    monkeypatch.setattr(allocation, "_raw_market_pe", unexpected_query)
    with pytest.raises(ValueError):
        allocation.run()


@pytest.mark.parametrize("failure", ["missing", "malformed", "invalid", "unassigned"])
def test_unavailable_account_preserves_macro_report(account, failure):
    if failure == "missing":
        account.unlink()
    elif failure in ("malformed", "invalid"):
        account.write_text("{" if failure == "malformed" else "{}", encoding="utf-8")
    else:
        current = portfolio_config.data(account)
        current["config"]["budgets"] = {}
        portfolio_config.update(account, current)
    before = account.read_bytes() if account.exists() else None

    report = allocation.run()

    assert "成交台账不可用，跳过持仓对照" in report
    assert "持仓数据不可用，暂不生成建议。" in report
    assert "近 5 年 **40%** 分位" in report
    assert "无调仓需求" not in report and "BUY " not in report and "SELL " not in report
    assert (account.read_bytes() if account.exists() else None) == before


def test_erp_failure_tolerated_but_market_pe_failure_propagates(account, monkeypatch):
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
