# -*- coding: utf-8 -*-
"""每周生成市场观察报告，保存 Markdown 并按入口参数推送。"""

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
    parser = argparse.ArgumentParser(description='市场观察周报')
    add_standard_flags(parser)
    args = parser.parse_args()

    setup_logging(log_prefix="etf_observe", debug=args.debug)

    # 交易日检查：非交易日且未 --force 时直接跳过（节假日周一）
    skip, _ = resolve_run_gate(force=args.force, dry_run=args.dry_run)
    if skip:
        logger.info("今天不是 A 股交易日，跳过执行")
        print("今天不是 A 股交易日，跳过执行")
        return 0

    logger.info("=" * 60)
    logger.info("市场观察周报")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report = allocation.run()
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
