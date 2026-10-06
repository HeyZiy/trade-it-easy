# -*- coding: utf-8 -*-
"""质量池编排层测试 — 调度推进、计划冻结、影子记账与退出重试（quality_pool.py）。"""

import datetime as dt

import pandas as pd
import pytest

import quality_pool as qp
import src.quality_pool.config as pool_config
from src.quality_pool import state as pool_state
from src.quality_pool.execution import ExecSnapshot


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的状态/台账文件 + 固定交易日历（2026-10 起的连续工作日）。"""
    state_path = tmp_path / "quality_pool_state.json"
    ledger_path = tmp_path / "pool_ledger.jsonl"
    monkeypatch.setattr(qp, "STATE_PATH", str(state_path))
    monkeypatch.setattr(qp, "POOL_LEDGER_PATH", str(ledger_path))
    # assembly 经 config 运行时读台账路径，此处单点替换
    monkeypatch.setattr(pool_config, "POOL_LEDGER_PATH", str(ledger_path))
    monkeypatch.setattr(pool_config, "STATE_PATH", str(state_path))
    monkeypatch.setattr(qp, "save_report", lambda report, prefix: str(tmp_path / "r.md"))
    monkeypatch.setattr(qp, "notify", lambda report: True)

    cal = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(150)]
    cal = [d for d in cal if d.weekday() < 5]
    monkeypatch.setattr(qp, "is_trading_day", lambda day=None: day in cal)
    # qp 顶部 from-import 已把日历名拷进自身命名空间，须两处同补
    monkeypatch.setattr(qp, "latest_trading_day_on_or_before",
                        lambda d, **kw: max((x for x in cal if x <= d), default=None))
    import src.trading_calendar as tc
    monkeypatch.setattr(tc, "get_trading_dates",
                        lambda start, end: [d for d in cal if start <= d <= end])
    monkeypatch.setattr(tc, "latest_trading_day_on_or_before",
                        lambda d, **kw: max((x for x in cal if x <= d), default=None))
    return monkeypatch


def _patch_signal_data(monkeypatch, universe={"600001", "600002"},
                       closes=None, fail=False):
    import src.quality_pool.feeds as feeds
    if fail:
        def boom(*a, **kw):
            raise RuntimeError("data down")
        monkeypatch.setattr(feeds, "fetch_universe", boom)
        return
    n = 130
    idx = pd.date_range(end="2026-09-30", periods=n, freq="B")
    closes = pd.DataFrame({c: 10.0 for c in universe}, index=idx)
    monkeypatch.setattr(feeds, "fetch_universe", lambda d: universe)
    monkeypatch.setattr(feeds, "fetch_closes", lambda codes, asof, rows=121: closes)
    monkeypatch.setattr(feeds, "raw_closes_at", lambda codes, asof: {c: 10.0 for c in codes})
    monkeypatch.setattr(feeds, "fetch_fundamentals",
                        lambda codes, asof, raw_closes=None: pd.DataFrame.from_dict(
                            {c: dict(roe_single_pct=4.0, np_yoy_pct=20.0,
                                     pe_ttm=15.0, pb=2.0) for c in codes},
                            orient="index"))
    monkeypatch.setattr(feeds, "fetch_status",
                        lambda codes, date: pd.DataFrame(
                            {"is_st": False, "is_suspended": False},
                            index=pd.Index(list(codes), name="code")))
    monkeypatch.setattr(feeds, "fetch_unlock_codes", lambda codes, d: set())


def _patch_execute_data(monkeypatch, prices, names=None,
                        suspended=(), st=()):
    """execute 侧 feeds 假数据：真实 assembly.build_snapshot 被测，不在 qp 上打洞。"""
    import src.quality_pool.feeds as feeds
    codes = sorted(prices)
    names = names or {c: f"股{c}" for c in codes}
    status = pd.DataFrame(
        {"is_st": [c in st for c in codes],
         "is_suspended": [c in suspended for c in codes],
         "high_limit": [prices.get(c, 10.0) * 1.1 for c in codes],
         "low_limit": [prices.get(c, 10.0) * 0.9 for c in codes]},
        index=pd.Index(codes, name="code"))
    monkeypatch.setattr(feeds, "fetch_realtime_quotes",
                        lambda cs: ({c: prices[c] for c in cs if c in prices},
                                    {c: names.get(c, f"股{c}") for c in cs}))
    monkeypatch.setattr(feeds, "raw_closes_at",
                        lambda cs, asof: {c: prices.get(c, 10.0) for c in cs})
    monkeypatch.setattr(feeds, "fetch_status",
                        lambda cs, d: status.loc[[c for c in cs if c in status.index]])


# ── state ──

def test_state_roundtrip_and_defaults(tmp_path):
    p = tmp_path / "s.json"
    assert pool_state.load_state(p)["pending_plan"] is None
    st = pool_state.load_state(p)
    pool_state.put_plan(st, {"signal_day": "2026-10-01"})
    st["exit_queue"] = {"600001": "out_of_pool"}
    pool_state.save_state(st, p)
    reloaded = pool_state.load_state(p)
    assert reloaded["pending_plan"]["signal_day"] == "2026-10-01"
    assert reloaded["exit_queue"] == {"600001": "out_of_pool"}


# ── signal ──

def test_signal_non_round_day_noop(env):
    today = dt.date(2026, 10, 5)
    st = pool_state.load_state(qp.STATE_PATH)
    st["next_signal_date"] = "2026-10-20"
    pool_state.save_state(st, qp.STATE_PATH)
    _patch_signal_data(env)
    report = qp.run_signal(today)
    assert "非调仓日" in report
    assert pool_state.load_state(qp.STATE_PATH)["pending_plan"] is None


def test_signal_first_run_writes_plan_and_advances(env):
    _patch_signal_data(env)
    today = dt.date(2026, 10, 1)
    report = qp.run_signal(today)
    st = pool_state.load_state(qp.STATE_PATH)
    assert st["pending_plan"]["selected"] == ["600001", "600002"]
    # 下一调仓信号日 = 第 20 个交易日后
    assert st["next_signal_date"] == "2026-10-29"
    assert "目标 2" in report


def test_signal_data_pause_keeps_holding_and_advances(env):
    _patch_signal_data(env, fail=True)
    today = dt.date(2026, 10, 1)
    report = qp.run_signal(today)
    st = pool_state.load_state(qp.STATE_PATH)
    assert st["pending_plan"] is None
    assert st["next_signal_date"] == "2026-10-29"
    assert "数据异常" in report


# ── execute ──

def test_execute_round_buys_and_records(env):
    _patch_signal_data(env)
    qp.run_signal(dt.date(2026, 10, 1))

    _patch_execute_data(env, {"600001": 10.0, "600002": 20.0})
    qp.run_execute(dt.date(2026, 10, 2))

    from src.trade_ledger import load_trades
    trades = load_trades(qp.POOL_LEDGER_PATH)
    assert {t.code for t in trades} == {"600001", "600002"}
    buys = {t.code: t.qty for t in trades if t.side == "buy"}
    assert buys["600001"] == 5000          # 100万/2/10 → 5000 股
    assert buys["600002"] == 2500          # 100万/2/20 → 2500 股
    st = pool_state.load_state(qp.STATE_PATH)
    assert st["pending_plan"] is None and st["exit_queue"] == {}


def test_execute_retry_exit_queue_daily(env):
    # 先建仓（台账落账）→ 落退出队列：停牌日受阻 → 复牌日卖出完成
    _patch_signal_data(env)
    qp.run_signal(dt.date(2026, 10, 1))
    _patch_execute_data(env, {"600001": 10.0, "600002": 20.0})
    qp.run_execute(dt.date(2026, 10, 2))

    st = pool_state.load_state(qp.STATE_PATH)
    st["exit_queue"] = {"600001": "out_of_pool"}
    pool_state.save_state(st, qp.STATE_PATH)

    # 停牌日：受阻 → 队列留存；复牌日：卖出完成 → 队列清空
    _patch_execute_data(env, {"600001": 10.0}, suspended={"600001"})
    qp.run_execute(dt.date(2026, 10, 5))
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == \
        {"600001": "out_of_pool"}

    _patch_execute_data(env, {"600001": 10.0})
    qp.run_execute(dt.date(2026, 10, 6))
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}

    from src.trade_ledger import load_trades
    sells = [t for t in load_trades(qp.POOL_LEDGER_PATH)
             if t.side == "sell" and t.code == "600001"]
    assert len(sells) == 1 and sells[0].qty == 5000


def test_execute_stale_plan_discarded(env):
    _patch_signal_data(env)
    qp.run_signal(dt.date(2026, 10, 1))
    # 跳过 10-02，直接 10-05 执行：计划已陈旧应丢弃、零记账
    _patch_execute_data(env, {})
    report = qp.run_execute(dt.date(2026, 10, 5))
    st = pool_state.load_state(qp.STATE_PATH)
    assert st["pending_plan"] is None
    from src.trade_ledger import load_trades
    assert load_trades(qp.POOL_LEDGER_PATH) == []


def _seed_positions(positions):
    from src.trade_ledger import TradeRecord, append_trade
    for code, qty, price in positions:
        append_trade(TradeRecord(date="2026-10-01", code=code, name=code,
                                 side="buy", qty=qty, price=price,
                                 account=pool_config.ACCOUNT), qp.POOL_LEDGER_PATH)


def _pending(selected, exits, queue=None):
    pool_state.save_state({
        "pending_plan": {"signal_day": "2026-10-01", "selected": selected,
                         "exit_reasons": exits},
        "exit_queue": queue or {}, "next_signal_date": "2026-10-29",
    }, qp.STATE_PATH)


def _fail_sell(monkeypatch, code):
    import src.trade_ledger.batch as batch
    original = batch.append_trade

    def append(record, path):
        if record.side == "sell" and record.code == code:
            raise OSError("simulated sell failure")
        return original(record, path=path)

    monkeypatch.setattr(batch, "append_trade", append)


def _ledger_snapshot(day="2026-10-02"):
    from src.trade_ledger import derive, load_trades
    return derive(load_trades(qp.POOL_LEDGER_PATH), day)


def test_failed_exit_keeps_actual_remainder_and_retries(env):
    _seed_positions([("600001", 100, 10.0), ("600002", 100, 10.0)])
    _pending([], {"600001": "out_of_pool", "600002": "rank_below_buffer"})
    _patch_execute_data(env, {"600001": 10.0, "600002": 10.0})
    with env.context() as faults:
        _fail_sell(faults, "600001")
        report = qp.run_execute(dt.date(2026, 10, 2))

    st = pool_state.load_state(qp.STATE_PATH)
    assert "simulated sell failure" in report
    assert st["exit_queue"] == {"600001": "out_of_pool"}
    assert st["pending_plan"] is None
    assert st["next_signal_date"] == "2026-10-29"
    assert _ledger_snapshot().held_counts() == {"600001": 100}
    qp.run_execute(dt.date(2026, 10, 5))
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}
    assert _ledger_snapshot("2026-10-05").held_counts() == {}


def test_failed_sale_cannot_fund_original_buy_quantity(env):
    _seed_positions([("600001", 99000, 10.0)])  # 剩余现金 10000
    _pending(["600002"], {"600001": "out_of_pool"})
    _patch_execute_data(env, {"600001": 10.0, "600002": 10.0})
    _fail_sell(env, "600001")
    report = qp.run_execute(dt.date(2026, 10, 2))
    snap = _ledger_snapshot()
    assert snap.held_counts() == {"600001": 99000, "600002": 900}
    assert snap.cash == 1000.0
    assert "900股" in report
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {"600001": "out_of_pool"}


def test_failed_exit_does_not_release_holding_slot(env):
    codes = [f"{600001 + i}" for i in range(20)]
    _seed_positions([(c, 100, 1.0) for c in codes])
    _pending(codes[1:] + ["600099"], {codes[0]: "out_of_pool"})
    _patch_execute_data(env, {c: 1.0 for c in codes + ["600099"]})
    _fail_sell(env, codes[0])
    report = qp.run_execute(dt.date(2026, 10, 2))
    snap = _ledger_snapshot()
    assert set(snap.held_counts()) == set(codes)
    assert snap.cash >= 0
    assert "600099 受阻（equal_weight）：slot" in report


def test_failed_buy_is_consumed_and_not_retried_next_day(env):
    import src.trade_ledger.batch as batch
    _pending(["600001"], {})
    _patch_execute_data(env, {"600001": 10.0})

    def fail_buy(*args, **kwargs):
        raise OSError("simulated buy failure")

    with env.context() as faults:
        faults.setattr(batch, "append_trade", fail_buy)
        report = qp.run_execute(dt.date(2026, 10, 2))
    assert "simulated buy failure" in report
    assert pool_state.load_state(qp.STATE_PATH)["pending_plan"] is None
    qp.run_execute(dt.date(2026, 10, 5))
    assert _ledger_snapshot("2026-10-05").held_counts() == {}


def test_failed_equal_weight_reduction_is_not_retried_daily(env):
    _seed_positions([("600001", 10000, 10.0)])
    _pending(["600001"], {})
    _patch_execute_data(env, {"600001": 10.0})
    with env.context() as faults:
        _fail_sell(faults, "600001")
        qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts() == {"600001": 10000}
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}
    qp.run_execute(dt.date(2026, 10, 5))
    assert _ledger_snapshot("2026-10-05").held_counts() == {"600001": 10000}


def test_new_selection_cancels_existing_exit(env):
    _seed_positions([("600001", 5000, 10.0)])
    _pending(["600001"], {}, queue={"600001": "out_of_pool"})
    _patch_execute_data(env, {"600001": 10.0})
    qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts() == {"600001": 5000}
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}


def test_safety_abort_preserves_exits_and_stops_buys(env):
    _seed_positions([("600001", 1000, 10.0), ("600002", 1000, 10.0)])
    _pending(["600003"], {"600001": "out_of_pool", "600002": "out_of_pool"})
    _patch_execute_data(env, {c: 10.0 for c in ["600001", "600002", "600003"]})
    original = qp.assembly.build_snapshot

    def changed_after_snapshot(*args):
        from src.trade_ledger import TradeRecord, append_trade
        snap = original(*args)
        append_trade(TradeRecord("2026-10-02", "600001", "600001", "sell",
                                 100, 10.0, account=pool_config.ACCOUNT), qp.POOL_LEDGER_PATH)
        return snap

    env.setattr(qp.assembly, "build_snapshot", changed_after_snapshot)
    report = qp.run_execute(dt.date(2026, 10, 2))
    assert "批次安全校验未过" in report
    assert _ledger_snapshot().held_counts() == {"600001": 900, "600002": 1000}
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {
        "600001": "out_of_pool", "600002": "out_of_pool"}


@pytest.mark.parametrize("fail_on", [1, 2])
def test_state_save_failure_does_not_replay_buys(env, fail_on):
    _seed_positions([("600001", 100, 10.0)])
    _pending(["600002"], {"600001": "out_of_pool"})
    _patch_execute_data(env, {"600001": 10.0, "600002": 10.0})
    original = pool_state.save_state
    calls = 0

    def fail_save(st, path):
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise OSError("simulated state failure")
        return original(st, path)

    with env.context() as faults:
        faults.setattr(pool_state, "save_state", fail_save)
        with pytest.raises(OSError, match="simulated state failure"):
            qp.run_execute(dt.date(2026, 10, 2))
    st = pool_state.load_state(qp.STATE_PATH)
    if fail_on == 1:
        assert _ledger_snapshot().held_counts() == {"600001": 100}
        assert st["pending_plan"] is not None
    else:
        assert st["pending_plan"] is None
        assert st["exit_queue"] == {"600001": "out_of_pool"}
        before = _ledger_snapshot().held_counts()
        qp.run_execute(dt.date(2026, 10, 2))
        assert _ledger_snapshot().held_counts() == before
        assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}


def test_dry_run_leaves_state_and_ledger_unchanged(env):
    from pathlib import Path
    _seed_positions([("600001", 100, 10.0)])
    _pending(["600002"], {"600001": "out_of_pool"})
    _patch_execute_data(env, {"600001": 10.0, "600002": 10.0})
    state_before = Path(qp.STATE_PATH).read_bytes()
    ledger_before = Path(qp.POOL_LEDGER_PATH).read_bytes()
    assert "dry-run" in qp.run_execute(dt.date(2026, 10, 2), dry_run=True)
    assert Path(qp.STATE_PATH).read_bytes() == state_before
    assert Path(qp.POOL_LEDGER_PATH).read_bytes() == ledger_before


def test_interrupted_selling_recovers_exits_without_replaying_round(env):
    import src.trade_ledger.batch as batch
    _seed_positions([("600001", 100, 10.0), ("600002", 100, 10.0)])
    _pending(["600003"], {"600001": "out_of_pool", "600002": "out_of_pool"})
    _patch_execute_data(env, {c: 10.0 for c in ["600001", "600002", "600003"]})
    original = batch.append_trade

    def interrupt(record, path):
        if record.code == "600002":
            raise RuntimeError("simulated interruption")
        return original(record, path=path)

    with env.context() as faults:
        faults.setattr(batch, "append_trade", interrupt)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts() == {"600002": 100}
    st = pool_state.load_state(qp.STATE_PATH)
    assert st["pending_plan"] is None
    assert set(st["exit_queue"]) == {"600001", "600002"}
    qp.run_execute(dt.date(2026, 10, 2))
    assert _ledger_snapshot().held_counts() == {}
    assert pool_state.load_state(qp.STATE_PATH)["exit_queue"] == {}


def test_atomic_state_replace_failure_preserves_old_file(env):
    from pathlib import Path
    _pending(["600001"], {})
    before = Path(qp.STATE_PATH).read_bytes()

    def fail_replace(*args):
        raise OSError("simulated replace failure")

    env.setattr(pool_state.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        pool_state.save_state({"exit_queue": {"600001": "out_of_pool"}}, qp.STATE_PATH)
    assert Path(qp.STATE_PATH).read_bytes() == before
    assert not list(Path(qp.STATE_PATH).parent.glob("*.tmp"))
