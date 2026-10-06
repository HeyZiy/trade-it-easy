# -*- coding: utf-8 -*-
"""
===================================
质量池 09:31 执行闭环 — 快照、模拟记账与跨日状态
===================================

根入口（编排）与判定核（execution / screener）之间的合并点：

- build_snapshot：台账派生 + 实时行情 + 当日状态。台账一次派生、状态表
  一次拉取（停牌/ST/涨跌停三个消费共享）；ST 与停牌一律以状态表官方
  标志为准（与筛分侧同口径，不用名称子串推断）；状态表缺该码的行按
  suspended=True / st=True 处置——确认不了可交易就不动（fail-closed，
  规格数据暂停精神）。总权益直接取台账快照 total_assets（现金 + 持仓
  按现价市值），不在本层重算。
- run_execution：名单消费、冻结预算、先卖后买与退出队列核对集中于此。
  卖单记账后以实际现金与持仓收敛原买单；退出队列只认台账剩余持仓。
  记账前保存完整退出意图与名单消费，记账后保存核对结果，中断后只重试
  退出，不用旧名单追补买入或等权调整。报告落盘与推送由入口负责。

本模块零网络：数据一律经 feeds seam 进入；测试在 feeds 层放假数据
打真实装配逻辑。路径经 config 运行时读取，测试可单点替换。
"""

import logging
from dataclasses import replace
from datetime import date, timedelta
from typing import List

from src.quality_pool import config, execution, feeds, state as pool_state
from src.trade_ledger.batch import BatchOrder, BatchResult, execute_batch
from src.trade_ledger.ledger import derive, load_trades
import src.trading_calendar as trading_calendar

logger = logging.getLogger(__name__)


def build_snapshot(exec_date: str, selected: List[str]) -> execution.ExecSnapshot:
    """09:31 执行时点快照：台账派生 + 实时价 + 当日状态（状态表单次、口径权威）。

    状态表缺行 → 该码按停牌/ST 处置（fail-closed，缺数据的品种不参与成交判定）。
    """
    import datetime as dt

    trades = load_trades(config.POOL_LEDGER_PATH)
    codes = sorted({t.code for t in trades} | set(selected))
    prices, names = feeds.fetch_realtime_quotes(codes)

    snap0 = derive(trades, as_of=exec_date, prices=prices)
    counts = snap0.held_counts()
    avail = {p["code"]: int(p["avail_count"]) for p in snap0.positions}

    prev_day = trading_calendar.latest_trading_day_on_or_before(
        dt.date.fromisoformat(exec_date) - dt.timedelta(days=1))
    if prev_day is None:
        raise RuntimeError(f"交易日历无 {exec_date} 之前的数据")
    prev_close = feeds.raw_closes_at(codes, prev_day.isoformat())
    status = feeds.fetch_status(codes, exec_date)
    limits = feeds.day_limits(codes, exec_date, prev_close, status=status)

    suspended: dict = {}
    st: dict = {}
    for code in codes:
        if code in status.index:
            suspended[code] = bool(status.at[code, "is_suspended"])
            st[code] = bool(status.at[code, "is_st"])
        else:
            suspended[code] = True
            st[code] = True
            logger.warning(f"[quality_pool] 状态表缺 {code}，按停牌/ST 处置")

    return execution.ExecSnapshot(
        trade_date=exec_date, counts=counts, avail=avail, cash=snap0.cash,
        equity=snap0.total_assets, prices=prices, names=names,
        suspended=suspended, st=st, limits=limits)


def _record_trades(trades: List[execution.TradeIntent], exec_date: str) -> BatchResult:
    """映射原指令，事实供料与校验由批次 module 负责。"""
    if not trades:
        return BatchResult(abort=None)
    orders = [BatchOrder(code=t.code, name=t.name, side=t.side, qty=t.qty,
                         price=t.price, reason=t.reason)
              for t in trades]
    return execute_batch(orders, account=config.ACCOUNT, trade_date=exec_date,
                         path=config.POOL_LEDGER_PATH)


