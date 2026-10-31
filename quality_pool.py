# -*- coding: utf-8 -*-
"""
===================================
质量池相对强弱轮动 — 09:30 启动，一次完成模拟
===================================

规格唯一来源：strategy/roe_quality_pool.md。持仓与资金事实来源是独立名义
台账（src/quality_pool/config.py POOL_LEDGER_PATH），模拟记账、无真实下单。

默认命令（quality_pool.py，无须子命令；deploy/crontab.server 一条 cron）：
每交易日09:30启动，先检查调仓周期、生成名单或补判未知旧仓，再以计算完成后
的实时行情先卖后买、处理退出队列；调仓轮或当日有成交才保存并推送一份报告，
非调仓且无成交的静默日免发日报（异常走日志告警通道）。全批失败次日
自动重算，不移动原定20交易日周期。预算 = 执行时总权益/6 冻结。
成交不固定为09:30或09:31，按实际取价时点模拟记账。真实账户由人工另行执行。

signal / execute 子命令仅保留供手动分阶段排查，不单独配置定时任务。

约定：
- 6只、ROE_TTM、12-2强弱、行业约束；只按质量池退出，无排名/解禁退出；
- 停牌只限制交易；未知数据保留旧仓且不增减，确认失败才退出；
- 买入与等权调整仅在调仓轮执行一次，不追补买入。
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


def run(today: date, dry_run: bool = False, retry: bool = False) -> str:
    """一次完成信号、执行与汇总；非调仓且无成交的静默日不保存不推送。"""
    return _finish_report(assembly.run_day(today, dry_run=dry_run, retry=retry),
                          "", today, dry_run)


def run_signal(today: date, dry_run: bool = False, retry: bool = False) -> str:
    """信号周期与恢复由质量池 module 负责；手动分阶段排查始终保存推送。"""
    return _finish_report(pool_signal.run_signal(today, dry_run=dry_run, retry=retry),
                          "signal", today, dry_run, acted=True)


# ── execute ──

def run_execute(today: date, dry_run: bool = False) -> str:
    """手动执行已生成的名单；手动分阶段排查始终保存推送。"""
    return _finish_report(assembly.run_execution(today.isoformat(), dry_run=dry_run),
                          "execute", today, dry_run, acted=True)


def _finish_report(report: str, stage: str, today: date, dry_run: bool,
                   *, acted: bool | None = None) -> str:
    from src.quality_pool.config import POOL_LEDGER_PATH
    # acted 必须在重建前读：partition/拼接会把 SignalReport 降级成普通 str，属性丢失
    if acted is None:
        acted = getattr(report, "acted", True)
    title, _, body = report.partition("\n")
    status = "**试运行：未写状态或交易台账。**\n\n" if dry_run else ""
    report = (title + "\n\n" + status
              + render_summary(POOL_LEDGER_PATH, today=today, label="质量池轮动")
              + "\n\n" + body)
    if not acted and not dry_run:
        logger.info(f"{today} 非调仓且无成交，免发日报")
        return report
    prefix = REPORT_PREFIX + (f"_{stage}" if stage else "") + ("_dry_run" if dry_run else "")
    path = save_report(report, prefix, report_date=today.isoformat())
    notify(report)
    logger.info(f"报告已保存并推送: {path}")
    return report


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    parser = argparse.ArgumentParser(description="质量池轮动模拟盘")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "signal", "execute"],
                        help="默认一次完成模拟；signal/execute 仅供手动分阶段排查")
    parser.add_argument("--dry-run", action="store_true",
                        help="只出报告与计划，不写状态/台账")
    parser.add_argument("--force", action="store_true",
                        help="跳过交易日检查（手动调试用；非交易日一律只读不记账）")
    parser.add_argument("--retry-signal", action="store_true",
                        help="手动重试未完成信号，保留原定周期；已成功生成的当日名单不重放")
    args = parser.parse_args()
    if args.retry_signal and args.command == "execute":
        parser.error("--retry-signal 不适用于单独 execute")
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
        run(today, dry_run=dry_run, retry=args.retry_signal)
    elif args.command == "signal":
        if args.retry_signal:
            run_signal(today, dry_run=dry_run, retry=True)
        else:
            run_signal(today, dry_run=dry_run)
    else:
        run_execute(today, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    hard_exit(main())
