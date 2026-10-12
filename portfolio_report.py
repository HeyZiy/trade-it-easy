"""每日收盘成绩采集与展示；只写成绩记录，不改交易台账、不下单、不发送通知。"""

import argparse
from datetime import datetime, timedelta, timezone
import logging

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import get_trading_dates
from src.trade_ledger.ledger import DEFAULT_LEDGER_PATH
from src.trade_ledger.closing_prices import ClosingPrices
from src.trade_ledger.performance import collect_close, render_summary

logger = logging.getLogger(__name__)
SHANGHAI = timezone(timedelta(hours=8))


def closed_date(now):
    """只接受交易日历确认、且已结束的交易日；盘中不得记录当日收盘。"""
    now = now.astimezone(SHANGHAI)
    end = now.date() if (now.hour, now.minute) >= (15, 20) else now.date() - timedelta(days=1)
    days = get_trading_dates(end - timedelta(days=40), end)
    if not days:
        raise ValueError("交易日历不可用，暂停收盘采集")
    return max(days)


def run(*, asof=None, dry_run=False, now=None):
    from src.etf.config import CORE_BASELINE
    from src.quality_pool.config import POOL_LEDGER_PATH
    now = now or datetime.now(SHANGHAI)
    latest = closed_date(now)
    target = datetime.strptime(asof, "%Y-%m-%d").date() if asof else latest
    if target > latest:
        raise ValueError("目标日期尚未收盘，不保存盘中成绩")
    if target not in get_trading_dates(target, target):
        raise ValueError("目标日期不是已确认交易日")
    feed = ClosingPrices()
    accounts = [
        (DEFAULT_LEDGER_PATH, "ETF共享组合（核心配置＋行业动量）",
         {"name": "ETF期初权重买入持有（现金零收益，不再平衡）",
          "weights": {a.code: a.neutral_weight for a in CORE_BASELINE}}),
        (POOL_LEDGER_PATH, "质量池轮动", {"name": "中证800价格指数", "index": "000906"}),
    ]
    sections = [f"# 模拟收盘日报 — {target}", ""]
    errors = 0
    for path, label, benchmark in accounts:
        try:
            state = collect_close(path, target.isoformat(), benchmark,
                                  close_prices=feed.close_prices, index_prices=feed.index_prices,
                                  trading_dates=get_trading_dates, dry_run=dry_run)
            sections.append(render_summary(path, today=target, label=label, state=state))
            row = next((r for r in state.get("records", []) if r["date"] == target.isoformat()), None)
            if row and row["issues"]:
                errors += 1
        except Exception:
            logger.exception("[收盘成绩降级] %s 采集失败，保留已有成绩", label)
            sections.append(f"## {label}\n\n⚠️ 本次收盘采集失败，保留已有成绩。\n\n"
                            + render_summary(path, today=target, label=label))
            errors += 1
    if dry_run:
        sections.append("试运行：未保存收盘记录。")
    return "\n\n".join(sections), errors


def main():
    parser = argparse.ArgumentParser(description="采集两个模拟资金池的完整收盘成绩")
    parser.add_argument("--date", help="补采已收盘交易日，YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="仅展示，不保存记录")
    args = parser.parse_args()
    setup_env()
    setup_logging(log_prefix="portfolio_report")
    asof = args.date or closed_date(datetime.now(SHANGHAI)).isoformat()
    report, errors = run(asof=asof, dry_run=args.dry_run)
    print(report)
    if not args.dry_run:
        from src.task_io import save_report
        # 补采历史日按数据日期归档，不覆盖今天的收盘日报。
        save_report(report, "portfolio_close", report_date=asof)
    return 1 if errors else 0


if __name__ == "__main__":
    # 与质量池入口相同：采集完成后不等待SDK后台回调线程。
    import os
    result = main()
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(result)
