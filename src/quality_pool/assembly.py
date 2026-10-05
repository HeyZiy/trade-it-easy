# -*- coding: utf-8 -*-
"""
===================================
质量池 09:31 装配 — 快照组装与模拟记账
===================================

根入口（编排）与判定核（execution / screener）之间的合并点：

- build_snapshot：台账派生 + 实时行情 + 当日状态。台账一次派生、状态表
  一次拉取（停牌/ST/涨跌停三个消费共享）；ST 与停牌一律以状态表官方
  标志为准（与筛分侧同口径，不用名称子串推断）；状态表缺该码的行按
  suspended=True / st=True 处置——确认不了可交易就不动（fail-closed，
  规格数据暂停精神）。总权益直接取台账快照 total_assets（现金 + 持仓
  按现价市值），不在本层重算。
- record_trades：交易意图 → 台账批次（收敛/安全校验/先卖后买/逐单隔离
  全部在 trade_ledger.execute_batch 单点）。记账前对台账重新派生持仓
  供批次安全校验——快照与记账之间台账可能被外部改动，安全校验的输入
  必须新鲜，此处不复用快照。

本模块零网络：数据一律经 feeds seam 进入；测试在 feeds 层放假数据
打真实装配逻辑。路径经 config 运行时读取，测试可单点替换。
"""

import logging
from typing import List

from src.quality_pool import config, execution, feeds
from src.trade_ledger.batch import BatchOrder, execute_batch
from src.trade_ledger.ledger import derive, load_trades
import src.trading_calendar as trading_calendar

logger = logging.getLogger(__name__)


def build_snapshot(exec_date: str, selected: List[str]) -> execution.ExecSnapshot:
    """09:31 执行时点快照：台账派生 + 实时价 + 当日状态（状态表单次、口径权威）。

    状态表缺行 → 该码按停牌/ST 处置（fail-closed，缺数据的品种不参与成交判定）。
    """
    import datetime as dt

    trades = load_trades(config.POOL_LEDGER_PATH)
    codes = sorted({t.code for t in trades} | set(selected))
    prices, names = feeds.fetch_realtime_quotes(codes)

    snap0 = derive(trades, as_of=exec_date, prices=prices)
    counts = {p["code"]: int(p["count"]) for p in snap0.positions}
    avail = {p["code"]: int(p["avail_count"]) for p in snap0.positions}

    prev_day = trading_calendar.latest_trading_day_on_or_before(
        dt.date.fromisoformat(exec_date) - dt.timedelta(days=1))
    if prev_day is None:
        raise RuntimeError(f"交易日历无 {exec_date} 之前的数据")
    prev_close = feeds.raw_closes_at(codes, prev_day.isoformat())
    status = feeds.fetch_status(codes, exec_date)
    limits = feeds.day_limits(codes, exec_date, prev_close, status=status)

    suspended: dict = {}
    st: dict = {}
    for code in codes:
        if code in status.index:
            suspended[code] = bool(status.at[code, "is_suspended"])
            st[code] = bool(status.at[code, "is_st"])
        else:
            suspended[code] = True
            st[code] = True
            logger.warning(f"[quality_pool] 状态表缺 {code}，按停牌/ST 处置")

    return execution.ExecSnapshot(
        trade_date=exec_date, counts=counts, avail=avail, cash=snap0.cash,
        equity=snap0.total_assets, prices=prices, names=names,
        suspended=suspended, st=st, limits=limits)


def record_trades(plan: execution.ExecPlan, exec_date: str) -> List[str]:
    """成台记账：意图 → 批次。返回报告行（受阻整批时为单条错误行）。

    台账持仓在记账时点重新派生（不复用快照）：批次安全校验的供料
    必须新鲜，防快照与记账之间台账被外部改动。
    """
    if not plan.trades:
        return []
    snap = derive(load_trades(config.POOL_LEDGER_PATH), as_of=exec_date)
    held = {p["code"]: int(p["count"]) for p in snap.positions}
    orders = [BatchOrder(code=t.code, name=t.name, side=t.side, qty=t.qty,
                         price=t.price, reason=t.reason)
              for t in plan.trades]
    res = execute_batch(orders, account=config.ACCOUNT, held_counts=held,
                        cash=snap.cash, trade_date=exec_date,
                        path=config.POOL_LEDGER_PATH)
    if res.abort:
        return [f"❌ 批次安全校验未过，整批零记账：{res.abort}"]
    return [oc.outcome_line for oc in res.outcomes]
