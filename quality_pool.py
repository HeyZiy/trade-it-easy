"""按实际账户持仓生成质量动量建议和日报。"""

import argparse
import logging
from datetime import date

from src.config import setup_env
from src.cron_entry import add_standard_flags, hard_exit, resolve_run_gate
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.quality_pool.config import REPORT_PREFIX
from src.task_io import notify, save_report

logger = logging.getLogger(__name__)


def run(today: date, dry_run: bool = False, *, no_notify: bool = False) -> str:
    from src.etf_account import publish_quality

    try:
        advice = publish_quality(today.isoformat(), dry_run=dry_run)
    except Exception:
        logger.exception("质量动量账户建议发布失败")
        advice = "账户建议更新失败，请检查日志。"
    label = "（试运行）" if dry_run else ""
    report = f"# 质量动量{label}（{today.isoformat()}）\n\n{advice}"
    prefix = REPORT_PREFIX + ("_dry_run" if dry_run else "")
    path = save_report(report, prefix, report_date=today.isoformat())
    logger.info("报告已保存: %s", path)
    if not no_notify and (dry_run or any(line.startswith(("- 买入 ", "- 卖出 "))
                                         for line in advice.splitlines())):
        notify(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="质量动量账户建议与报告")
    add_standard_flags(parser)
    args = parser.parse_args()
    setup_logging(log_prefix="quality_pool", debug=args.debug)

    today = date.today()
    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run,
                                     trading_day=is_trading_day(today))
    if not skip:
        run(today, dry_run=dry_run, no_notify=args.no_notify)
    return 0


if __name__ == "__main__":
    hard_exit(main())
