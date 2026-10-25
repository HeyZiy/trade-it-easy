# -*- coding: utf-8 -*-
"""
cron 入口装具 — 交易日政策与硬退出的单点
===================================

四个 cron 入口（quality_pool / etf_rotation / etf_observe / industry_momentum，
另 portfolio_report 仅用 hard_exit）共用的运行政策件：

- add_standard_flags：四个标准旗标（--debug/--no-notify/--dry-run/--force）
  与帮助文案单点；质量池入口的子命令旗标是真差异，不用此件；
- resolve_run_gate：交易日闸门与「非交易日 --force 一律只读」推导——
  非交易日不可能有真实成交，硬记账就是假日假账；政策只在装具改一处，
  推导结果（skip / dry_run）由入口自己记日志；
- hard_exit：AmazingData/TGW SDK 启动了非守护 SWIG 回调线程，正常
  sys.exit 会等不到该线程导致进程挂住（terminate 时打
  Swig::DirectorMethodException）；主流程（信号/执行/报告/推送）完成后
  先 flush 再 os._exit 直接终止，残留 C 线程随进程一起被回收。

件库而非整体接管：入口的 main 骨架（banner/落盘/推送/异常处理）语义
各有差异，留在入口；本装具只收四个入口逐字重复的政策件。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Tuple

from src.trading_calendar import is_trading_day


def add_standard_flags(parser: argparse.ArgumentParser) -> None:
    """ETF 侧三个入口共用的标准旗标（帮助文案单点）。"""
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    parser.add_argument('--no-notify', action='store_true', help='不发送推送通知')
    parser.add_argument('--dry-run', action='store_true', help='只检测不记账（调试用）')
    parser.add_argument('--force', action='store_true',
                        help='跳过交易日检查（手动调试用；非交易日一律只读不记账）')


def resolve_run_gate(*, force: bool, dry_run: bool = False,
                     trading_day: Optional[bool] = None) -> Tuple[bool, bool]:
    """交易日闸门 → (skip, dry_run)。

    非交易日且未 --force：skip=True（调用方记日志后直接返回）。
    非交易日 + --force：只读，dry_run=True（不可能有真实成交，硬记账
    就是假日假账）。trading_day 注入供测试与按指定日期判定的入口
    （质量池），默认按今天判。
    """
    trading = is_trading_day() if trading_day is None else trading_day
    if not force and not trading:
        return True, dry_run
    return False, dry_run or not trading


def hard_exit(code: int = 0) -> None:
    """进程硬退出：flush 后 os._exit，不等 SDK 非守护回调线程（见模块头）。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
