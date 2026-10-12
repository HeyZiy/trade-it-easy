# -*- coding: utf-8 -*-
"""质量池信号轮次：周期、失败恢复、未知旧仓补判与名单提交。

run_signal 是调用 seam：调用者只给日期与只读/恢复选项，不掌握状态字段。
整轮取数前原子保存未完成标记，成功名单与完成标记一次提交；失败恢复
沿用原定周期。非调仓日的补判只更新未知旧仓及退出意图，不生成买入轮。

数据经 feeds 进入，质量规则由 screener 判定，状态文件读写仍由 state 负责。
路径运行时从 config 读取；报告落盘与推送由入口负责。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

import pandas as pd

import src.trading_calendar as trading_calendar
from src.quality_pool import config, feeds, screener, state as pool_state
from src.trade_ledger.ledger import derive, load_trades

logger = logging.getLogger(__name__)


def _prev_trading_day(day: date) -> date:
    """返回 day 之前的最近交易日。"""
    import datetime as dt
    prev = trading_calendar.latest_trading_day_on_or_before(day - dt.timedelta(days=1))
    if prev is None:
        raise RuntimeError(f"交易日历无 {day} 之前的数据")
    return prev


def _next_rebalance_day(day: date) -> date:
    """返回 day 之后第 ROTATE_EVERY 个交易日。"""
    import datetime as dt
    upcoming = trading_calendar.get_trading_dates(
        day + dt.timedelta(days=1),
        day + dt.timedelta(days=config.ROTATE_EVERY * 3 + 10))
    if len(upcoming) < config.ROTATE_EVERY:
        raise RuntimeError("交易日历不足以推算下一调仓日")
    return upcoming[config.ROTATE_EVERY - 1]


def run_signal(today: date, dry_run: bool = False, retry: bool = False) -> str:
    """处理一个信号日，返回报告；状态提交先于报告生成，零通知副作用。

    retry 与入口的手动恢复选项一致：只恢复持久化的失败轮，
    不提前开启未到期轮，也不重放已成功生成的当日名单。
    """
    today_str = today.isoformat()
    st = pool_state.load_state(config.STATE_PATH)
    next_day = st.get("next_signal_date")
    is_round_day = next_day is None or today_str >= next_day
    if st.get('last_signal_date') == today_str and not st.get('signal_retry'):
        return f'# 质量池信号（{today_str}）\n\n- 今日名单已生成，不重复本轮。\n'
    if not is_round_day and not st.get('signal_retry'):
        if st.get('deferred_codes'):
            return _review_deferred(today, st, dry_run)
        return f"# 质量池信号（{today_str}）\n\n- 非调仓日：下一调仓信号日 {next_day}\n"

    asof = _prev_trading_day(today).isoformat()
    lines: List[str] = [f"# 质量池信号（{today_str}）", f"- 财务/估值/波动/强弱截至 T−1：{asof}"]
    nxt = (_next_rebalance_day(today).isoformat() if is_round_day or next_day is None
           else next_day)
    # 取数前先持久化未完成标记；中断后只重算信号，不重放已消费的交易。
    st["next_signal_date"] = nxt
    st['signal_retry'] = True
    pool_state.put_plan(st, None)
    if not dry_run:
        pool_state.save_state(st, config.STATE_PATH)
    try:
        universe = feeds.fetch_universe(asof)
        codes = sorted(universe)
        closes = feeds.fetch_closes(codes, asof, rows=config.SCORE_CLOSES)
        if (closes.empty or len(closes) < config.SCORE_CLOSES
                or pd.Timestamp(closes.index[-1]).normalize() != pd.Timestamp(asof)):
            raise RuntimeError('收盘矩阵未完整更新至上一交易日')
        raw_close = feeds.raw_closes_at(codes, asof)
        fundamentals = feeds.fetch_fundamentals(codes, asof, raw_closes=raw_close)
        if fundamentals.empty or not fundamentals.notna().any().any():
            raise RuntimeError('财务快照为空或全部未知')
        status = feeds.fetch_status(codes, today_str)
        if status.empty:
            raise RuntimeError('状态快照为空')

        trades = load_trades(config.POOL_LEDGER_PATH)
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
        return "\n".join(lines + [
            f"- ⚠️ 数据异常，本轮暂停；下一交易日盘前自动重试，原定节点 {nxt}：{exc}"])

    rank = {c: i + 1 for i, c in enumerate(ranked)}
    exit_reasons = {c: config.EXIT_OUT_OF_POOL for c in sorted(held)
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
        pool_state.save_state(st, config.STATE_PATH)

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
    return "\n".join(lines)


def _review_deferred(today: date, st: dict, dry_run: bool) -> str:
    """非调仓日仅补判缺数据旧仓；不重新排名/开仓/再平衡。"""
    today_str = today.isoformat()
    asof = _prev_trading_day(today).isoformat()
    held = {p['code'] for p in derive(load_trades(config.POOL_LEDGER_PATH), as_of=today_str).positions}
    codes = sorted(set(st['deferred_codes']) & held)
    audit = {c: {} for c in codes}
    if codes:
        try:
            universe = feeds.fetch_universe(asof)
            closes = feeds.fetch_closes(codes, asof, rows=config.SCORE_CLOSES)
            if closes.empty or pd.Timestamp(closes.index[-1]).normalize() != pd.Timestamp(asof):
                raise RuntimeError('补判行情未更新至截止日')
            raw_close = feeds.raw_closes_at(codes, asof)
            fundamentals = feeds.fetch_fundamentals(codes, asof, raw_closes=raw_close)
            status = feeds.fetch_status(codes, today_str)
            screener.build_pool(set(codes) & universe, fundamentals, status, closes, audit=audit)
        except Exception as exc:
            return f'# 质量池补判（{today_str}）\n- 数据仍不可用，持仓暂不调整：{exc}'
    unresolved = []
    for code in codes:
        state = audit[code]['pool_state']
        if state == 'failed':
            st['exit_queue'][code] = config.EXIT_OUT_OF_POOL
        elif state == 'unknown':
            unresolved.append(code)
        else:
            st['exit_queue'].pop(code, None)
    st['deferred_codes'] = unresolved
    if not dry_run:
        pool_state.save_state(st, config.STATE_PATH)
    return (f'# 质量池补判（{today_str}）\n'
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
