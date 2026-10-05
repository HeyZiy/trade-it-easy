# -*- coding: utf-8 -*-
"""
===================================
质量池轮动 — 模拟盘编排入口（signal / execute 双任务）
===================================

规格唯一来源：strategy/roe_quality_pool.md。持仓与资金事实来源是独立名义
台账（src/quality_pool/config.py POOL_LEDGER_PATH），影子成交、无真实下单。

两个子命令（deploy/crontab.server 各一条 cron）：

- signal（每交易日 15:10 盘后）：
  到达调仓日（每 20 个交易日）时按 T−1 财务/估值/波动/强弱生成名单，
  冻结待执行计划；非调仓日或数据异常（整轮暂停）只推进日程。
- execute（每交易日 09:31 开盘）：
  有待执行计划则整轮先卖后买（预算 = 总权益/20 冻结），否则只重试退出
  队列；全部按 09:31 实时价影子记账，渲染报告 → 落盘 + 推送。

约定：
- 信号时钟沿用 ROE 研究：T 收盘涨跌不参与本轮，质量/估值/波动/强弱截至
  T−1，ST/停牌与涨跌停用执行时点当时信息；
- 池内旧仓缺失排序行情 → 整轮暂停（规格二节 5）；
- 买入或等权调整未完成不逐日追补，下一调仓轮重新判断（规格四节表）。
"""

import argparse
import io
import logging
import sys
from datetime import date
from typing import Dict, List, Optional

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import (is_trading_day, latest_trading_day_on_or_before)

setup_env()

from src.quality_pool import execution, feeds, screener  # noqa: E402
from src.quality_pool.config import (  # noqa: E402
    ACCOUNT, EXIT_OUT_OF_POOL, EXIT_RANK_BELOW_BUFFER, POOL_LEDGER_PATH,
    REPORT_PREFIX, ROTATE_EVERY, SCORE_CLOSES, STATE_PATH, TOP_N,
)
from src.quality_pool import state as pool_state  # noqa: E402
from src.task_io import notify, save_report       # noqa: E402
from src.trade_ledger import execute_batch  # noqa: E402  (BatchResult 供报告行)
from src.trade_ledger.batch import BatchOrder  # noqa: E402
from src.trade_ledger.ledger import derive, load_trades  # noqa: E402

logger = logging.getLogger(__name__)


def _prev_trading_day(day: date) -> date:
    """day 的前一交易日（latest_trading_day_on_or_before(day) 为 day 本身或更早，
    先取 ≤ day 再退一天：调用方保证 day 是交易日，故取 ≤ day-1）。"""
    import datetime as dt
    prev = latest_trading_day_on_or_before(day - dt.timedelta(days=1))
    if prev is None:
        raise RuntimeError(f"交易日历无 {day} 之前的数据")
    return prev


def _next_rebalance_day(day: date) -> date:
    """day（交易日）起第 ROTATE_EVERY 个交易日。"""
    import datetime as dt
    from src.trading_calendar import get_trading_dates
    upcoming = get_trading_dates(day + dt.timedelta(days=1),
                                 day + dt.timedelta(days=ROTATE_EVERY * 3 + 10))
    if len(upcoming) < ROTATE_EVERY:
        raise RuntimeError("交易日历不足以推算下一调仓日")
    return upcoming[ROTATE_EVERY - 1]


def _snapshot(exec_date: str, selected: List[str]) -> execution.ExecSnapshot:
    """09:31 执行时点快照：台账派生 + 实时价 + 当日状态/涨跌停。"""
    import datetime as dt
    trades = load_trades(POOL_LEDGER_PATH)
    codes_held = list({t.code for t in trades})
    prices, names = feeds.fetch_realtime_quotes(sorted(set(codes_held) | set(selected)))
    snap0 = derive(trades, as_of=exec_date, prices=prices)
    counts = {p["code"]: int(p["count"]) for p in snap0.positions}
    avail = {p["code"]: int(p["avail_count"]) for p in snap0.positions}
    equity = snap0.cash + sum(counts.get(c, 0) * prices.get(c, 0.0)
                              for c in counts)
    prev_day = _prev_trading_day(dt.date.fromisoformat(exec_date))
    prev_close = feeds.raw_closes_at(sorted(set(counts) | set(selected)),
                                     prev_day.isoformat())
    limits = feeds.day_limits(sorted(set(counts) | set(selected)),
                              exec_date, prev_close)
    status = feeds.fetch_status(sorted(set(counts) | set(selected)), exec_date)
    suspended = {c: bool(status.at[c, "is_suspended"])
                 for c in status.index if c in set(counts) | set(selected)}
    st = {c: (str(names.get(c, "")).upper().find("ST") >= 0)
          for c in set(counts) | set(selected)}
    return execution.ExecSnapshot(
        trade_date=exec_date, counts=counts, avail=avail, cash=snap0.cash,
        equity=equity, prices=prices, names=names, suspended=suspended,
        st=st, limits=limits)


