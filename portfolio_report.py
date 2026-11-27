"""采集投资账户收盘估值，回补历史记录并生成再平衡建议。"""

import argparse
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import logging
import math

from src import account_valuation, allocation_report
from src.account_store import revision
from src.config import setup_env
from src.cron_entry import hard_exit
from src.logging_config import setup_logging
from src.trading_calendar import get_trading_dates
from data_provider.benchmark_returns import benchmark_returns
from data_provider.closing_prices import ClosingPrices

logger = logging.getLogger(__name__)
SHANGHAI = timezone(timedelta(hours=8))
BACKFILL_TRADING_DAYS = 5
DCA_RECORDING_TIME = (18, 30)  # 与生产收盘任务的启动时间一致


def closed_date(now):
    """只接受交易日历确认、且已结束的交易日；盘中不得记录当日收盘。"""
    now = now.astimezone(SHANGHAI)
    end = now.date() if (now.hour, now.minute) >= (15, 20) else now.date() - timedelta(days=1)
    days = get_trading_dates(end - timedelta(days=40), end)
    if not days:
        raise ValueError("交易日历不可用，暂停收盘采集")
    return max(days)


def _current_record(state: dict, row: dict | None) -> bool:
    if row is None:
        return False
    nav = row.get("nav")
    return (isinstance(nav, (int, float)) and math.isfinite(nav) and nav > 0
            and not row.get("issues")
            and row.get("revision") == revision(state, row["date"]))


def _record(state, day):
    return next((r for r in state.get("records", []) if r["date"] == day), None)


def _backfill_dates(target):
    """目标日之前最近 5 个交易日（含目标日，窗口内前后顺序不变）。"""
    days = get_trading_dates(target - timedelta(days=40), target)
    return days[-BACKFILL_TRADING_DAYS:]


def _backfill_records(
    state: dict, window: list[date], *, first_day: date, target: date,
    collect_day: Callable[[date], dict],
) -> tuple[list[str], list[str]]:
    """补齐目标日之前缺失、不完整或已失效的估值。"""
    healed, lingering = [], []
    for day in window:
        if not first_day <= day < target:
            continue
        day_key = day.isoformat()
        if _current_record(state, _record(state, day_key)):
            continue
        try:
            state = collect_day(day)
        except Exception:
            logger.exception("[回补] 投资账户 %s 采集失败", day)
            lingering.append(f"{day}（采集失败）")
            continue
        row = _record(state, day_key)
        if _current_record(state, row):
            healed.append(str(day))
        elif row is not None:
            lingering.append(f"{day}（{'；'.join(row.get('issues', [])) or '估值无效'}）")
        else:
            lingering.append(f"{day}（未生成记录）")
    return healed, lingering


def _backfill_notes(healed: list[str], lingering: list[str]) -> list[str]:
    notes = []
    if healed:
        notes.append(f"- 投资账户 已补齐：{'、'.join(healed)}")
    if lingering:
        unresolved = "、".join(lingering)
        notes.append(f"- 投资账户 仍不完整：{unresolved}")
        logger.warning("[回补] 投资账户 仍不完整：%s", unresolved)
    return notes


def run(*, asof=None, dry_run=False, now=None, backfill=False, no_notify=False):
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    today = now.date()
    latest = closed_date(now)
    target = datetime.strptime(asof, "%Y-%m-%d").date() if asof else latest
    if target > latest:
        raise ValueError("目标日期尚未收盘，不保存盘中成绩")
    if target not in get_trading_dates(target, target):
        raise ValueError("目标日期不是已确认交易日")
    feed = ClosingPrices()
    from src.etf_account import (
        ACCOUNT_PATH, backfill_benchmarks, collect_close as collect_core_close,
        load as load_core, refresh_close_prices,
        refresh_fund_navs, render_summary as core_summary, valuation_day,
    )
    from src.fund_plans import record_due_subscriptions
    sections = [f"# 账户收盘采集 — {target}", ""]
    notes = []
    errors = 0
    window = _backfill_dates(target) if backfill else []
    path = ACCOUNT_PATH

    def collect_day(day: date) -> dict:
        return collect_core_close(path, day.isoformat(), feed.close_prices, dry_run=dry_run)

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
            if (now.hour, now.minute) >= DCA_RECORDING_TIME:
                try:
                    recorded = record_due_subscriptions(path, run_day=today)
                    if recorded:
                        logger.info("定投登记 %s 笔待确认申购", len(recorded))
                except Exception:
                    logger.exception("当日定投记账失败，请核对定投计划")
            state = load_core(path)
        first_day = datetime.fromisoformat(state["initial"]["date"]).date() + timedelta(days=1)
        healed, lingering = _backfill_records(
            state, window, first_day=first_day, target=target, collect_day=collect_day,
        )
        state = collect_day(target)
        if not dry_run:
            try:
                # 历史记录的基准净值只补基准，不重采持仓估值；失败保留空值下次再补。
                backfill_benchmarks(path, benchmark_returns, through=target.isoformat())
            except Exception:
                logger.exception("基准收益回补失败，保留已有基准")
        notes = _backfill_notes(healed, lingering)
        if asof is None and not dry_run:
            try:
                missing = refresh_close_prices(path, today, valuation_day(today, now=now), feed.close_prices)
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
    if notes:
        sections += ["## 本次回补（目标日之前）", "", *notes]
    after_close = now.astimezone(SHANGHAI).time() >= account_valuation.VALUATION_CUTOFF
    if not errors and asof is None and target == today and after_close:
        try:
            if not dry_run:
                state = load_core(path)
            day_key = target.isoformat()
            if _current_record(state, _record(state, day_key)):
                if dry_run:
                    known = account_valuation.closing_prices(state, day_key)
                    needed = account_valuation.valuation_codes(state, target) - known.keys()
                    if needed:
                        fetched = feed.close_prices(sorted(needed), day_key)
                        account_valuation.merge_closing_prices(state, day_key, needed, fetched)
                actions = allocation_report.allocation_actions(state, target)
                if actions is not None:
                    if dry_run:
                        sections.append(allocation_report.render_report(day_key, actions, {})
                                        if actions else "权益组合与固收组合暂无再平衡建议。")
                    elif not allocation_report.publish(day_key, actions, no_notify=no_notify):
                        errors += 1
            else:
                logger.warning("收盘记录待重新估值，跳过再平衡报告")
        except Exception:
            logger.exception("收盘再平衡报告生成失败")
            errors += 1
    if dry_run:
        sections.append("试运行：未保存收盘记录。")
    return "\n\n".join(sections), errors


def main():
    parser = argparse.ArgumentParser(description="采集投资账户收盘估值并生成再平衡建议")
    parser.add_argument("--date", help="补采已收盘交易日，YYYY-MM-DD（只采指定日）")
    parser.add_argument("--dry-run", action="store_true", help="仅展示，不保存记录")
    parser.add_argument("--no-notify", action="store_true", help="有再平衡建议时只保存报告，不推送")
    args = parser.parse_args()
    setup_env()
    setup_logging(log_prefix="portfolio_report")
    report, errors = run(asof=args.date, dry_run=args.dry_run, backfill=args.date is None, no_notify=args.no_notify)
    if args.dry_run:
        import io
        import sys
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        print("\n" + report)
    return 1 if errors else 0


if __name__ == "__main__":
    hard_exit(main())
