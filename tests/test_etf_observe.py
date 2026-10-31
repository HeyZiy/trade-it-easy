# -*- coding: utf-8 -*-
"""入口 etf_observe 门禁口径：非交易日未 --force 跳过；报告生成从不写台账。
周度批次闭环本体在 src/etf/allocation，由 test_etf_allocation 覆盖。"""
import io
import sys

import etf_observe as eo
from src import cron_entry
from src.etf import allocation


def _stub_main(monkeypatch, trading, argv):
    seen = {}

    def fake_run():
        seen["called"] = True
        return "# 报告"

    # 交易日政策单点在 cron_entry.resolve_run_gate，桩打在其查找命名空间。
    monkeypatch.setattr(cron_entry, "is_trading_day", lambda day=None: trading)
    monkeypatch.setattr(allocation, "run", fake_run)
    monkeypatch.setattr(eo, "setup_logging", lambda **kw: None)
    monkeypatch.setattr(eo, "save_report", lambda *a, **kw: None)
    monkeypatch.setattr(eo, "notify", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["etf_observe.py"] + argv)
    # main() 用 TextIOWrapper(sys.stdout.buffer) 重绑 stdout：旧流被回收时会连带关掉底层
    # BytesIO，所以把整条流挂在 seen 上保命，别踩到 pytest 的捕获流。
    seen["stdout"] = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdout", seen["stdout"])
    return seen


def test_holiday_force_generates_report(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=["--force"])
    assert eo.main() == 0
    assert seen.get("called") is True


def test_trading_day_generates_report(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--force"])
    assert eo.main() == 0
    assert seen.get("called") is True


def test_trading_day_explicit_dry_run_flag_accepted(monkeypatch):
    seen = _stub_main(monkeypatch, trading=True, argv=["--dry-run"])
    assert eo.main() == 0
    assert seen.get("called") is True


def test_holiday_without_force_skips(monkeypatch):
    seen = _stub_main(monkeypatch, trading=False, argv=[])
    assert eo.main() == 0
    assert "called" not in seen          # 报告根本没生成