def _record(plan: execution.ExecPlan, exec_date: str) -> List[str]:
    """成台影子记账：批次语义（收敛/安全校验/先卖后买/逐单隔离）在 execute_batch。"""
    if not plan.trades:
        return []
    snap = derive(load_trades(POOL_LEDGER_PATH), as_of=exec_date)
    held = {p["code"]: int(p["count"]) for p in snap.positions}
    orders = [BatchOrder(code=t.code, name=t.name, side=t.side, qty=t.qty,
                         price=t.price, reason=t.reason)
              for t in plan.trades]
    res = execute_batch(orders, account=ACCOUNT, held_counts=held,
                        cash=snap.cash, trade_date=exec_date,
                        path=POOL_LEDGER_PATH)
    if res.abort:
        return [f"❌ 批次安全校验未过，整批零记账：{res.abort}"]
    return [oc.outcome_line for oc in res.outcomes]


# ── signal ──

def run_signal(today: date, dry_run: bool = False) -> str:
    """调仓日盘后生成名单；其余情形只推进日程并说明。"""
    today_str = today.isoformat()
    st = pool_state.load_state(STATE_PATH)
    next_day = st.get("next_signal_date")
    is_round_day = next_day is None or today_str >= next_day
    if not is_round_day:
        return _finish_report(
            f"# 质量池信号（{today_str}）\n\n- 非调仓日：下一调仓信号日 {next_day}\n")

    asof = _prev_trading_day(today).isoformat()
    lines: List[str] = [f"# 质量池信号（{today_str}）", f"- 财务/估值/波动/强弱截至 T−1：{asof}"]
    try:
        universe = feeds.fetch_universe(today_str)
        codes = sorted(universe)
        closes = feeds.fetch_closes(codes, asof, rows=SCORE_CLOSES)
        raw_close = feeds.raw_closes_at(codes, asof)
        fundamentals = feeds.fetch_fundamentals(codes, asof, raw_closes=raw_close)
        status = feeds.fetch_status(codes, today_str)
        unlock = feeds.fetch_unlock_codes(codes, today_str)

        trades = load_trades(POOL_LEDGER_PATH)
        held = {p["code"] for p in derive(trades, as_of=today_str).positions}

        audit: Dict[str, dict] = {c: {} for c in held}
        pool = screener.build_pool(universe, fundamentals, status, closes,
                                   unlock, audit=audit)
        ranked, scores = screener.rank_pool(pool, closes)
        missing = held & (set(pool) - set(ranked))
        if missing:
            raise RuntimeError(f"池内持仓缺失排序行情，整轮暂停：{sorted(missing)}")
        selected, kept = screener.select_targets(ranked, held)
    except Exception as exc:
        # 整轮暂停：维持持仓，日程照常推进（规格「运行与记录」的数据异常提醒）
        logger.exception("调仓名单生成失败")
        nxt = _next_rebalance_day(today).isoformat()
        st["next_signal_date"] = nxt
        pool_state.put_plan(st, None)
        if not dry_run:
            pool_state.save_state(st, STATE_PATH)
        return _finish_report("\n".join(lines + [
            f"- ⚠️ 数据异常，本轮暂停（持仓不动，日程推进至 {nxt}）：{exc}"]))

    rank = {c: i + 1 for i, c in enumerate(ranked)}
    exit_reasons = {c: (EXIT_OUT_OF_POOL if c not in set(pool)
                        else EXIT_RANK_BELOW_BUFFER)
                    for c in sorted(held - set(selected))}
    decisions = []
    for code in sorted(held | set(selected)):
        decisions.append({
            "code": code,
            "action": ("keep" if code in held else "entry")
                      if code in selected else "exit",
            "rank": rank.get(code), "score": scores.get(code),
            "exit_reason": exit_reasons.get(code, ""),
            "pool_reason": (audit.get(code, {}) or {}).get("pool_reason", ""),
        })
    plan = {"signal_day": today_str, "asof": asof, "selected": selected,
            "kept": kept, "exit_reasons": exit_reasons, "decisions": decisions}

    nxt = _next_rebalance_day(today).isoformat()
    st["next_signal_date"] = nxt
    pool_state.put_plan(st, plan)
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)

    lines.append(f"- 质量池 {len(pool)} 只 | 可排名 {len(ranked)} 只 | "
                 f"留存 {len(kept)} | 目标 {len(selected)} | 计划退出 {len(exit_reasons)}")
    lines.append(f"- 下一调仓信号日 {nxt}；明日 09:31 先卖后买")
    for d in decisions:
        mark = {"keep": "🟢", "entry": "🛒", "exit": "🔴"}.get(d["action"], "·")
        lines.append(f"  - {mark} {d['code']} #{d['rank']} "
                     f"score={d['score'] if d['score'] is None else round(d['score'], 4)} "
                     f"{d['exit_reason'] or d['action']}"
                     + (f"（{d['pool_reason']}）" if d["pool_reason"] else ""))
    return _finish_report("\n".join(lines))


