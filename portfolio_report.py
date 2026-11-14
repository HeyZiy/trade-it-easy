"""次日盘前采集各账户上一交易日收盘成绩；不改持仓、不下单、不发送通知、不生成日报。

默认运行还会回补目标日之前最近 5 个交易日的不完整记录；手动 --date 只采指定日。
采集结果经各账户台账的 performance 记录供 dashboard 展示；异常与缺价走日志告警。
"""

import argparse
from datetime import datetime, timedelta, timezone
from functools import partial
import logging
import math

from src.config import setup_env
from src.cron_entry import hard_exit
from src.logging_config import setup_logging
from src.trading_calendar import get_trading_dates
from src.trade_ledger.closing_prices import ClosingPrices
from src.trade_ledger.ledger import load_trades
from src.trade_ledger.performance import (
    collect_close, load_performance, record_matches_trades, render_summary,
)

logger = logging.getLogger(__name__)
SHANGHAI = timezone(timedelta(hours=8))
BACKFILL_TRADING_DAYS = 5


def closed_date(now):
    """只接受交易日历确认、且已结束的交易日；盘中不得记录当日收盘。"""
    now = now.astimezone(SHANGHAI)
    end = now.date() if (now.hour, now.minute) >= (15, 20) else now.date() - timedelta(days=1)
    days = get_trading_dates(end - timedelta(days=40), end)
    if not days:
        raise ValueError("交易日历不可用，暂停收盘采集")
    return max(days)


def _complete(row):
    """净值有效且没有缺价/基准告警；只有这种记录不再重采。"""
    nav = (row or {}).get("nav")
    return (isinstance(nav, (int, float)) and math.isfinite(nav) and nav > 0
            and not row.get("issues"))


def _record(state, day):
    return next((r for r in state.get("records", []) if r["date"] == day), None)


def _backfill_dates(target):
    """目标日之前最近 5 个交易日（含目标日，窗口内前后顺序不变）。"""
    days = get_trading_dates(target - timedelta(days=40), target)
    return days[-BACKFILL_TRADING_DAYS:]


def _collect(path, day, benchmark, feed, dry_run):
    return collect_close(path, day.isoformat(), benchmark,
                         close_prices=feed.close_prices, index_prices=feed.index_prices,
                         trading_dates=get_trading_dates, dry_run=dry_run)


def _collect_dates(state, target, window, *, first_day, collect, matches, label,
                   refresh_target=False):
    """补齐窗口内缺失、未完成或已失效的成绩，再处理目标日。"""
    healed, lingering = [], []
    for day in window:
        if day >= target or (first_day and day.isoformat() < first_day):
            continue
        row = _record(state, day.isoformat())
        if _complete(row) and matches(state, row):
            continue
        try:
            state = collect(day)
        except Exception:
            logger.exception("[回补] %s %s 采集失败", label, day)
            lingering.append(f"{day}（采集失败）")
            continue
        row = _record(state, day.isoformat())
        if _complete(row) and matches(state, row):
            healed.append(str(day))
        elif row is not None:
            lingering.append(f"{day}（{'；'.join(row.get('issues', [])) or '估值无效'}）")
        else:
            lingering.append(f"{day}（未生成记录）")
    row = _record(state, target.isoformat())
    if refresh_target or not _complete(row) or not matches(state, row):
        state = collect(target)
    return state, healed, lingering


def _backfill_notes(notes, label, healed, lingering):
    if healed:
        notes.append(f"- {label} 已补齐：{'、'.join(healed)}")
    if lingering:
        notes.append(f"- {label} 仍不完整：{'、'.join(lingering)}")
        logger.warning("[回补] %s 仍不完整：%s", label, "、".join(lingering))


