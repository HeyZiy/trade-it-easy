"""次日盘前采集上一交易日模拟成绩；只写成绩记录，不改交易台账、不下单、不发送通知。

默认运行还会回补目标日之前最近 5 个交易日的不完整记录；手动 --date 只采指定日。
"""

import argparse
from datetime import datetime, timedelta, timezone
import logging
import math

from src.config import setup_env
from src.cron_entry import hard_exit
from src.logging_config import setup_logging
from src.trading_calendar import get_trading_dates
from src.trade_ledger.closing_prices import ClosingPrices
from src.trade_ledger.performance import collect_close, load_performance, render_summary

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


def run(*, asof=None, dry_run=False, now=None, backfill=False):
    from src.sim_accounts import all_accounts

    now = now or datetime.now(SHANGHAI)
    latest = closed_date(now)
    target = datetime.strptime(asof, "%Y-%m-%d").date() if asof else latest
    if target > latest:
        raise ValueError("目标日期尚未收盘，不保存盘中成绩")
    if target not in get_trading_dates(target, target):
        raise ValueError("目标日期不是已确认交易日")
    feed = ClosingPrices()
    accounts = all_accounts()
    sections = [f"# 模拟收盘日报 — {target}", ""]
    notes = []
    errors = 0
    window = _backfill_dates(target) if backfill else []
    for account in accounts:
        path, label, benchmark = (account.ledger_path, account.label,
                                  account.benchmark)
        try:
            state = load_performance(path)
            healed, lingering = [], []
            for day in window:
                if day >= target:
                    continue
                start = state.get("start_date")
                if start and day.isoformat() < start:
                    continue
                if _complete(_record(state, day.isoformat())):
                    continue
                try:
                    state = _collect(path, day, benchmark, feed, dry_run)
                except Exception:
                    logger.exception("[回补] %s %s 采集失败", label, day)
                    lingering.append(f"{day}（采集失败）")
                    continue
                row = _record(state, day.isoformat())
                if _complete(row):
                    healed.append(str(day))
                elif row is not None:
                    lingering.append(f"{day}（{'；'.join(row.get('issues', [])) or '估值无效'}）")
                else:
                    lingering.append(f"{day}（未生成记录）")
            # 自动运行不重采已完整的当日记录；手动 --date 保留强制重算语义。
            if not backfill or not _complete(_record(state, target.isoformat())):
                state = _collect(path, target, benchmark, feed, dry_run)
            sections.append(render_summary(path, today=target, label=label, state=state))
            row = _record(state, target.isoformat())
            if row and row["issues"]:
                errors += 1
            if healed:
                notes.append(f"- {label} 已补齐：{'、'.join(healed)}")
            if lingering:
                notes.append(f"- {label} 仍不完整：{'、'.join(lingering)}")
                logger.warning("[回补] %s 仍不完整：%s", label, "、".join(lingering))
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
    parser = argparse.ArgumentParser(description="采集三个模拟资金池的完整收盘成绩")
    parser.add_argument("--date", help="补采已收盘交易日，YYYY-MM-DD（只采指定日）")
    parser.add_argument("--dry-run", action="store_true", help="仅展示，不保存记录")
    args = parser.parse_args()
    setup_env()
    setup_logging(log_prefix="portfolio_report")
    asof = args.date or closed_date(datetime.now(SHANGHAI)).isoformat()
    report, errors = run(asof=asof, dry_run=args.dry_run, backfill=args.date is None)
    print(report)
    if not args.dry_run:
        from src.task_io import save_report
        # 补采历史日按数据日期归档，不覆盖今天的收盘日报。
        save_report(report, "portfolio_close", report_date=asof)
    return 1 if errors else 0


if __name__ == "__main__":
    hard_exit(main())
