# -*- coding: utf-8 -*-
"""环境判定任务 — 每交易日产出环境快照（生产者，策略任务只读结果）。

以同一份指数数据、同一判定时点计算并落盘：
  gate_state = market_gate 判定核（AmazingData 指数，快照补当日 bar）

当前状态：**休眠**——唯一消费方（趋势回踩双任务）2026-10 退役后暂无运行中的
消费方，cron 已摘除；未来策略需要市场门控时，在 deploy/crontab.server 加回
一行（14:35，先于消费任务）即可，生产者/快照仓库/测试均保持可用。

分层约定（CONTEXT.md「环境快照」）：
- 本任务只产标签与判定事实，不产 policy（CAN_OPEN 归策略层）。

用法：
  python env_report.py            # 交易日检查（非交易日直接退出）
  python env_report.py --force    # 跳过交易日检查（调试用）
"""
import argparse
import logging
import sys

from src.logging_config import setup_logging
from src.market_state.environment import save_environment
from src.market_state.market_gate import diagnose_gate, fetch_index_df
from src.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="环境判定任务：gate_state 环境快照")
    parser.add_argument("--force", action="store_true", help="跳过交易日检查（调试用）")
    args = parser.parse_args()

    setup_logging()

    if not args.force and not is_trading_day():
        logger.info("非交易日，环境判定任务退出")
        return 0

    index_df = fetch_index_df()
    if index_df is None:
        logger.error("指数数据取数失败（登录已重试仍不可用），环境快照不更新，任务退出")
        return 1

    diag = diagnose_gate(index_df)
    save_environment(diag)
    logger.info(f"环境快照已落盘：gate_state={diag.gate_state}｜{diag.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