def run(*, asof=None, dry_run=False, now=None, backfill=False):
    from src.account_sources import all_sources

    now = now or datetime.now(SHANGHAI)
    today = now.astimezone(SHANGHAI).date()
    latest = closed_date(now)
    target = datetime.strptime(asof, "%Y-%m-%d").date() if asof else latest
    if target > latest:
        raise ValueError("目标日期尚未收盘，不保存盘中成绩")
    if target not in get_trading_dates(target, target):
        raise ValueError("目标日期不是已确认交易日")
    feed = ClosingPrices()
    accounts = all_sources()
    from src.etf_account import (
        collect_close as collect_core_close, load as load_core, refresh_close_prices,
        refresh_fund_navs, render_summary as core_summary, revision, valuation_day,
    )
    from src.fund_plans import record_due_subscriptions
    sections = [f"# 账户收盘采集 — {target}", ""]
    notes = []
    errors = 0
    window = _backfill_dates(target) if backfill else []
    for account in accounts:
        path, label, benchmark = (account.ledger_path, account.label,
                                  account.benchmark)
        if account.is_investment:
            try:
                state = load_core(path)
                if asof is None and not dry_run:
                    try:
                        funds = refresh_fund_navs(path, today.isoformat())
                        if funds["updated"]:
                            logger.info("场外基金净值已更新 %s 只", len(funds["updated"]))
                        if funds["failed"]:
                            logger.warning("场外净值未能更新，保留已有净值：%s", "、".join(funds["failed"]))
                    except Exception:
                        logger.exception("每日场外净值更新失败，保留已有净值")
                    try:
                        # 定投补记放在净值刷新与份额确认之后：同一轮不确认本轮扣款，保留全天作废窗口。
                        recorded = record_due_subscriptions(path)
                        if recorded:
                            logger.info("定投补记 %s 笔待确认申购", len(recorded))
                    except Exception:
                        logger.exception("定投补记失败，跳过本轮（下次运行自动补上）")
                    state = load_core(path)
                first_day = datetime.fromisoformat(state["initial"]["date"]).date() + timedelta(days=1)
                state, healed, lingering = _collect_dates(
                    state, target, window, first_day=first_day.isoformat(), label=label,
                    collect=lambda day: collect_core_close(path, day.isoformat(), feed.close_prices,
                                                          dry_run=dry_run),
                    matches=lambda current, row: row.get("revision") == revision(current, row["date"]),
                    refresh_target=True,
                )
                _backfill_notes(notes, label, healed, lingering)
                if asof is None and not dry_run:
                    try:
                        missing = refresh_close_prices(path, today, valuation_day(today), feed.close_prices)
                        if missing:
                            logger.warning("[账户估值] 收盘价缺失：%s", "、".join(missing))
                    except Exception:
                        logger.exception("盘前账户估值补价失败")
                if target.isoformat() >= state["initial"]["date"]:
                    sections.append(core_summary(path))
                row = _record(state, target.isoformat())
                if row and row.get("issues"):
                    errors += 1
                    logger.warning("[收盘提示] 投资账户 %s：%s", target, "；".join(row["issues"]))
                    sections.append("## 投资账户估值提示\n\n" + "；".join(row["issues"]))
            except FileNotFoundError:
                sections.append("## 投资账户\n\n尚未初始化，暂不生成成绩。")
                errors += 1
            except Exception:
                logger.exception("投资账户收盘采集失败，保留已有估值")
                sections.append("## 投资账户\n\n本次估值失败，保留已有成绩。")
                errors += 1
            continue
        try:
            state = load_performance(path)
            trades = load_trades(path)
            first_day = trades[0].date if trades else target.isoformat()
            state, healed, lingering = _collect_dates(
                state, target, window, first_day=first_day, label=label,
                collect=partial(_collect, path, benchmark=benchmark, feed=feed, dry_run=dry_run),
                matches=lambda _, row: record_matches_trades(trades, row),
                refresh_target=not backfill,
            )
            sections.append(render_summary(path, today=target, label=label, state=state))
            row = _record(state, target.isoformat())
            if row and row["issues"]:
                errors += 1
                logger.warning("[收盘提示] %s %s：%s", label, target, "；".join(row["issues"]))
            _backfill_notes(notes, label, healed, lingering)
        except Exception:
            logger.exception("[收盘成绩降级] %s 采集失败，保留已有成绩", label)
            sections.append(f"## {label}\n\n⚠️ 本次收盘采集失败，保留已有成绩。\n\n"
                            + render_summary(path, today=target, label=label))
            errors += 1
    if notes:
        sections += ["## 本次回补（目标日之前）", "", *notes]
    if dry_run:
        sections.append("试运行：未保存收盘记录。")
    return "\n\n".join(sections), errors


def main():
    parser = argparse.ArgumentParser(description="采集各策略账户的完整收盘成绩")
    parser.add_argument("--date", help="补采已收盘交易日，YYYY-MM-DD（只采指定日）")
    parser.add_argument("--dry-run", action="store_true", help="仅展示，不保存记录")
    args = parser.parse_args()
    setup_env()
    setup_logging(log_prefix="portfolio_report")
    report, errors = run(asof=args.date, dry_run=args.dry_run, backfill=args.date is None)
    if args.dry_run:
        # 预览采集结果；正式运行静默采集——成绩看板在 dashboard，不再生成收盘日报。
        import io
        import sys
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        print("\n" + report)
    return 1 if errors else 0


if __name__ == "__main__":
    hard_exit(main())