# ── execute ──

def run_execute(today: date, dry_run: bool = False) -> str:
    """09:31：有待执行计划整轮先卖后买；否则只重试退出队列。"""
    today_str = today.isoformat()
    st = pool_state.load_state(STATE_PATH)
    plan = pool_state.pop_plan(st)

    if plan is not None:
        prev = _prev_trading_day(today).isoformat()
        if plan.get("signal_day") != prev:
            logger.warning("STALE_SIGNAL：计划信号日 %s 非昨日 %s，丢弃",
                           plan.get("signal_day"), prev)
            plan = None

    snapshot = _snapshot(today_str, list((plan or {}).get("selected", [])))
    if plan is not None:
        per = snapshot.equity / TOP_N
        exec_plan = execution.plan_round(plan["selected"], plan["exit_reasons"],
                                         per, snapshot)
    else:
        per = None
        exec_plan = execution.plan_exit_retry(st.get("exit_queue", {}), snapshot)

    lines = [f"# 质量池执行（{today_str}）"]
    if per is not None:
        lines.append(f"- 调仓轮：总权益 {snapshot.equity:,.0f}，单只预算 {per:,.0f}")
    else:
        lines.append("- 非调仓轮：仅退出队列重试")
    if dry_run:
        for t in exec_plan.trades:
            lines.append(f"- （dry-run）{t.side.upper()} {t.name}({t.code}) "
                         f"{t.qty}股 @ {t.price}（{t.reason}）")
    else:
        lines += _record(exec_plan, today_str) or ["- 无成台交易"]
    for b in exec_plan.blocked:
        lines.append(f"- ⛔ {b.action.upper()} {b.code} 受阻（{b.reason}）：{b.blocked_by}")

    st["exit_queue"] = exec_plan.exit_queue
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)
    return _finish_report("\n".join(lines))


def _finish_report(report: str) -> str:
    path = save_report(report, REPORT_PREFIX)
    notify(report)
    logger.info(f"报告已保存并推送: {path}")
    return report


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description="质量池轮动模拟盘")
    parser.add_argument("command", choices=["signal", "execute"])
    parser.add_argument("--dry-run", action="store_true",
                        help="只出报告与计划，不写状态/台账")
    args = parser.parse_args()
    setup_logging()

    today = date.today()
    if not is_trading_day(today):
        logger.info(f"{today} 非交易日，跳过")
        return 0
    if args.command == "signal":
        run_signal(today, dry_run=args.dry_run)
    else:
        run_execute(today, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
