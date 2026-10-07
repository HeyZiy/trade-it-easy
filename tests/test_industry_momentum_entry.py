# -*- coding: utf-8 -*-
"""入口脚本 industry_momentum 的只读降级：非交易日 --force 一律不记账（同 etf_observe 口径）。"""
import io
import sys

import industry_momentum as entry


def _stub_main(monkeypatch, trading, argv):
    seen = {}
    monkeypatch.setattr(entry, "is_trading_day", lambda day=None: trading)
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
