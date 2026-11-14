# -*- coding: utf-8 -*-
"""跨资产动量独立模拟账户的分红登记、行情检查与调仓记账。"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from data_provider.security_names import security_name
from src.etf.momentum import EtfOrder
from src.etf.rotation import (
    ACCOUNT, COMMISSION, MIN_COMMISSION, RotationRow, dividend_records, plan_rotation,
)
from src.trade_ledger import (
    BatchOrder, BatchResult, LedgerSnapshot, TradeRecord,
    append_trade, derive, execute_batch, load_trades,
)
from src.trade_ledger.performance import execution_summary

logger = logging.getLogger(__name__)


@dataclass
class EtfExecution:
    batch: BatchResult
    summary: str
    positions: list[dict] | None


@dataclass
class RotationRun:
    before: LedgerSnapshot
    dividend_count: int
    plan: dict
    guard: str | None = None
    execution: EtfExecution | None = None


def run_simulation(
    rows: Sequence[RotationRow], prices: dict[str, float], diag: dict, *,
    trade_date: str, ledger_path: Path | str, dry_run: bool = False,
) -> RotationRun:
    """登记分红权益，按当日行情校验并执行独立模拟账户的调仓计划。"""
    trades = load_trades(ledger_path)
    distributions = dividend_records(trades, diag.get("dividends", {}), trade_date)
    if dry_run:
        trades = trades + distributions
    else:
        for record in distributions:
            append_trade(record, path=ledger_path)
        trades = load_trades(ledger_path)

    before = derive(trades, trade_date, prices)
    positions = [{**p, "name": security_name(p["code"])} for p in before.positions]
    plan = plan_rotation(rows, positions, prices, before.cash)

    guard = _market_guard(rows, prices, diag, before)
    if guard:
        logger.warning(f"[行情降级] 轮动本次不执行: {guard}")
        return RotationRun(before, len(distributions), plan, guard=guard)

    execution = execute_orders(plan["orders"], account=ACCOUNT, trade_date=trade_date,
                               ledger_path=ledger_path, before=trades, prices=prices,
                               dry_run=dry_run, commission_rate=COMMISSION,
                               min_commission=MIN_COMMISSION)
    return RotationRun(before, len(distributions), plan, execution=execution)


def _market_guard(rows, prices, diag, before: LedgerSnapshot) -> str | None:
    """取数故障与策略清仓信号使用不同的执行路径。"""
    if not rows:
        return ("池内 15 只全部无可用日线数据，按 fail-closed 本次不调仓"
                "（不清仓、不动持仓）")
    if any(p["code"] not in prices for p in before.positions):
        return "持仓现价缺失，本次不执行；收盘价不得用作盘中成交价"
    if diag["realtime_missing"]:
        return "实时价不完整，本次不执行；收盘价不得用作盘中成交价"
    if any(diag.get("skipped", {}).get(k, 0) for k in ("no_bars", "stale", "bad_close", "duplicate")):
        return "候选池行情或分红数据不完整，本次不执行"
    if before.total_assets <= 0:
        return "策略账户净值不可用，本次不调仓"
    if any(float(p.get("current_price") or 0) <= 0 for p in before.positions):
        return "持仓现价缺失，无法确定卖出金额，本次不调仓"
    return None


def execute_orders(
    orders: Sequence[EtfOrder], *, account: str, trade_date: str,
    ledger_path: Path | str, before: list[TradeRecord], prices: dict[str, float],
    dry_run: bool = False, commission_rate: float = 0.0, min_commission: float = 0.0,
) -> EtfExecution:
    """执行已通过策略校验的订单，用同一份执行后流水核对成交和持仓。"""
    if dry_run or not orders:
        return EtfExecution(
            BatchResult(abort=None),
            execution_summary(before, before, dry_run=dry_run, planned=len(orders)),
            None,
        )

    batch_orders = [
        BatchOrder(code=o.code, name=o.name, side=o.action, qty=o.shares,
                   price=o.price, reason=o.reason)
        for o in orders
    ]
    batch = execute_batch(batch_orders, account=account, trade_date=trade_date, path=ledger_path,
                          commission_rate=commission_rate, min_commission=min_commission)
    after = load_trades(ledger_path)
    summary = execution_summary(before, after, planned=len(orders))
    positions = None
    if not batch.abort:
        valuation_prices = {**prices, **{o.code: o.price for o in orders if o.price > 0}}
        positions = derive(after, trade_date, valuation_prices).positions
    return EtfExecution(batch, summary, positions)