def _result_lines(result: BatchResult) -> List[str]:
    if result.abort:
        return [f"❌ 批次安全校验未过，本阶段零记账：{result.abort}"]
    return [oc.outcome_line for oc in result.outcomes]


def _settle_trades(plan: execution.ExecPlan, exec_date: str) -> List[str]:
    """先记账卖单，按实际现金与名额缩减原买单，禁止使用预计回款。"""
    sells = [t for t in plan.trades if t.side == "sell"]
    result = _record_trades(sells, exec_date)
    lines = _result_lines(result)
    if result.abort:
        return lines

    snap = derive(load_trades(config.POOL_LEDGER_PATH), as_of=exec_date)
    cash = snap.cash
    reserved = set(snap.held_counts())
    buys = []
    for t in plan.trades:
        if t.side != "buy":
            continue
        if t.code not in reserved and len(reserved) >= config.TOP_N:
            lines.append(f"- ⛔ BUY {t.code} 受阻（{t.reason}）：slot")
            continue
        qty = min(t.qty, execution.affordable_shares(cash, t.price))
        if qty < 100:
            lines.append(f"- ⛔ BUY {t.code} 受阻（{t.reason}）：cash/lot")
            continue
        buys.append(replace(t, qty=qty))
        cash -= qty * t.price
        reserved.add(t.code)
    lines += _result_lines(_record_trades(buys, exec_date))
    return lines


def run_execution(exec_date: str, dry_run: bool = False) -> str:
    """消费有效名单，执行并按台账核对退出；返回报告，零通知副作用。"""
    st = pool_state.load_state(config.STATE_PATH)
    signal = pool_state.pop_plan(st)
    if signal is not None:
        prev = trading_calendar.latest_trading_day_on_or_before(
            date.fromisoformat(exec_date) - timedelta(days=1))
        if prev is None:
            raise RuntimeError(f"交易日历无 {exec_date} 之前的数据")
        if signal.get("signal_day") != prev.isoformat():
            logger.warning("STALE_SIGNAL：计划信号日 %s 非昨日 %s，丢弃",
                           signal.get("signal_day"), prev)
            signal = None

    selected = list((signal or {}).get("selected", []))
    exits = dict(st.get("exit_queue", {}))
    if signal is not None:
        exits.update(signal["exit_reasons"])
        exits = {c: r for c, r in exits.items() if c not in set(selected)}
    snapshot = build_snapshot(exec_date, selected)
    per = snapshot.equity / config.TOP_N if signal is not None else None
    plan = (execution.plan_round(selected, exits, per, snapshot)
            if signal is not None else execution.plan_exit_retry(exits, snapshot))

    lines = [f"# 质量池执行（{exec_date}）"]
    lines.append(f"- 调仓轮：总权益 {snapshot.equity:,.0f}，单只预算 {per:,.0f}"
                 if per is not None else "- 非调仓轮：仅退出队列重试")
    if dry_run:
        for t in plan.trades:
            lines.append(f"- （dry-run）{t.side.upper()} {t.name}({t.code}) "
                         f"{t.qty}股 @ {t.price}（{t.reason}）")
    else:
        # 先消费名单并存完整退出意图；失败则零记账，重启也不重复整轮买入。
        held = derive(load_trades(config.POOL_LEDGER_PATH), as_of=exec_date).held_counts()
        st["exit_queue"] = {c: r for c, r in exits.items() if held.get(c, 0) > 0}
        pool_state.save_state(st, config.STATE_PATH)
        lines += _settle_trades(plan, exec_date) or ["- 无成台交易"]
        held = derive(load_trades(config.POOL_LEDGER_PATH), as_of=exec_date).held_counts()
        st["exit_queue"] = {c: r for c, r in exits.items() if held.get(c, 0) > 0}
        pool_state.save_state(st, config.STATE_PATH)
    for b in plan.blocked:
        lines.append(f"- ⛔ {b.action.upper()} {b.code} 受阻（{b.reason}）：{b.blocked_by}")
    return "\n".join(lines)
