# -*- coding: utf-8 -*-
"""按实际账户持仓生成跨资产动量建议和日报。"""

import argparse
import io
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from src.config import setup_env
from src.cron_entry import add_standard_flags, hard_exit, resolve_run_gate
from src.logging_config import setup_logging

setup_env()

from src.etf.rotation import rotation_snapshot  # noqa: E402
from src.task_io import notify, save_report       # noqa: E402

logger = logging.getLogger(__name__)
LABEL = "跨资产动量"


def _account_advice(trade_date, rows, prices, diag, *, dry_run=False, error=None):
    from src.etf_account import ACCOUNT_PATH, publish_rotation

    try:
        return publish_rotation(trade_date, rows, prices, diag, path=Path(ACCOUNT_PATH),
                                dry_run=dry_run, error=error)
    except Exception:
        logger.exception("投资账户轮动建议生成失败")
        if not dry_run:
            try:
                publish_rotation(trade_date, [], {}, {}, path=Path(ACCOUNT_PATH),
                                 error="账户建议生成失败，等待更新")
            except Exception:
                logger.exception("投资账户建议暂停状态保存失败")
        return "账户建议生成失败，请检查日志"


def _fmt_score(row) -> str:
    return f"{row.score:+.4f}" if row.score == row.score else "—"


def _status_text(row) -> str:
    if row.eligible:
        return "✅ 合格候选"
    if row.score != row.score:
        return "数据不足/价格非法"
    if row.score == 0:
        return "跳水过滤记 0"
    return "不合格"


def _cross_section_table(rows) -> List[str]:
    """当日截面表（合格候选在前、按名次；不合格按分数降序）。"""
    lines = ["| 标的 | 类别 | 评分 | 状态 |",
             "| --- | --- | ---: | --- |"]
    for row in rows:
        lines.append(f"| {row.name}({row.code}) | {row.group} | "
                     f"{_fmt_score(row)} | {_status_text(row)} |")
    return lines


def run(dry_run: bool = False, trade_date: Optional[str] = None) -> str:
    """获取当日截面，发布投资账户建议并生成报告。"""
    trade_date = trade_date or date.today().isoformat()
    decision_date = date.fromisoformat(trade_date)
    try:
        rows, prices, diag = rotation_snapshot(decision_date=decision_date)
    except Exception:
        if not dry_run:
            _account_advice(trade_date, [], {}, {}, error="行情更新失败，暂停建议")
        raise
    account_advice = _account_advice(trade_date, rows, prices, diag, dry_run=dry_run)
    lines = [
        f"# {LABEL} — {trade_date}",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 数据基准日：上一交易日 {diag.get('prev_trading_day') or '—'}；"
        f"池内可用 {diag['n']}/{diag['pool']} 只，合格候选 {diag['eligible']} 只",
    ]
    if diag["realtime_missing"]:
        lines.append(f"- 实时价缺失 {diag['realtime_missing']}/{diag['n']} 只，收盘价仅供展示，本次暂停建议")
    lines += ["", "## 今日截面", "", *_cross_section_table(rows),
              "", "## 投资账户建议", "", account_advice, ""]
    return "\n".join(lines)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='跨资产动量 — 账户建议与报告',
    )
    add_standard_flags(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    setup_logging(log_prefix="etf_rotation", debug=args.debug)

    logger.info("=" * 60)
    logger.info("跨资产动量 — 账户建议任务启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run)
    if skip:
        logger.info("今天不是 A 股交易日，跳过执行")
        return 0
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不保存建议")

    try:
        report = run(dry_run=dry_run)
    except Exception as e:
        logger.exception(f"运行失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    save_report(report, "etf_rotation_advice_dry_run" if dry_run else "etf_rotation_advice")

    if not args.no_notify:
        notify(report)

    logger.info("运行完成")
    return 0


if __name__ == "__main__":
    # 硬退出理由（SDK 非守护回调线程）单点在 cron_entry.hard_exit。
    hard_exit(main())
