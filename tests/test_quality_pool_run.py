"""单次模拟的真实信号→执行链：一次报告、只读预览与失败恢复。"""

from datetime import date
from pathlib import Path

import pytest

import quality_pool as entry
from src.quality_pool import config, state
from src.trade_ledger.ledger import load_trades
from test_quality_pool_state import env, _patch_signal_data, _patch_execute_data, _seed_positions


def test_run_consumes_new_signal_once_and_sends_one_report(env):
    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 12.0, "600002": 20.0})
    reports, notifications = [], []
    env.setattr(entry, "save_report", lambda report, prefix, **kw: reports.append((prefix, report)))
    env.setattr(entry, "notify", notifications.append)

    report = entry.run(date(2026, 10, 1))

    trades = load_trades(config.POOL_LEDGER_PATH)
    assert {(t.code, t.price) for t in trades} == {("600001", 12.0), ("600002", 20.0)}
    assert all(t.side == "buy" for t in trades) and len(trades) == 2
    assert "目标 2" in report and "已记账 2 笔" in report
    assert reports == [("quality_pool", report)] and notifications == [report]
    saved = state.load_state(config.STATE_PATH)
    assert saved["pending_plan"] is None and saved["next_signal_date"] == "2026-10-29"

    before = Path(config.POOL_LEDGER_PATH).read_bytes()
    assert "不重复本轮" in entry.run(date(2026, 10, 1))
    assert Path(config.POOL_LEDGER_PATH).read_bytes() == before


@pytest.mark.parametrize("existing_files", [False, True])
def test_run_dry_run_previews_new_plan_without_state_or_ledger_writes(env, existing_files):
    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 12.0, "600002": 20.0})
    if existing_files:
        _seed_positions([("600001", 100, 10.0)])
        state.save_state(state.load_state(config.STATE_PATH), config.STATE_PATH)
    paths = [Path(config.STATE_PATH), Path(config.POOL_LEDGER_PATH)]
    before = [p.read_bytes() if p.exists() else None for p in paths]

    report = entry.run(date(2026, 10, 2), dry_run=True)

    assert "目标 2" in report and "（dry-run）BUY" in report
    assert "600001" in report and "600002" in report
    assert [p.read_bytes() if p.exists() else None for p in paths] == before


def test_failed_signal_still_retries_exits_then_recovers_original_cycle(env):
    _seed_positions([("600001", 100, 10.0)])
    saved = state.load_state(config.STATE_PATH)
    saved["exit_queue"] = {"600001": "out_of_pool"}
    state.save_state(saved, config.STATE_PATH)
    _patch_signal_data(env, fail=True)
    _patch_execute_data(env, {"600001": 10.0, "600002": 20.0})

    report = entry.run(date(2026, 10, 2))

    assert "数据异常" in report
    assert load_trades(config.POOL_LEDGER_PATH)[-1].side == "sell"
    saved = state.load_state(config.STATE_PATH)
    assert saved["signal_retry"] and saved["exit_queue"] == {}
    assert saved["next_signal_date"] == "2026-10-30"

    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 10.0, "600002": 20.0})
    assert "目标 2" in entry.run(date(2026, 10, 5))
    saved = state.load_state(config.STATE_PATH)
    assert not saved["signal_retry"] and saved["next_signal_date"] == "2026-10-30"


def test_non_round_day_reviews_unknown_holding_and_exits_in_same_run(env):
    from src.quality_pool import feeds

    _seed_positions([("600001", 100, 10.0)])
    saved = state.load_state(config.STATE_PATH)
    saved.update(next_signal_date="2026-10-30", deferred_codes=["600001"])
    state.save_state(saved, config.STATE_PATH)
    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 10.0})
    fundamentals = feeds.fetch_fundamentals

    def failed_quality(*args, **kwargs):
        frame = fundamentals(*args, **kwargs)
        frame.loc["600001", "roe_ttm_pct"] = 0.0
        return frame

    env.setattr(feeds, "fetch_fundamentals", failed_quality)
    report = entry.run(date(2026, 10, 2))

    assert "已检查 1 只" in report
    assert [t.side for t in load_trades(config.POOL_LEDGER_PATH)] == ["buy", "sell"]
    saved = state.load_state(config.STATE_PATH)
    assert saved["deferred_codes"] == [] and saved["exit_queue"] == {}
    assert saved["next_signal_date"] == "2026-10-30"
