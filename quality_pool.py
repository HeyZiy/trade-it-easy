# -*- coding: utf-8 -*-
"""
===================================
质量池相对强弱轮动 — 盘前信号 / 09:31 执行
===================================

规格唯一来源：strategy/roe_quality_pool.md。持仓与资金事实来源是独立名义
台账（src/quality_pool/config.py POOL_LEDGER_PATH），模拟记账、无真实下单。

两个子命令（deploy/crontab.server 各一条 cron）：

- signal（每交易日 08:00 盘前）：
  每20交易日用上一交易日快照生成当日名单；全批失败次日自动重算，
  不移动原定周期。个别数据未知旧仓每天补判，不触发新的买入轮。
- execute（每交易日 09:31 开盘）：
  有当日计划则整轮先卖后买（预算 = 总权益/6 冻结），否则只重试退出
  队列；全部按 09:31 实时价模拟记账（ST/停牌/涨跌停以执行时点状态表
  为准），渲染报告 → 落盘 + 推送。真实账户由人工另行执行，不回流本系统。

约定：
- 6只、ROE_TTM、12-2强弱、行业约束；只按质量池退出，无排名/解禁退出；
- 停牌只限制交易；未知数据保留旧仓且不增减，确认失败才退出；
- 买入与等权调整仅在调仓日09:31执行一次，不追补买入。
"""

import argparse
import io
import logging
import os
import sys
from datetime import date
from typing import Dict, List, Optional
import pandas as pd

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import (is_trading_day, latest_trading_day_on_or_before)

setup_env()

from src.quality_pool import assembly, feeds, screener  # noqa: E402
from src.quality_pool.config import (  # noqa: E402
    EXIT_OUT_OF_POOL, POOL_LEDGER_PATH,
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
    """盘前生成当日名单，或恢复故障/补判未知旧仓。"""
    today_str = today.isoformat()
    st = pool_state.load_state(STATE_PATH)
    next_day = st.get("next_signal_date")
    is_round_day = next_day is None or today_str >= next_day
    if st.get('last_signal_date') == today_str and not st.get('signal_retry'):
        return _finish_report(f'# 质量池信号（{today_str}）\n\n- 今日名单已生成，不重复本轮。\n')
    if not is_round_day and not st.get('signal_retry'):
        if st.get('deferred_codes'):
            return _review_deferred(today, st, dry_run)
        return _finish_report(
            f"# 质量池信号（{today_str}）\n\n- 非调仓日：下一调仓信号日 {next_day}\n")

    asof = _prev_trading_day(today).isoformat()
    lines: List[str] = [f"# 质量池信号（{today_str}）", f"- 财务/估值/波动/强弱截至 T−1：{asof}"]
    nxt = (_next_rebalance_day(today).isoformat() if is_round_day or next_day is None
           else next_day)
    # 取数前先持久化未完成标记；中断后只重算信号，不重放已消费的交易。
    st["next_signal_date"] = nxt
    st['signal_retry'] = True
    pool_state.put_plan(st, None)
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)
    try:
        universe = feeds.fetch_universe(today_str)
        codes = sorted(universe)
        closes = feeds.fetch_closes(codes, asof, rows=SCORE_CLOSES)
        if (closes.empty or len(closes) < SCORE_CLOSES
                or pd.Timestamp(closes.index[-1]).normalize() != pd.Timestamp(asof)):
            raise RuntimeError('收盘矩阵未完整更新至上一交易日')
        raw_close = feeds.raw_closes_at(codes, asof)
        fundamentals = feeds.fetch_fundamentals(codes, asof, raw_closes=raw_close)
        if fundamentals.empty or not fundamentals.notna().any().any():
            raise RuntimeError('财务快照为空或全部未知')
        status = feeds.fetch_status(codes, today_str)
        if status.empty:
            raise RuntimeError('状态快照为空')

        trades = load_trades(POOL_LEDGER_PATH)
        held = {p["code"] for p in derive(trades, as_of=today_str).positions}

        audit: Dict[str, dict] = {c: {} for c in held}
        pool = screener.build_pool(universe, fundamentals, status, closes, audit=audit)
        ranked, scores = screener.rank_pool(pool, closes)
        deferred = {c for c in held if audit[c]['pool_state'] == 'unknown'}
        industries = feeds.fetch_industries(sorted(set(ranked) | held), asof)
        # 无法重新确认质量时，不撤销此前已确认的退出计划。
        preserved_exits = set(st.get('exit_queue', {})) & deferred
        selected, kept = screener.select_targets(
            ranked, held, set(pool), industries, deferred - preserved_exits)
    except Exception as exc:
        # 恢复标记已经落盘；取数/日志中断不丢失失败轮，也不挪动原定周期。
        logger.exception("调仓名单生成失败")
        return _finish_report("\n".join(lines + [
            f"- ⚠️ 数据异常，本轮暂停；下一交易日盘前自动重试，原定节点 {nxt}：{exc}"]))

    rank = {c: i + 1 for i, c in enumerate(ranked)}
    exit_reasons = {c: EXIT_OUT_OF_POOL for c in sorted(held)
                    if audit[c]['pool_state'] == 'failed'}
    exit_reasons.update({c: st['exit_queue'][c] for c in preserved_exits})
    no_adjust = deferred | {c for c in kept if not industries.get(c)}
    decisions = []
    for code in sorted(held | set(selected)):
        decisions.append({
            "code": code,
            "action": ('defer' if code in no_adjust else "keep" if code in held else "entry")
                      if code in selected else "exit",
            "rank": rank.get(code), "score": scores.get(code),
            "exit_reason": exit_reasons.get(code, ""),
            "pool_reason": (audit.get(code, {}) or {}).get("pool_reason", ""),
        })
    plan = {"signal_day": today_str, "execute_day": today_str,
            "asof": asof, "selected": selected, 'deferred': sorted(no_adjust),
            'industries': industries,
            "kept": kept, "exit_reasons": exit_reasons, "decisions": decisions}

    pool_state.put_plan(st, plan)
    st['signal_retry'] = False
    st['last_signal_date'] = today_str
    st['deferred_codes'] = sorted(deferred)
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)

    lines.append(f"- 质量池 {len(pool)} 只 | 可排名 {len(ranked)} 只 | "
                 f"留存 {len(kept)} | 目标 {len(selected)} | 计划退出 {len(exit_reasons)}")
    lines.append(f"- 下一调仓信号日 {nxt}；今日 09:31 先卖后买；暂缓判断 {len(deferred)} 只")
    names = _decision_names(decisions)
    for d in decisions:
        mark = {"keep": "🟢", "entry": "🛒", "exit": "🔴", 'defer': '⏸️'}.get(d["action"], "·")
        name = names.get(d["code"], "")
        lines.append(f"  - {mark} {d['code']}"
                     + (f" {name}" if name else "")
                     + f" #{d['rank']} "
                     f"score={d['score'] if d['score'] is None else round(d['score'], 4)} "
                     f"{d['exit_reason'] or d['action']}"
                     + (f"（{d['pool_reason']}）" if d["pool_reason"] else ""))
    return _finish_report("\n".join(lines))


