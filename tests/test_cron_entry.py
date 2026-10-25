# -*- coding: utf-8 -*-
"""cron 入口装具（src/cron_entry.py）直测：旗标件、交易日闸门、硬退出。

装具是四个 cron 入口「--force＋非交易日只读降级＋os._exit」政策的单点；
各入口的装配行为由各自测试钉住（如 test_quality_pool_entry.py）。
"""
import argparse

import src.cron_entry as cron_entry
from src.cron_entry import add_standard_flags, hard_exit, resolve_run_gate


class TestStandardFlags:
    def test_all_four_flags_parse_true(self):
        parser = argparse.ArgumentParser()
        add_standard_flags(parser)
        args = parser.parse_args(
            ["--debug", "--no-notify", "--dry-run", "--force"])
        assert (args.debug, args.no_notify, args.dry_run, args.force) == \
            (True, True, True, True)

    def test_defaults_all_false(self):
        parser = argparse.ArgumentParser()
        add_standard_flags(parser)
        args = parser.parse_args([])
        assert not (args.debug or args.no_notify or args.dry_run or args.force)


class TestResolveRunGate:
    def test_non_trading_without_force_skips(self):
        assert resolve_run_gate(force=False, trading_day=False) == (True, False)

    def test_non_trading_without_force_keeps_explicit_dry_run(self):
        assert resolve_run_gate(force=False, dry_run=True,
                                trading_day=False) == (True, True)

    def test_trading_day_runs_normally(self):
        assert resolve_run_gate(force=False, trading_day=True) == (False, False)
        assert resolve_run_gate(force=True, trading_day=True) == (False, False)

    def test_non_trading_force_derives_readonly(self):
        assert resolve_run_gate(force=True, trading_day=False) == (False, True)

    def test_readonly_derivation_composes_with_explicit_dry_run(self):
        assert resolve_run_gate(force=True, dry_run=True,
                                trading_day=False) == (False, True)

    def test_default_trading_day_from_calendar(self, monkeypatch):
        monkeypatch.setattr(cron_entry, "is_trading_day", lambda: False)
        assert resolve_run_gate(force=False) == (True, False)
        monkeypatch.setattr(cron_entry, "is_trading_day", lambda: True)
        assert resolve_run_gate(force=False) == (False, False)


class TestHardExit:
    def test_flushes_then_exits_with_code(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cron_entry.sys, "stdout",
                            type("S", (), {"flush": lambda s: calls.append("out")})())
        monkeypatch.setattr(cron_entry.sys, "stderr",
                            type("S", (), {"flush": lambda s: calls.append("err")})())
        monkeypatch.setattr(cron_entry.os, "_exit",
                            lambda code: calls.append(("exit", code)))
        hard_exit(3)
        assert calls == ["out", "err", ("exit", 3)]
