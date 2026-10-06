# -*- coding: utf-8 -*-
"""
===================================
名义台账 — 批次执行（execute_batch）
===================================

「一批指令安全记入台账」的批次语义单点（入口脚本不各自复制）：

- 整手收敛：卖 floor_lot（不足一手跳过该笔）/ 买 round_lot，复用
  src/mx/executor 单点，对已整手的上游指令幂等；
- 两项通用安全校验（任一失败整批中止、零记账）：
  卖出股数 ≤ 持仓；买入总额 ≤ 现金 + 卖出回款；
- 先卖后买排序；逐单隔离记账（单笔失败不阻断其余）。

策略自有约束不进本模块（如卫星 10% 预算、趋势卖出侧四态）——
那是消费方 policy，留在各入口/管线。返回结构化 BatchResult，
入口经 OrderOutcome.outcome_line 收集报告行（渲染单点）。
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional

from src.mx.executor import floor_lot, round_lot
from src.trade_ledger.ledger import (
    DEFAULT_LEDGER_PATH, LedgerError, TradeRecord, append_trade,
)

logger = logging.getLogger(__name__)

ST_OK = "ok"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"


@dataclass(frozen=True)
class BatchOrder:
    """一条批次指令（消费方从各自异构 Order 一行映射而来）。"""
    code: str
    name: str
    side: str          # buy | sell
    qty: int           # 收敛前请求股数
    price: float
    reason: str = ""


@dataclass(frozen=True)
class OrderOutcome:
    """单笔执行结论（收敛后口径）。"""
    order: BatchOrder
    qty: int           # 实际记账股数（skipped 为 0）
    status: str        # ok | failed | skipped
    message: str = ""

    @property
    def amount(self) -> float:
        return self.qty * self.order.price

    @property
    def outcome_line(self) -> str:
        """单笔结论的报告行（渲染单点；入口循环只收集不拼串）。"""
        o = self.order
        head = f"{o.side.upper()} {o.name}({o.code})"
        if self.status == ST_SKIPPED:
            return f"⚠️ 跳过 {head}：{self.message}"
        if self.status == ST_OK:
            return f"✅ {head} {self.qty}股 ≈ {self.amount:,.0f}元 —— 已记账（{o.reason}）"
        return f"❌ {head} {self.qty}股 —— 记账失败: {self.message}"


@dataclass(frozen=True)
class BatchResult:
    """整批结论：abort 非空 = 安全校验未过（outcomes 为空、零记账）。"""
    abort: Optional[str]
    outcomes: List[OrderOutcome] = field(default_factory=list)


def _converge(order: BatchOrder) -> int:
    return floor_lot(order.qty) if order.side == "sell" else round_lot(order.qty)


def execute_batch(orders: List[BatchOrder], *, account: str,
                  held_counts: dict, cash: float,
                  trade_date: Optional[str] = None,
                  path: Optional[Path | str] = None) -> BatchResult:
    """执行一批台账指令：收敛 → 安全校验 → 先卖后买逐笔记账。

    path=None 走默认台账文件（消费方多为不传即默认的入口脚本）。
    """
    path = DEFAULT_LEDGER_PATH if path is None else path
    trade_date = trade_date or date.today().isoformat()

    # 整手收敛（先收敛后校验，校验数字即真实记账数字）
    plan: List[tuple] = []      # (order, qty)
    skipped: List[OrderOutcome] = []
    for o in orders:
        qty = _converge(o) if o.qty > 0 else 0
        if qty <= 0:
            skipped.append(OrderOutcome(
                order=o, qty=0, status=ST_SKIPPED, message="股数不足一手"))
            continue
        plan.append((o, qty))

    sells = [(o, q) for o, q in plan if o.side == "sell"]
    buys = [(o, q) for o, q in plan if o.side == "buy"]

    # 安全校验 1：卖出股数不得超过台账实际持仓
    for o, q in sells:
        held = int(held_counts.get(o.code, 0) or 0)
        if q > held:
            return BatchResult(
                abort=f"{o.name}({o.code}) 卖出 {q} 股 > 持仓 {held} 股")

    # 安全校验 2：买入总额不得超过台账现金 + 卖出回款（费用盲检；
    # 计划侧 affordable_shares 已按费用从严预检，严口径在前，见 quality_pool/execution）
    sell_amount = sum(q * o.price for o, q in sells)
    buy_amount = sum(q * o.price for o, q in buys)
    if buy_amount > cash + sell_amount:
        return BatchResult(
            abort=(f"买入总额 {buy_amount:,.0f} 元 > 可用资金 {cash:,.0f} 元"
                   f" + 卖出回款 {sell_amount:,.0f} 元"))

    # 先卖后买，逐单隔离
    outcomes: List[OrderOutcome] = list(skipped)
    for o, q in sells + buys:
        try:
            append_trade(TradeRecord(
                date=trade_date, code=o.code, name=o.name, side=o.side,
                qty=q, price=round(o.price, 4),
                account=account, reasons=[o.reason] if o.reason else []),
                path=path)
            outcomes.append(OrderOutcome(order=o, qty=q, status=ST_OK))
        except (LedgerError, OSError) as e:
            logger.warning(f"批次记账失败 {o.side} {o.code} {q}股: {e}")
            outcomes.append(OrderOutcome(order=o, qty=q, status=ST_FAILED,
                                         message=str(e)))
    return BatchResult(abort=None, outcomes=skipped + outcomes)
