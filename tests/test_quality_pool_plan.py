# -*- coding: utf-8 -*-
"""SignalPlan（src/quality_pool/signal）直测：持久化名单的读回与 STALE 校验。

校验语义（版本/执行日/数据截止日）原居 assembly，现归名单类型所有；
轮次生成与装配行为仍由 test_quality_pool_state / test_quality_pool_v2 钉住。
"""
from datetime import date

from src.quality_pool import config
from src.quality_pool.signal import SignalPlan
from src.quality_pool.state import put_plan


def _raw(**over):
    base = {"signal_day": "2026-10-01", "execute_day": "2026-10-01",
            "asof": "2026-09-30", "selected": ["600001"],
            "deferred": [], "industries": {"600001": "银行"},
            "kept": ["600002"], "exit_reasons": {"600003": "out_of_pool"},
            "strategy_version": config.STRATEGY_VERSION}
    base.update(over)
    return base


def _prev_always(monkeypatch, prev="2026-09-30"):
    monkeypatch.setattr("src.trading_calendar.latest_trading_day_on_or_before",
                        lambda day: date.fromisoformat(prev))


def test_none_yields_none(monkeypatch):
    _prev_always(monkeypatch)
    assert SignalPlan.from_stored(None, "2026-10-01") is None


def test_fresh_plan_round_trips_via_put_plan(monkeypatch):
    _prev_always(monkeypatch)
    st = {}
    put_plan(st, _raw())                     # 与生产同一写入路径（盖版本戳）
    plan = SignalPlan.from_stored(st["pending_plan"], "2026-10-01")
    assert plan is not None
    assert plan.selected == ["600001"] and plan.kept == ["600002"]
    assert plan.industries == {"600001": "银行"}
    assert plan.exit_reasons == {"600003": "out_of_pool"}
    assert plan.asof == "2026-09-30"


def test_version_mismatch_drops(monkeypatch):
    _prev_always(monkeypatch)
    raw = _raw(strategy_version=config.STRATEGY_VERSION + 1)
    assert SignalPlan.from_stored(raw, "2026-10-01") is None


def test_execute_day_mismatch_drops(monkeypatch):
    _prev_always(monkeypatch)
    assert SignalPlan.from_stored(_raw(), "2026-10-02") is None


def test_asof_mismatch_drops(monkeypatch):
    _prev_always(monkeypatch, prev="2026-09-29")
    assert SignalPlan.from_stored(_raw(), "2026-10-01") is None
