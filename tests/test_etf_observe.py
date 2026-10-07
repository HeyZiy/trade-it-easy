# -*- coding: utf-8 -*-
"""etf_observe 只读试跑口径：dry-run 零台账写入，非交易日 --force 自动降级为只读。"""
import io
import sys

import etf_observe as eo
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
        "plan": plan,
    }


# ── _execute_batch：dry-run 是唯一的记账闸门 ──

def test_dry_run_writes_no_ledger(tmp_path):
    ledger = tmp_path / "trade_ledger.jsonl"
    out = eo._execute_batch(_alloc(), ledger_path=ledger, dry_run=True)

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
    eo._execute_batch(_alloc(), ledger_path="unused.jsonl")

    assert len(calls) == 1 and calls[0]["account"] == "core"


def test_dry_run_short_circuits_before_execute(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("dry-run 不应触达 execute_batch")

    monkeypatch.setattr("src.trade_ledger.execute_batch", _boom)
    out = eo._execute_batch(_alloc(), dry_run=True)
    assert "[DRY]" in out


# ── main：--force 开门、非交易日一律只读 ──

def _stub_main(monkeypatch, trading, argv):
    seen = {}

    def fake_report(dry_run=False):
        seen["dry_run"] = dry_run
        return "# 报告"

    monkeypatch.setattr(eo, "is_trading_day", lambda day=None: trading)
    monkeypatch.setattr(eo, "_generate_report", fake_report)
    monkeypatch.setattr(eo, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(eo, "save_report", lambda *a, **kw: None)
    monkeypatch.setattr(eo, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["etf_observe.py"] + argv)
    # main() 用 TextIOWrapper(sys.stdout.buffer) 重绑 stdout：旧流被回收时会连带关掉底层
    # BytesIO，所以把整条流挂在 seen 上保命，别踩到 pytest 的捕获流。
    seen["stdout"] = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", seen["stdout"])
    return seen


def test_holiday_force_forces_readonly(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=["--force"])
    assert eo.main() == 0
    assert seen["dry_run"] is True


def test_trading_day_force_keeps_ledger_write(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--force"])
    assert eo.main() == 0
    assert seen["dry_run"] is False


def test_trading_day_explicit_dry_run(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--dry-run"])
    assert eo.main() == 0
    assert seen["dry_run"] is True


def test_holiday_without_force_skips(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=[])
    assert eo.main() == 0
    assert "dry_run" not in seen          # 报告根本没生成，也没记账
