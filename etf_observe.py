# -*- coding: utf-8 -*-
"""
==================================
ETF 周度观察报告 — 估值导向
==================================

定位：每周生成核心配置策略建议；成交由用户确认，统一记入ETF账户。

报告生成流程（账户快照 → 配置建议 → 周报）在 src/etf/allocation，
本入口只剩旗标/闸门/落盘/推送；规格唯一来源 strategy/etf_allocation.md。

报告结构：
  1. 市场估值概览（纯数据：全市场 PE / 收益率 / 风险溢价及其分位，不给操作建议）
  2. 各标的估值参考（锚对准跟踪指数当前 PE/股息率，跨标的不可比，仅展示）
  3. 新钱投放参考（全市场估值分位一个数，节奏人工决策，人工执行）
  4. 持仓对照与调仓建议（核心口径，旧钱只做阈值再平衡）

估值口径分两层，不互相兜底：市场级 = 全市场 PE/ERP 分位（第一、三节）；
标的级 = 跟踪指数自身 PE/股息率（估值参考节，无锚或取数失败即显示"无估值锚"）。
任何估值都不进自动决策——核心仓目标恒等于中性基准。
"""

import argparse
import io
import logging
import sys
from datetime import datetime

from src.config import setup_env
from src.cron_entry import add_standard_flags, hard_exit, resolve_run_gate
from src.logging_config import setup_logging

setup_env()

from src.etf import allocation
from src.task_io import notify, save_report

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='ETF 周度观察报告')
    add_standard_flags(parser)
    args = parser.parse_args()

    setup_logging(log_prefix="etf_observe", debug=args.debug)

    # 交易日检查：非交易日且未 --force 时直接跳过（节假日周一）
    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run)
    if skip:
        logger.info("今天不是 A 股交易日，跳过执行")
        print("今天不是 A 股交易日，跳过执行")
        return 0
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不写台账")

    logger.info("=" * 60)
    logger.info("ETF 周度观察报告（配置建议）")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report = allocation.run(dry_run=dry_run)
    except Exception as e:
        logger.exception(f"生成报告失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    save_report(report, "etf_weekly")

    if not args.no_notify:
        notify(report)

    return 0


if __name__ == "__main__":
    # 硬退出理由（SDK 非守护回调线程）单点在 cron_entry.hard_exit。
    hard_exit(main())
