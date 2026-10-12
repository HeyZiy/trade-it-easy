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

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.quality_pool import assembly, signal as pool_signal  # noqa: E402
from src.quality_pool.config import REPORT_PREFIX  # noqa: E402
from src.task_io import notify, save_report       # noqa: E402
from src.trade_ledger.performance import render_summary  # noqa: E402

logger = logging.getLogger(__name__)


def run_signal(today: date, dry_run: bool = False, retry: bool = False) -> str:
    """信号周期与恢复由质量池 module 负责，入口只保存和推送报告。"""
    return _finish_report(pool_signal.run_signal(today, dry_run=dry_run, retry=retry),
                          "signal", today, dry_run)


# ── execute ──

def run_execute(today: date, dry_run: bool = False) -> str:
    """09:31 执行闭环由质量池 module 负责，入口只保存和推送报告。"""
    return _finish_report(assembly.run_execution(today.isoformat(), dry_run=dry_run),
                          "execute", today, dry_run)


def _finish_report(report: str, stage: str, today: date, dry_run: bool) -> str:
    from src.quality_pool.config import POOL_LEDGER_PATH
    title, _, body = report.partition("\n")
    status = "**试运行：未写状态或交易台账。**\n\n" if dry_run else ""
    report = (title + "\n\n" + status
              + render_summary(POOL_LEDGER_PATH, today=today, label="质量池轮动")
              + "\n\n" + body)
    prefix = f"{REPORT_PREFIX}_{stage}" + ("_dry_run" if dry_run else "")
    path = save_report(report, prefix, report_date=today.isoformat())
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
