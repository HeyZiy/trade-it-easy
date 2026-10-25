# -*- coding: utf-8 -*-
"""执行计划 — 纯判定核（规格三、四节）。

输入是执行时点快照（价格/涨跌停/状态/台账持仓），输出成台的交易意图与
受阻事件；记账交给 trade_ledger.execute_batch（通用批次安全校验单点）。
本模块零 I/O：真实下单不存在，成交价 = 执行时实时价（按判定价模拟记账）。

资金口径两层关系（勿混）：affordable_shares 是计划侧费用感知预检
（佣金+滑点，从严），execute_batch 安全校验 2 是记账侧费用盲检
（买入 ≤ 现金+卖出回款）——计划量必然通过记账校验，严口径在前。

执行序（规格三节，须保留）：先待退出仓 → 减超额留存仓 → 按目标名单顺序
补不足（旧仓排新仓前）。现金受限时该顺序影响实际组合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.mx.executor import floor_lot  # 整手收敛单点（勿在他处重写）
from src.quality_pool.config import (
    LIMIT_PAD, MIN_COMMISSION, FEE_COMMISSION, SLIPPAGE_SPREAD, TOP_N,
)
from src.quality_pool.screener import industry_buy_block_reason

SELL_TAX_NOTE = ""  # 台账不记费用；滑点/佣金只用于资金可行性检查（规格三节）


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
    reason: str        # equal_weight | exit_reason
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
    """非调仓日：仅重试退出队列（每交易日模拟任务中），照常受停牌/跌停/可卖量约束。
    部分成交的剩余持仓继续留在队列（与调仓日退出语义一致）。"""
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
    """调仓日整轮计划：先退出 → 减超额留存 → 补不足。

    selected          目标名单（旧仓排新仓前，即执行序）
    exit_reasons      确认出池/尚未完成的既有退出
    per_budget        本轮冻结的单只目标市值（总权益/6，编排层冻结）
    deferred          数据未知旧仓，保留原数量，不参与再平衡
    industries        本轮行业映射；退出受阻仍按实际持仓占用行业额度
    """
    result = ExecPlan()
    counts = dict(snap.counts)
    deferred = set(deferred)
    cash = snap.cash
    queue = {code: reason for code, reason in exit_reasons.items()
             if counts.get(code, 0) > 0}

    # ── 1. 待退出仓：正常可卖即全出；部分可卖先出可卖部分，剩余留队列 ──
    for code in sorted(queue):
        reason = queue[code]
        blocked = _sell_block(code, snap)
        if blocked is not None:
            result.blocked.append(BlockedEvent(code, "sell", reason, blocked))
            continue
        qty = _sellable_qty(code, snap)
        if qty <= 0:
            result.blocked.append(BlockedEvent(code, "sell", reason, "no_closeable"))
            continue
        result.trades.append(TradeIntent(
            code, snap.names.get(code, ""), "sell", qty, snap.prices[code], reason))
        cash += qty * snap.prices[code]
        counts[code] -= qty
        if counts[code] > 0:
            queue[code] = reason      # 未退出完成：继续占用持仓名额

    result.exit_queue = {c: r for c, r in queue.items() if counts.get(c, 0) > 0}

    # ── 2. 超额留存仓差额减持（高于预算减到预算，不为费用微差强卖整手） ──
    for code in selected:
        current = counts.get(code, 0)
        if current <= 0 or code in queue or code in deferred:
            continue
        px = snap.prices.get(code)
        if px is None:
            continue
        reduction = floor_lot(current - per_budget / px)
        if reduction <= 0:
            continue
        blocked = _sell_block(code, snap)
        if blocked is not None:
            result.blocked.append(BlockedEvent(code, "sell", "equal_weight", blocked))
            continue
        qty = floor_lot(min(reduction, snap.avail.get(code, 0)))
        if qty <= 0:
            result.blocked.append(BlockedEvent(code, "sell", "equal_weight", "no_closeable"))
            continue
        result.trades.append(TradeIntent(
            code, snap.names.get(code, ""), "sell", qty, px, "equal_weight"))
        cash += qty * px
        counts[code] -= qty

    # ── 3. 按目标名单顺序补不足；在途退出占用的名额不让给新股票 ──
    reserved = {c for c, n in counts.items() if n > 0}
    for code in selected:
        if code in queue or code in deferred:
            continue
        current = counts.get(code, 0)
        px = snap.prices.get(code)
        blocked = _buy_block(code, snap)
        if blocked is not None:
            result.blocked.append(BlockedEvent(code, "buy", "equal_weight", blocked))
            continue
        desired = floor_lot(per_budget / px)
        delta = desired - current
        if delta < 100:
            continue
        if current == 0 and code not in reserved and len(reserved) >= TOP_N:
            result.blocked.append(BlockedEvent(code, "buy", "equal_weight", "slot"))
            continue
        blocked = industry_buy_block(code, reserved, industries)
        if blocked:
            result.blocked.append(BlockedEvent(code, 'buy', 'equal_weight', blocked))
            continue
        qty = floor_lot(min(delta, affordable_shares(cash, px)))
        if qty < 100:
            result.blocked.append(BlockedEvent(code, "buy", "equal_weight", "cash/lot"))
            continue
        result.trades.append(TradeIntent(
            code, snap.names.get(code, ""), "buy", qty, px, "equal_weight"))
        cash -= qty * px
        counts[code] = current + qty
        reserved.add(code)

    return result
