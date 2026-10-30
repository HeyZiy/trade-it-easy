# -*- coding: utf-8 -*-
"""入口脚本 industry_momentum 的只读降级：非交易日 --force 一律不记账（同 etf_observe 口径）。"""
import io
import sys

import industry_momentum as entry
from src import cron_entry


def _stub_main(monkeypatch, trading, argv):
    seen = {}
    # 交易日政策单点在 cron_entry.resolve_run_gate，桩打在其查找命名空间。
    monkeypatch.setattr(cron_entry, "is_trading_day", lambda day=None: trading)
    monkeypatch.setattr(entry, "run",
                        lambda dry_run=False: seen.update(dry_run=dry_run) or "报告")
    monkeypatch.setattr(entry, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(entry, "save_report", lambda *a, **kw: None)
    monkeypatch.setattr(entry, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["industry_momentum.py"] + argv)
    # main() 末尾用 TextIOWrapper(sys.stdout.buffer) 重绑 stdout：旧流被回收会连带关掉
    # 底层 BytesIO，挂在 seen 上保命，别踩到 pytest 的捕获流。
    seen["stdout"] = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", seen["stdout"])
    return seen


def test_holiday_force_forces_readonly(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=["--force"])
    assert entry.main() == 0
    assert seen["dry_run"] is True


def test_trading_day_force_keeps_ledger_write(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--force"])
    assert entry.main() == 0
    assert seen["dry_run"] is False


def test_trading_day_explicit_dry_run(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--dry-run"])
    assert entry.main() == 0
    assert seen["dry_run"] is True


def test_holiday_without_force_skips(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=[])
    assert entry.main() == 0
    assert "dry_run" not in seen


def test_execution_writes_only_industry_ledger(tmp_path, monkeypatch):
    from src.etf.momentum import EtfOrder
    from src.trade_ledger.ledger import load_trades

    path = tmp_path / "industry.jsonl"
    monkeypatch.setattr(entry.SIM_ACCOUNT, "ledger_path", str(path))
    order = EtfOrder(code="512500", name="行业ETF", action="buy",
                     shares=100, price=2.0, amount=200, reason="test")
    abort, _ = entry._execute([order], "2026-10-29")
    assert abort is None
    trades = load_trades(path)
    assert len(trades) == 1 and trades[0].account == "satellite"


def test_run_reads_only_industry_ledger(monkeypatch):
    from src.trade_ledger.ledger import TradeRecord

    paths = []
    monkeypatch.setattr(entry.SIM_ACCOUNT, "ledger_path", "industry-only.jsonl")

    def read(path):
        paths.append(path)
        return [TradeRecord("2026-10-01", "512500", "行业ETF", "buy", 100, 2, account="satellite")]

    monkeypatch.setattr(entry, "load_trades", read)
    monkeypatch.setattr(entry, "resolve_prices", lambda codes: {"512500": 2})
    monkeypatch.setattr(entry, "render_summary", lambda *args, **kwargs: "独立成绩")
    monkeypatch.setattr(entry, "analyze_rotation", lambda *args, **kwargs: {
        "rows": [], "sells": [], "buy_orders": [], "satellite_mv": 200,
        "diag": {}, "held_codes": {"512500"},
    })
    assert "独立成绩" in entry.run(dry_run=True)
    assert paths == ["industry-only.jsonl", "industry-only.jsonl"]
