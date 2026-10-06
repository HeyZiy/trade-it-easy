# -*- coding: utf-8 -*-
"""
===================================
名义台账 — 批次执行（execute_batch）
===================================

「一批指令安全记入台账」的批次语义单点（入口脚本不各自复制）：

- 整手收敛：卖 floor_lot（不足一手跳过该笔）/ 买 round_lot，复用
  src/mx/executor 单点，对已整手的上游指令幂等；
- 指定台账派生真实持仓和现金，按代码累计卖量；初始安全校验失败
  整批零记账（累计卖量 ≤ 持仓、买额 ≤ 现金 + 计划卖出回款）；
- 先卖后买，逐笔以新鲜台账检查持仓与现金；卖出写入失败不能为买单
  提供预计回款，资金不足的原买单失败，不擅自缩量，其余指令继续。

策略自有约束不进本模块（如卫星 10% 预算、趋势卖出侧四态）——
那是消费方 policy，留在各入口/管线。返回结构化 BatchResult，
入口经 OrderOutcome.outcome_line 收集报告行（渲染单点）。
"""

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import List, Optional

from src.mx.executor import floor_lot, round_lot
from src.trade_ledger.ledger import (
    DEFAULT_LEDGER_PATH, LedgerError, TradeRecord, append_trade, derive, load_trades,
)
from data_provider.codes import canonical_stock_code

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
                  trade_date: Optional[str] = None,
                  path: Optional[Path | str] = None) -> BatchResult:
    """执行一批台账指令：收敛 → 安全校验 → 先卖后买逐笔记账。

    持仓/现金只从 path 派生，不接受调用方快照；account 仅归因不分账。
    批次日期不得早于现有流水；手动旧日期补记仍走 append_trade。
    """
    path = DEFAULT_LEDGER_PATH if path is None else path
    trade_date = trade_date or date.today().isoformat()

    # 整手收敛（先收敛后校验，校验数字即真实记账数字）
    plan: List[tuple] = []      # (归一后的指令, 最终记账记录)
    outcomes: List[OrderOutcome] = []
    for o in orders:
        qty = 0
        try:
            qty = _converge(o) if o.qty > 0 else 0
            if qty <= 0:
                outcomes.append(OrderOutcome(
                    order=o, qty=0, status=ST_SKIPPED, message="股数不足一手"))
                continue
            date.fromisoformat(trade_date)
            if not math.isfinite(o.price):
                raise LedgerError(f"price 须为有限正数，收到 {o.price!r}")
            normalized = replace(o, code=canonical_stock_code(o.code),
                                 price=round(o.price, 4))
            record = TradeRecord(
                date=trade_date, code=normalized.code, name=o.name, side=o.side,
                qty=qty, price=normalized.price,
                account=account, reasons=[o.reason] if o.reason else [])
            if not math.isfinite(record.qty * record.price):
                raise LedgerError("成交金额非有限数，拒绝记账")
            plan.append((normalized, record))
        except (LedgerError, ValueError, TypeError, OverflowError) as e:
            outcomes.append(OrderOutcome(order=o, qty=qty, status=ST_FAILED,
                                         message=str(e)))
    if not plan:
        return BatchResult(abort=None, outcomes=outcomes)

    trades = load_trades(path)
    if trades and trades[-1].date > trade_date:
        return BatchResult(abort=f"批次日期 {trade_date} 早于现有流水 {trades[-1].date}")
    snapshot = derive(trades, as_of=trade_date)
    held_counts = snapshot.held_counts()
    cash = snapshot.cash
    if not math.isfinite(cash):
        raise LedgerError("台账现金非有限数，拒绝批次记账")
    sells = [(o, r) for o, r in plan if o.side == "sell"]
    buys = [(o, r) for o, r in plan if o.side == "buy"]

    # 累计卖量校验：同码多条指令共享一本台账的实际持仓。
    sold_counts = {}
    for o, record in sells:
        sold_counts[o.code] = sold_counts.get(o.code, 0) + record.qty
        held = held_counts.get(o.code, 0)
        if sold_counts[o.code] > held:
            return BatchResult(
                abort=f"{o.name}({o.code}) 累计卖出 {sold_counts[o.code]} 股 > 持仓 {held} 股")

    # 初始计划预检：买入总额不得超过台账现金 + 计划卖出回款（费用盲检；
    # 计划侧 affordable_shares 已按费用从严预检，严口径在前，见 quality_pool/execution）
    sell_amount = sum(r.qty * r.price for _, r in sells)
    buy_amount = sum(r.qty * r.price for _, r in buys)
    if not all(math.isfinite(value) for value in (sell_amount, buy_amount, cash + sell_amount)):
        return BatchResult(abort="批次累计成交金额或资金非有限数，拒绝记账")
    if _cash_short(buy_amount, cash + sell_amount):
        return BatchResult(
            abort=(f"买入总额 {buy_amount:,.0f} 元 > 可用资金 {cash:,.0f} 元"
                   f" + 卖出回款 {sell_amount:,.0f} 元"))

    # 每笔跨同一 seam：重新读台账，写入结果而非预计回款决定后续可用资金。
    for o, record in sells + buys:
        current = derive(load_trades(path), as_of=trade_date)
        if not math.isfinite(current.cash):
            raise LedgerError("台账现金非有限数，拒绝继续记账")
        message = ""
        if o.side == "sell" and record.qty > current.held_counts().get(o.code, 0):
            message = f"卖出 {record.qty} 股 > 当前台账持仓 {current.held_counts().get(o.code, 0)} 股"
        elif o.side == "buy" and _cash_short(record.qty * record.price, current.cash):
            message = (f"买入额 {record.qty * record.price:,.2f} 元 > "
                       f"实际台账现金 {current.cash:,.2f} 元，本笔零记账")
        change = record.qty * record.price * (1 if o.side == "sell" else -1)
        if not math.isfinite(current.cash + change):
            message = "记账后现金非有限数，本笔零记账"
        if message:
            outcomes.append(OrderOutcome(order=o, qty=record.qty, status=ST_FAILED,
                                         message=message))
            continue
        try:
            append_trade(record, path=path)
            outcomes.append(OrderOutcome(order=o, qty=record.qty, status=ST_OK))
        except (LedgerError, OSError) as e:
            logger.warning(f"批次记账失败 {o.side} {o.code} {record.qty}股: {e}")
            outcomes.append(OrderOutcome(order=o, qty=record.qty, status=ST_FAILED,
                                         message=str(e)))
    return BatchResult(abort=None, outcomes=outcomes)


def _cash_short(amount: float, available: float) -> bool:
    """只容忍浮点累加误差；台账名义额保持原无费用口径。"""
    return amount > available and not math.isclose(
        amount, available, rel_tol=0.0, abs_tol=1e-7)
