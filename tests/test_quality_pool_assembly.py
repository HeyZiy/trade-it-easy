# -*- coding: utf-8 -*-
"""质量池装配层测试 — 09:31 快照合并点（真实 assembly，feeds 层假数据）。

钉死三条口径：
- ST/停牌以状态表官方标志为准（名称子串不参与判定）；
- 状态表缺行按停牌/ST 处置（fail-closed）；
- 状态表单次拉取，停牌/ST/涨跌停三消费共享；
- equity = 台账快照 total_assets（与手算 现金+Σ持仓×现价 恒等）。
"""

import datetime as dt

import pandas as pd
import pytest

import src.trading_calendar as tc
from src.quality_pool import assembly
from src.quality_pool import config as pool_config
from src.quality_pool import execution
from src.trade_ledger.batch import BatchOrder, execute_batch
from src.trade_ledger.ledger import load_trades


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离台账文件 + 固定日历（2026-09 起连续工作日）+ 单次状态表计数器。"""
    monkeypatch.setattr(pool_config, "POOL_LEDGER_PATH",
                        str(tmp_path / "pool_ledger.jsonl"))
    cal = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(150)]
    cal = [d for d in cal if d.weekday() < 5]
    monkeypatch.setattr(tc, "latest_trading_day_on_or_before",
                        lambda d, **kw: max((x for x in cal if x <= d), default=None))
    calls = {"fetch_status": 0}

    def _count_status(codes, date):
        calls["fetch_status"] += 1
        return pd.DataFrame(
            {"is_st": [c == "600002" for c in codes],
             "is_suspended": [False] * len(codes),
             "high_limit": [12.0 * 1.1 for _ in codes],
             "low_limit": [12.0 * 0.9 for _ in codes]},
            index=pd.Index(list(codes), name="code"))

    import src.quality_pool.feeds as feeds
    monkeypatch.setattr(feeds, "fetch_status", _count_status)
    monkeypatch.setattr(feeds, "fetch_realtime_quotes",
                        lambda cs: ({c: 12.0 for c in cs},
                                    {c: f"股{c}" for c in cs}))
    monkeypatch.setattr(feeds, "raw_closes_at",
                        lambda cs, asof: {c: 12.0 for c in cs})
    monkeypatch.calls = calls
    return monkeypatch


def _seed_holding(trade_date="2026-10-01", qty=5000, price=10.0):
    execute_batch([BatchOrder(code="600001", name="股600001", side="buy",
                              qty=qty, price=price, reason="entry")],
                  account=pool_config.ACCOUNT, held_counts={},
                  cash=1_000_000.0, trade_date=trade_date,
                  path=pool_config.POOL_LEDGER_PATH)


def test_snapshot_merges_ledger_quotes_status_once(env):
    """合并点全链：台账派生 + 实时价 + 状态表单次三消费。"""
    _seed_holding()
    snap = assembly.build_snapshot("2026-10-02", ["600002"])

    assert snap.counts == {"600001": 5000}
    assert snap.avail == {"600001": 5000}          # T+1 已结算
    assert snap.cash == 950_000.0
    assert snap.prices == {"600001": 12.0, "600002": 12.0}
    # equity = total_assets ≡ 现金 + Σ持仓×现价（台账口径单点，不重算）
    hand = snap.cash + sum(snap.counts[c] * snap.prices.get(c, 0.0)
                           for c in snap.counts)
    assert snap.equity == hand == 1_010_000.0
    # ST 以状态表为准：600002 无 "ST" 字样但官方标志为真
    assert snap.st == {"600001": False, "600002": True}
    assert not any(snap.suspended.values())
    assert snap.limits["600001"] == (12.0 * 1.1, 12.0 * 0.9)
    # 状态表只拉一次（停牌/ST/涨跌停共享）
    assert env.calls["fetch_status"] == 1


def test_snapshot_missing_status_row_fail_closed(env):
    """状态表缺行 → 按停牌/ST 处置，不当作可交易。"""
    import src.quality_pool.feeds as feeds
    env.setattr(feeds, "fetch_status",
                lambda codes, date: pd.DataFrame(
                    {"is_st": [False], "is_suspended": [False],
                     "high_limit": [13.2], "low_limit": [10.8]},
                    index=pd.Index(["600001"], name="code")))
    snap = assembly.build_snapshot("2026-10-02", ["600001", "600002"])
    assert snap.suspended["600002"] is True
    assert snap.st["600002"] is True
    assert snap.st["600001"] is False  # 在表中的码不受缺行影响


def test_record_trades_maps_intent_to_batch(env):
    """意图 → 台账批次；空计划零记账零派生。"""
    assert assembly.record_trades(execution.ExecPlan(), "2026-10-02") == []
    assert load_trades(pool_config.POOL_LEDGER_PATH) == []

    _seed_holding()
    plan = execution.ExecPlan(trades=[execution.TradeIntent(
        code="600001", name="股600001", side="sell", qty=5000,
        price=12.0, reason="out_of_pool")])
    lines = assembly.record_trades(plan, "2026-10-02")
    assert lines and all("600001" in ln for ln in lines)
    trades = load_trades(pool_config.POOL_LEDGER_PATH)
    assert [t.side for t in trades] == ["buy", "sell"]
    assert trades[-1].qty == 5000 and trades[-1].price == 12.0
