# -*- coding: utf-8 -*-
"""根据实时价格、持仓及交易限制生成退出和补位意图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.mx.executor import floor_lot  # 整手收敛单点（勿在他处重写）
from src.quality_pool.config import (
    LIMIT_PAD, MIN_COMMISSION, FEE_COMMISSION, SLIPPAGE_SPREAD, TOP_N,
)
from src.quality_pool.screener import industry_buy_block_reason

@dataclass
class ExecSnapshot:
    """执行时点快照（编排层组装，台账派生 + 实时行情 + 当日状态）。"""
    trade_date: str
    counts: Dict[str, int]          # code → 台账持仓股数
    avail: Dict[str, int]           # code → T+1 可卖股数
    cash: float
    equity: float                   # 总权益 = 现金 + 持仓按现价市值（每轮冻结预算用）
    prices: Dict[str, float]        # code → 执行时实时价（缺/无效不在 dict 中）
    names: Dict[str, str]
    suspended: Dict[str, bool]      # code → 执行日当时停牌
    st: Dict[str, bool]             # code → 执行日当时 ST
    limits: Dict[str, Tuple[float, float]]   # code → (涨停价, 跌停价)
    unavailable: set = field(default_factory=set)  # 官方状态/涨跌停价缺失，不推造规则


@dataclass
class TradeIntent:
    code: str
    name: str
    side: str          # buy | sell
    qty: int
    price: float
    reason: str


@dataclass
class BlockedEvent:
    code: str
    action: str        # buy | sell
    reason: str        # entry | exit_reason
    blocked_by: str    # paused | ST | invalid_price | upper_limit | lower_limit | no_closeable | cash/lot | slot


@dataclass
class ExecPlan:
    trades: List[TradeIntent] = field(default_factory=list)
    blocked: List[BlockedEvent] = field(default_factory=list)
    exit_queue: Dict[str, str] = field(default_factory=dict)   # code → 退出原因（仍持有的部分含未完成退出）


def affordable_shares(cash: float, price: float) -> int:
    """可用资金能买起的整手股数：实际佣金=max(比例佣金,最低佣金)，双约束取严。"""
    estimated = price * (1 + SLIPPAGE_SPREAD / 2)
    by_rate = int(max(0.0, cash) / (estimated * (1 + FEE_COMMISSION)) // 100) * 100
    by_minimum = int(max(0.0, cash - MIN_COMMISSION) / estimated // 100) * 100
    return min(by_rate, by_minimum)


def _sell_block(code: str, snap: ExecSnapshot) -> Optional[str]:
    if code in snap.unavailable:
        return 'status/limits_unknown'
    if snap.suspended.get(code, False):
        return "paused"
    if snap.prices.get(code) is None:
        return "invalid_price"
    limits = snap.limits.get(code)
    if limits is not None and snap.prices[code] <= limits[1] + LIMIT_PAD:
        return "lower_limit"
    return None


def _buy_block(code: str, snap: ExecSnapshot) -> Optional[str]:
    if code in snap.unavailable:
        return 'status/limits_unknown'
    if snap.suspended.get(code, False):
        return "paused"
    if snap.st.get(code, False):
        return "ST"
    if snap.prices.get(code) is None:
        return "invalid_price"
    limits = snap.limits.get(code)
    if limits is not None and snap.prices[code] >= limits[0] - LIMIT_PAD:
        return "upper_limit"
    return None


def _sellable_qty(code: str, snap: ExecSnapshot) -> int:
    return floor_lot(min(snap.counts.get(code, 0), snap.avail.get(code, 0)))


def industry_buy_block(code: str, held, industries: Optional[dict]) -> Optional[str]:
    """执行核对的行业闸门：规则单点在 screener.industry_buy_block_reason。

    industries 为 None（无名单的退出重试轮）或已持有（不重复买）不拦。
    """
    if industries is None or code in held:
        return None
    return industry_buy_block_reason(industries.get(code), held, industries)


def plan_exit_retry(exit_queue: Dict[str, str], snap: ExecSnapshot) -> ExecPlan:
    """无有效新仓计划时重试退出队列，照常受停牌/跌停/可卖量约束。
    部分成交的剩余持仓继续留在队列（与完整计划一致）。"""
    result = ExecPlan()
    remaining: Dict[str, str] = {}
    counts = dict(snap.counts)
    for code in sorted(exit_queue):
        if counts.get(code, 0) <= 0:
            continue
        reason = exit_queue[code]
        blocked = _sell_block(code, snap)
        if blocked is None:
            qty = _sellable_qty(code, snap)
            if qty <= 0:
                result.blocked.append(BlockedEvent(code, "sell", reason, "no_closeable"))
                remaining[code] = reason
                continue
            result.trades.append(TradeIntent(
                code, snap.names.get(code, ""), "sell", qty,
                snap.prices[code], reason))
            counts[code] -= qty
            if counts[code] > 0:
                remaining[code] = reason
        else:
            result.blocked.append(BlockedEvent(code, "sell", reason, blocked))
            remaining[code] = reason
    result.exit_queue = {c: r for c, r in remaining.items() if counts.get(c, 0) > 0}
    return result


def plan_round(selected: List[str], exit_reasons: Dict[str, str],
               per_budget: float, snap: ExecSnapshot, deferred=(), industries=None) -> ExecPlan:
    """先执行退出意图，再用剩余名额和现金补新仓。"""
    result = plan_exit_retry(exit_reasons, snap)
    counts = dict(snap.counts)
    cash = snap.cash
    for sale in result.trades:
        counts[sale.code] -= sale.qty
        cash += sale.qty * sale.price

    reserved = {c for c, n in counts.items() if n > 0}
    deferred = set(deferred)
    for code in selected:
        if snap.counts.get(code, 0) > 0 or code in deferred or code in reserved:
            continue
        px = snap.prices.get(code)
        blocked = _buy_block(code, snap)
        if blocked is not None:
            result.blocked.append(BlockedEvent(code, "buy", "entry", blocked))
            continue
        if len(reserved) >= TOP_N:
            result.blocked.append(BlockedEvent(code, "buy", "entry", "slot"))
            continue
        blocked = industry_buy_block(code, reserved, industries)
        if blocked:
            result.blocked.append(BlockedEvent(code, 'buy', 'entry', blocked))
            continue
        qty = floor_lot(min(per_budget / px, affordable_shares(cash, px)))
        if qty < 100:
            result.blocked.append(BlockedEvent(code, "buy", "entry", "cash/lot"))
            continue
        result.trades.append(TradeIntent(
            code, snap.names.get(code, ""), "buy", qty, px, "entry"))
        cash -= qty * px
        reserved.add(code)

    return result
