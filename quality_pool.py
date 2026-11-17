# -*- coding: utf-8 -*-
"""质量动量任务：每交易日 09:30 检查持仓并补位，先卖后买模拟记账。

实际账户按自己的持仓和预算生成建议，成交需人工确认。规则见 strategy/quality_pool.md。
signal / execute 子命令用于分阶段排查。
"""

import argparse
import io
import logging
import sys
from datetime import date

from src.config import setup_env
from src.cron_entry import hard_exit, resolve_run_gate
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.quality_pool import assembly, signal as pool_signal  # noqa: E402
from src.quality_pool.config import REPORT_PREFIX  # noqa: E402
from src.task_io import notify, save_report       # noqa: E402
from src.trade_ledger.performance import render_summary  # noqa: E402

logger = logging.getLogger(__name__)


def run(today: date, dry_run: bool = False) -> str:
    """完成独立模拟及实际账户建议；两者均无行动的静默日不保存不推送。"""
    report = assembly.run_day(today, dry_run=dry_run)
    report, acted = _account_report(report, today, dry_run)
    return _finish_report(report, "", today, dry_run, acted=acted)


def _account_report(report, today, dry_run):
    from src.etf_account import publish_quality

    acted = getattr(report, "acted", True)
    try:
        advice = publish_quality(today.isoformat(), dry_run=dry_run)
    except Exception:
        logger.exception("质量动量账户建议发布失败")
        advice = "账户建议更新失败，请重新运行"
    return str(report) + "\n\n## 投资账户 · 质量动量计划\n\n" + advice, acted or "- 买入" in advice or "- 卖出" in advice


def run_signal(today: date, dry_run: bool = False) -> str:
    """信号生成与恢复由质量动量模块负责；手动分阶段排查始终保存推送。"""
    return _finish_report(pool_signal.run_signal(today, dry_run=dry_run),
                          "signal", today, dry_run, acted=True)


# ── execute ──

def run_execute(today: date, dry_run: bool = False) -> str:
    """手动执行已生成的名单；手动分阶段排查始终保存推送。"""
    report, _ = _account_report(assembly.run_execution(today.isoformat(), dry_run=dry_run), today, dry_run)
    return _finish_report(report, "execute", today, dry_run, acted=True)


def _finish_report(report: str, stage: str, today: date, dry_run: bool,
                   *, acted: bool | None = None) -> str:
    from src.quality_pool.config import POOL_LEDGER_PATH
    # acted 必须在重建前读：partition/拼接会把 SignalReport 降级成普通 str，属性丢失
    if acted is None:
        acted = getattr(report, "acted", True)
    title, _, body = report.partition("\n")
    status = "**试运行：未写状态或交易台账。**\n\n" if dry_run else ""
    report = (title + "\n\n" + status
              + render_summary(POOL_LEDGER_PATH, today=today, label="质量动量")
              + "\n\n" + body)
    if not acted and not dry_run:
        logger.info(f"{today} 无操作或异常，免发日报")
        return report
    prefix = REPORT_PREFIX + (f"_{stage}" if stage else "") + ("_dry_run" if dry_run else "")
    path = save_report(report, prefix, report_date=today.isoformat())
    notify(report)
    logger.info(f"报告已保存并推送: {path}")
    return report


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description="质量动量模拟盘")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "signal", "execute"],
                        help="默认一次完成模拟；signal/execute 仅供手动分阶段排查")
    parser.add_argument("--dry-run", action="store_true",
                        help="只出报告与计划，不写状态/台账")
    parser.add_argument("--force", action="store_true",
                        help="跳过交易日检查（手动调试用；非交易日一律只读不记账）")
    args = parser.parse_args()
    setup_logging()

    today = date.today()
    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run,
                                     trading_day=is_trading_day(today))
    if skip:
        logger.info(f"{today} 非交易日，跳过")
        return 0
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不写状态/台账")

    if args.command == "run":
        run(today, dry_run=dry_run)
    elif args.command == "signal":
        run_signal(today, dry_run=dry_run)
    else:
        run_execute(today, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    hard_exit(main())