def _review_deferred(today: date, st: dict, dry_run: bool) -> str:
    """非调仓日仅补判缺数据旧仓；不重新排名/开仓/再平衡。"""
    today_str = today.isoformat()
    asof = _prev_trading_day(today).isoformat()
    held = {p['code'] for p in derive(load_trades(POOL_LEDGER_PATH), as_of=today_str).positions}
    codes = sorted(set(st['deferred_codes']) & held)
    audit = {c: {} for c in codes}
    if codes:
        try:
            universe = feeds.fetch_universe(today_str)
            closes = feeds.fetch_closes(codes, asof, rows=SCORE_CLOSES)
            if closes.empty or pd.Timestamp(closes.index[-1]).normalize() != pd.Timestamp(asof):
                raise RuntimeError('补判行情未更新至截止日')
            raw_close = feeds.raw_closes_at(codes, asof)
            fundamentals = feeds.fetch_fundamentals(codes, asof, raw_closes=raw_close)
            status = feeds.fetch_status(codes, today_str)
            screener.build_pool(set(codes) & universe, fundamentals, status, closes, audit=audit)
        except Exception as exc:
            return _finish_report(f'# 质量池补判（{today_str}）\n- 数据仍不可用，持仓暂不调整：{exc}')
    unresolved = []
    for code in codes:
        state = audit[code]['pool_state']
        if state == 'failed':
            st['exit_queue'][code] = EXIT_OUT_OF_POOL
        elif state == 'unknown':
            unresolved.append(code)
        else:
            st['exit_queue'].pop(code, None)
    st['deferred_codes'] = unresolved
    if not dry_run:
        pool_state.save_state(st, STATE_PATH)
    return _finish_report(f'# 质量池补判（{today_str}）\n'
                          f'- 已检查 {len(codes)} 只；仍未知 {len(unresolved)} 只；'
                          '不重建买入名单，确认出池者今日09:31退出。')


def _decision_names(decisions: List[dict]) -> Dict[str, str]:
    """决策码的简称（实时行情，fail-soft；取不到就只显代码）。"""
    try:
        _, names = feeds.fetch_realtime_quotes(
            sorted({d["code"] for d in decisions}))
    except Exception:
        logger.exception("信号报告简称抓取失败，降级为只显代码")
        return {}
    return names


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
                        help="手动重试未完成信号，保留原定周期；已成功生成的当日名单不重放")
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
