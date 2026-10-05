# -*- coding: utf-8 -*-
"""质量池执行核测试 — 规格三、四节逐条映射（strategy/roe_quality_pool.md）。"""

import pytest

from src.quality_pool import execution
from src.quality_pool.config import TOP_N
from src.quality_pool.execution import ExecSnapshot, plan_exit_retry, plan_round


def _snap(**over) -> ExecSnapshot:
    """默认快照：空仓 + 现金 100 万 + 全部可正常成交。"""
    base = dict(
        trade_date="2026-10-05",
        counts={}, avail={}, cash=1_000_000.0, equity=1_000_000.0,
        prices={}, names={}, suspended={}, st={}, limits={},
    )
    base.update(over)
    return ExecSnapshot(**base)


def _holding(snap: ExecSnapshot, code: str, count: int, price: float,
             avail: int = None) -> ExecSnapshot:
    snap.counts[code] = count
    snap.avail[code] = count if avail is None else avail
    snap.prices[code] = price
    snap.equity = snap.cash + sum(snap.counts[c] * snap.prices[c]
                                  for c in snap.counts if c in snap.prices)
    return snap


# ── 资金可行性 ──

def test_affordable_shares_dual_constraint():
    # 比例佣金约束更紧的情形：cash/(px*1.001*(1+0.00025))
    px = 10.0
    by_rate = int(100_000 / (10.0 * 1.001 * 1.00025) // 100) * 100
    assert execution.affordable_shares(100_000, px) == by_rate
    # 最低佣金约束更紧的情形：小额现金
    assert execution.affordable_shares(6.0, 100.0) == 0
    # 现金为负 → 0
    assert execution.affordable_shares(-1.0, 10.0) == 0


# ── 四、怎么卖、卖不掉怎么办 ──

def test_exit_sells_full_position():
    snap = _holding(_snap(), "600001", 1000, 10.0)
    plan = plan_round([], {"600001": "out_of_pool"}, 50_000.0, snap)
    sells = [t for t in plan.trades if t.side == "sell"]
    assert len(sells) == 1 and sells[0].qty == 1000 and sells[0].reason == "out_of_pool"
    assert plan.exit_queue == {}


def test_exit_blocked_by_suspension_stays_in_queue():
    snap = _holding(_snap(suspended={"600001": True}), "600001", 1000, 10.0)
    plan = plan_round([], {"600001": "rank_below_buffer"}, 50_000.0, snap)
    assert plan.trades == []
    assert plan.exit_queue == {"600001": "rank_below_buffer"}


def test_exit_blocked_at_lower_limit():
    snap = _holding(_snap(limits={"600001": (11.0, 9.0)}), "600001", 1000, 9.0)
    plan = plan_round([], {"600001": "out_of_pool"}, 50_000.0, snap)
    assert plan.trades == []
    assert plan.exit_queue == {"600001": "out_of_pool"}
    assert plan.blocked[0].blocked_by == "lower_limit"


def test_exit_partial_closeable_rest_queued():
    """部分成交：可卖部分先出，剩余持仓留在退出队列且继续占名额。"""
    snap = _holding(_snap(), "600001", 1000, 10.0, avail=500)
    plan = plan_round([], {"600001": "out_of_pool"}, 50_000.0, snap)
    assert plan.trades[0].qty == 500
    assert plan.exit_queue == {"600001": "out_of_pool"}


def test_exit_retry_only_touches_queue():
    snap = _holding(_snap(), "600001", 1000, 10.0, avail=500)
    plan = plan_exit_retry({"600001": "out_of_pool", "600099": "out_of_pool"}, snap)
    assert [t.qty for t in plan.trades] == [500]
    assert plan.exit_queue == {"600001": "out_of_pool"}


def test_exit_retry_completed_position_dropped():
    snap = _snap()   # 已无持仓
    plan = plan_exit_retry({"600001": "out_of_pool"}, snap)
    assert plan.trades == [] and plan.exit_queue == {}


# ── 三、怎么买、配多少钱 ──

def test_new_position_buys_budget_lot_floored():
    snap = _snap(prices={"600001": 9.87})   # 50_000/9.87 ≈ 5065.8 → 5000 股
    plan = plan_round(["600001"], {}, 50_000.0, snap)
    buys = [t for t in plan.trades if t.side == "buy"]
    assert len(buys) == 1 and buys[0].qty == 5000


def test_cash_short_buys_what_is_affordable():
    snap = _snap(cash=40_000.0, prices={"600001": 10.0})   # 预算5万，只买得起约3.9万
    plan = plan_round(["600001"], {}, 50_000.0, snap)
    assert plan.trades[0].qty < 5000 and plan.trades[0].qty % 100 == 0
    assert plan.trades[0].qty * 10.0 * 1.001 * 1.00025 <= 40_000.0 + 100


def test_buy_blocked_by_upper_limit_or_st():
    snap = _snap(limits={"600001": (11.0, 9.0)}, prices={"600001": 11.0})
    plan = plan_round(["600001"], {}, 50_000.0, snap)
    assert plan.trades == [] and plan.blocked[0].blocked_by == "upper_limit"
    snap = _snap(st={"600002": True}, prices={"600002": 10.0})
    plan = plan_round(["600002"], {}, 50_000.0, snap)
    assert plan.trades == [] and plan.blocked[0].blocked_by == "ST"


def test_overweight_keeper_reduced_to_budget():
    snap = _holding(_snap(), "600001", 8000, 10.0)
    plan = plan_round(["600001"], {}, 50_000.0, snap)   # 目标 5000 股
    sells = [t for t in plan.trades if t.side == "sell"]
    assert len(sells) == 1 and sells[0].qty == 3000 and sells[0].reason == "equal_weight"


def test_underweight_keeper_topped_up():
    snap = _holding(_snap(), "600001", 3000, 10.0)
    plan = plan_round(["600001"], {}, 50_000.0, snap)   # 补到 5000
    buys = [t for t in plan.trades if t.side == "buy"]
    assert len(buys) == 1 and buys[0].qty == 2000


def test_tiny_deviation_not_traded():
    """差额不足一手不动（规格三节：交易差额不足100股则不动）。"""
    snap = _holding(_snap(), "600001", 5040, 10.0)   # 高于预算 400 股 → 减 0
    plan = plan_round(["600001"], {}, 50_000.0, snap)
    assert plan.trades == []
    snap = _holding(_snap(), "600001", 5060, 10.0)   # 高 60 股
    plan = plan_round(["600001"], {}, 50_000.0, snap)
    assert plan.trades == []


def test_pending_exit_occupies_slot():
    """退出受阻占名额：20 只满员 + 1 只退出受阻时，不为新股票开仓。"""
    snap = _snap()
    codes = [f"{600000 + i}" for i in range(TOP_N)]
    for i, code in enumerate(codes):
        _holding(snap, code, 1000, 10.0)
    snap.suspended[codes[0]] = True                  # 第 1 只停牌退不出
    snap.prices["600099"] = 10.0                     # 候补新股票
    plan = plan_round(["600099"], {codes[0]: "out_of_pool"}, 50_000.0, snap)
    buys = [t for t in plan.trades if t.side == "buy"]
    assert buys == []
    assert any(b.blocked_by == "slot" for b in plan.blocked)


def test_freed_slot_allows_new_buy():
    """退出在本轮完成的名额让给新股票（规格二节例：B 退出，第 8 名补入）。"""
    snap = _snap()
    codes = [f"{600000 + i}" for i in range(TOP_N)]
    for i, code in enumerate(codes):
        _holding(snap, code, 1000, 10.0)
    snap.prices["600099"] = 10.0
    plan = plan_round(["600099"], {codes[0]: "out_of_pool"}, 50_000.0, snap)
    sells = [t for t in plan.trades if t.side == "sell"]
    buys = [t for t in plan.trades if t.side == "buy"]
    assert any(t.code == codes[0] for t in sells)
    assert any(t.code == "600099" for t in buys)
    assert plan.exit_queue == {}


def test_sequencing_sells_before_buys():
    """先卖后买：卖出指令先于买入指令（规格三节执行序）。"""
    snap = _holding(_snap(), "600001", 8000, 10.0)
    snap.prices["600002"] = 10.0
    plan = plan_round(["600001", "600002"], {}, 50_000.0, snap)
    sides = [t.side for t in plan.trades]
    assert sides[0] == "sell" and "buy" in sides


def test_sell_proceeds_fund_later_buys():
    """卖出回款可用于本轮买入（先卖后买执行序的自然结果）；受阻卖出的回款
    不会虚增（受阻不产生回款）。"""
    snap = _holding(_snap(cash=0.0), "600001", 8000, 10.0)   # 现金 0，减仓 3000 股
    snap.prices["600002"] = 10.0
    plan = plan_round(["600001", "600002"], {}, 50_000.0, snap)
    buy = [t for t in plan.trades if t.side == "buy" and t.code == "600002"]
    assert buy and buy[0].qty > 0
