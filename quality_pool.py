# -*- coding: utf-8 -*-
"""
===================================
质量池轮动 — 模拟盘编排入口（signal / execute 双任务）
===================================

规格唯一来源：strategy/roe_quality_pool.md。持仓与资金事实来源是独立名义
台账（src/quality_pool/config.py POOL_LEDGER_PATH），模拟记账、无真实下单。

两个子命令（deploy/crontab.server 各一条 cron）：

- signal（每交易日 15:10 盘后）：
  到达调仓日（每 20 个交易日）时按 T−1 财务/估值/波动/强弱生成名单，
  冻结待执行计划；非调仓日或数据异常（整轮暂停）只推进日程。
- execute（每交易日 09:31 开盘）：
  有待执行计划则整轮先卖后买（预算 = 总权益/20 冻结），否则只重试退出
  队列；全部按 09:31 实时价模拟记账（ST/停牌/涨跌停以执行时点状态表
  为准），渲染报告 → 落盘 + 推送。真实账户由人工另行执行，不回流本系统。

约定：
- 信号时钟沿用 ROE 研究：T 收盘涨跌不参与本轮，质量/估值/波动/强弱截至
  T−1，ST/停牌与涨跌停用执行时点当时信息；
- 池内旧仓缺失排序行情 → 整轮暂停（规格二节 5）；
- 买入或等权调整未完成不逐日追补，下一调仓轮重新判断（规格四节表）。
"""

import argparse
import io
import logging
import os
import sys
from datetime import date
from typing import Dict, List, Optional

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import (is_trading_day, latest_trading_day_on_or_before)

setup_env()

from src.quality_pool import assembly, feeds, screener  # noqa: E402
from src.quality_pool.config import (  # noqa: E402
    EXIT_OUT_OF_POOL, EXIT_RANK_BELOW_BUFFER, POOL_LEDGER_PATH,
    REPORT_PREFIX, ROTATE_EVERY, SCORE_CLOSES, STATE_PATH,
)
from src.quality_pool import state as pool_state  # noqa: E402
from src.task_io import notify, save_report       # noqa: E402
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


# ── signal ──

def run_signal(today: date, dry_run: bool = False, retry: bool = False) -> str:
    """调仓日盘后生成名单；其余情形只推进日程并说明。"""
    today_str = today.isoformat()
    st = pool_state.load_state(STATE_PATH)
    next_day = st.get("next_signal_date")
    is_round_day = next_day is None or today_str >= next_day
    if not is_round_day and not retry:
        return _finish_report(
            f"# 质量池信号（{today_str}）\n\n- 非调仓日：下一调仓信号日 {next_day}\n")

    asof = _prev_trading_day(today).isoformat()
    lines: List[str] = [f"# 质量池信号（{today_str}）", f"- 财务/估值/波动/强弱截至 T−1：{asof}"]
    nxt = _next_rebalance_day(today).isoformat()
    # 先持久化本轮暂停的安全状态。磁盘不足导致保存失败时不开始取数；
    # SDK 崩溃/OOM/进程中断也不会使下一日 cron 自动重放整轮。
    st["next_signal_date"] = nxt
    pool_state.put_plan(st, None)
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)
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
        # 安全状态已经落盘，不依赖失败后的日志/报告写入来推进日程。
        logger.exception("调仓名单生成失败")
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
    """09:31 执行闭环由质量池 module 负责，入口只保存和推送报告。"""
    return _finish_report(assembly.run_execution(today.isoformat(), dry_run=dry_run))


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
    parser.add_argument("--force", action="store_true",
                        help="跳过交易日检查（手动调试用；非交易日一律只读不记账）")
    parser.add_argument("--retry-signal", action="store_true",
                        help="修复数据故障后手动重跑信号，跳过调仓日检查并重设本轮日程")
    args = parser.parse_args()
    if args.retry_signal and args.command != "signal":
        parser.error("--retry-signal 仅适用于 signal")
    setup_logging()

    today = date.today()
    trading = is_trading_day(today)
    if not args.force and not trading:
        logger.info(f"{today} 非交易日，跳过")
        return 0

    # 非交易日不可能有真实成交，硬记账就是假日假账 → --force 试跑一律只读
    dry_run = args.dry_run or not trading
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不写状态/台账")

    if args.command == "signal":
        if args.retry_signal:
            run_signal(today, dry_run=dry_run, retry=True)
        else:
            run_signal(today, dry_run=dry_run)
    else:
        run_execute(today, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    # AmazingData/TGW SDK 启动了非守护 SWIG 回调线程，正常 sys.exit 会
    # 等不到该线程导致进程挂住（terminate 时打 Swig::DirectorMethodException）。
    # 主流程（信号/执行/报告/飞书推送）已全部完成后用 os._exit 直接终止进程，
    # 残留 C 线程随进程一起被回收。
    os._exit(main())
