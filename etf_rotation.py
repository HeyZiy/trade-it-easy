# -*- coding: utf-8 -*-
"""
===================================
ETF 跨资产动量轮动 — 账户建议与独立模拟入口
===================================

把研究线 `research/studies/etf_rotation/unified_coverage.py`（聚宽日频策略）
接到本项目的名义成交台账上，每个交易日运行一次：

1. 读独立流水（data/trade_ledger_etf_rotation.jsonl）推导策略账户持仓与现金
   —— 台账是持仓与资金的唯一事实来源；
2. `src.etf.rotation` 出当日截面：15 只 ETF 的 25 日加权动量分，
   合格候选按分数降序，第一名即目标（0 < score < 5，跳水记 0）；
3. 规则：无合格候选清仓；持仓不是第一名则先卖后买换入新第一名；
   仍是第一名则不动（单只全仓，无止损/止盈/再平衡）；
4. 同一行情为ETF账户的轮动持仓与已分配现金生成建议，不写成交；
5. 独立模拟指令经 `trade_ledger.execute_batch` 记入本策略台账（account=etf_rotation，
   信号当日现价模拟成交），与ETF账户的人工成交记录分开；
6. 渲染报告 → reports/ 落盘 + 推送。

执行约定：
- 决策时点与聚宽研究线一致为 10:40（cron 对齐）；现价缺失时收盘价只供展示，
  本次不调仓。运行时刻不同只影响当次决策价，不改变规则；
- 独立策略账户 = 独立流水文件（单池名义期初 100 万），与ETF账户资金和成交记录
  互不合并；account 标签只做复盘归因；
- 通用批次安全校验（卖出 ≤ 台账持仓、买入额 ≤ 现金 + 卖出回款）单点在
  execute_batch；本入口额外 fail-closed：无截面数据或持仓缺价时整次不调仓，
  避免把取数故障记成"清仓"；
- 买侧股数由引擎按万二佣金/最低 5 元做资金可行性预检，新成交台账按相同口径
  扣费；现金分红按登记日份额确认，发放日前记应收。

运行：
    python etf_rotation.py            # 更新账户建议 + 独立模拟记账 + 推送
    python etf_rotation.py --dry-run  # 预览建议和模拟指令，不保存账户或模拟流水
"""

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

from src.etf.rotation import (                    # noqa: E402
    ACCOUNT, LEDGER_PATH, COMMISSION, MIN_COMMISSION, EtfOrder,
    dividend_records, plan_rotation, rotation_snapshot,
)
from src.task_io import notify, save_report       # noqa: E402
from src.trade_ledger import (                    # noqa: E402
    append_trade, derive, load_trades,
)
from src.trade_ledger.performance import (        # noqa: E402
    execution_summary, render_summary,
)

logger = logging.getLogger(__name__)
LABEL = "ETF跨资产动量轮动"


def _account_advice(trade_date, rows, prices, diag, *, dry_run=False, error=None):
    from src.etf_account import ACCOUNT_PATH
    from src.etf.rotation_advice import publish

    try:
        return publish(trade_date, rows, prices, diag, path=Path(ACCOUNT_PATH),
                       dry_run=dry_run, error=error)
    except Exception:
        logger.exception("ETF账户轮动建议生成失败")
        if not dry_run:
            try:
                publish(trade_date, [], {}, {}, path=Path(ACCOUNT_PATH),
                        error="账户建议生成失败，等待更新")
            except Exception:
                logger.exception("ETF账户建议暂停状态保存失败")
        return "账户建议生成失败，请检查日志"


def _execute(orders: List[EtfOrder], trade_date: str, *,
             ledger_path=None):
    """执行一批轮动指令：批次语义（收敛/安全校验/先卖后买/逐单记账）
    委托 trade_ledger.execute_batch，本函数只映射指令与渲染结果行。

    返回 (abort, lines)：abort 非空 = 通用安全校验未过，整批零记账。"""
    from src.trade_ledger import BatchOrder, execute_batch

    batch_orders = [BatchOrder(code=o.code, name=o.name, side=o.action,
                               qty=o.shares, price=o.price, reason=o.reason)
                    for o in orders]
    res = execute_batch(batch_orders, account=ACCOUNT, trade_date=trade_date,
                        path=ledger_path or LEDGER_PATH,
                        commission_rate=COMMISSION, min_commission=MIN_COMMISSION)
    return res.abort, [oc.outcome_line for oc in res.outcomes]


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
    """每日闭环：台账持仓 → 截面判定 → 安全校验 → 记账 → 报告。"""
    trade_date = trade_date or date.today().isoformat()
    decision_date = date.fromisoformat(trade_date)
    try:
        rows, prices, diag = rotation_snapshot(decision_date=decision_date)
    except Exception:
        if not dry_run:
            _account_advice(trade_date, [], {}, {}, error="行情更新失败，暂停建议")
        raise
    account_advice = _account_advice(trade_date, rows, prices, diag, dry_run=dry_run)

    trades = load_trades(LEDGER_PATH)
    distributions = dividend_records(trades, diag.get("dividends", {}), trade_date)
    if not dry_run:
        for record in distributions:
            append_trade(record, path=LEDGER_PATH)
        trades = load_trades(LEDGER_PATH)
    else:
        trades = sorted(trades + distributions, key=lambda t: t.date)
    unpriced = derive(trades, trade_date)
    held_codes = [p["code"] for p in unpriced.positions]
    missing_quotes = [code for code in held_codes if code not in prices]
    snap = derive(trades, trade_date, prices)
    positions = snap.positions
    total_assets = snap.total_assets
    avail_cash = snap.cash

    plan = plan_rotation(rows, positions, prices, avail_cash)
    orders = plan["orders"]

    lines = [
        f"# {LABEL} — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        f"{'（dry-run，未记账）' if dry_run else ''}",
        "",
        "## ETF账户 · 待执行建议",
        "",
        account_advice,
        "",
        "## 独立模拟",
        "",
        render_summary(LEDGER_PATH, label=LABEL),
        "",
        "## 决策口径",
        "",
        f"- 数据基准日：上一交易日 {diag.get('prev_trading_day') or '—'}；"
        f"池内可用 {diag['n']}/{diag['pool']} 只，合格候选 {diag['eligible']} 只",
    ]
    if diag["realtime_missing"]:
        lines.append(f"- 实时价缺失 {diag['realtime_missing']}/{diag['n']} 只，收盘价仅供展示，本次不执行")
    if distributions:
        lines.append(f"- {'拟登记' if dry_run else '已登记'}分红权益 {len(distributions)} 项，按公告发放日转为可用现金")
    if snap.receivables:
        lines.append(f"- 尚未到账的应收红利：{snap.receivables:,.2f}元（不用于买入）")
    lines += ["", "## 今日截面（15 只池）", ""]
    lines += _cross_section_table(rows)
    lines += [""]

    # fail-closed：取数故障不得被记账成"清仓"
    guard = None
    if not rows:
        guard = ("池内 15 只全部无可用日线数据，按 fail-closed 本次不调仓"
                 "（不清仓、不动持仓）")
    elif missing_quotes:
        guard = "持仓现价缺失，本次不执行；收盘价不得用作盘中成交价"
    elif diag["realtime_missing"]:
        guard = "实时价不完整，本次不执行；收盘价不得用作盘中成交价"
    elif any(diag.get("skipped", {}).get(k, 0) for k in ("no_bars", "stale", "bad_close", "duplicate")):
        guard = "候选池行情或分红数据不完整，本次不执行"
    elif total_assets <= 0:
        guard = "策略账户净值不可用，本次不调仓"
    elif positions and any(float(p.get("current_price") or 0) <= 0 for p in positions):
        guard = "持仓现价缺失，无法确定卖出金额，本次不调仓"
    if guard:
        # 单源缺失在行情层只记 INFO；到这里才是真正影响策略的行情故障，汇总成一条 WARNING
        logger.warning(f"[行情降级] 轮动本次不执行: {guard}")
        lines += ["## ⛔ 本次不执行", "", f"- {guard}", ""]
        return "\n".join(lines)

    target = plan["target"]
    holding = plan["holding"]
    lines += ["## 决策", ""]
    if target is not None:
        lines.append(f"- 目标：**{target.name}({target.code})** "
                     f"score {_fmt_score(target)}（合格候选第 {target.rank} 名）")
    else:
        lines.append("- 目标：**无合格候选 → 空仓**")
    if holding is not None:
        lines.append(f"- 当前持仓：{holding.get('name', '')}({holding.get('code', '')}) "
                     f"{holding.get('count', 0)}股")
    else:
        lines.append("- 当前持仓：空仓")
    lines += [f"- {note}" for note in plan["notes"]]
    lines += [""]

    action_index = len(lines)
    lines += ["## 执行明细", ""]

    def finish() -> str:
        summary = execution_summary(trades, load_trades(LEDGER_PATH),
                                    dry_run=dry_run, planned=len(orders))
        lines.insert(action_index, summary + "\n")
        return "\n".join(lines)

    if not orders:
        lines += ["无调仓指令，保持当前持仓。", ""]
        return finish()

    if dry_run:
        lines.append("**dry-run：以下指令未记账**")
        for order in orders:
            lines.append(f"- [DRY] {order.action.upper()} {order.name}({order.code})"
                         f" {order.shares}股 ≈ {order.amount:,.0f}元（{order.reason}）")
        lines += [""]
        return finish()

    abort, exec_lines = _execute(orders, trade_date)
    if abort:
        lines += [f"⛔ 安全校验未通过，本次不执行：{abort}", ""]
        return finish()
    lines += exec_lines
    lines += [""]

    prices.update({o.code: o.price for o in orders if o.price > 0})
    fresh = derive(load_trades(LEDGER_PATH), trade_date, prices).positions
    if fresh:
        lines += ["**执行后持仓**：", ""]
        for pos in fresh:
            mv = float(pos.get("market_value", 0) or 0)
            pct = mv / total_assets * 100 if total_assets > 0 else 0
            lines.append(f"- {pos.get('name', '')}({pos.get('code', '')}) "
                         f"{pos.get('count', 0)}股 ≈ {mv:,.0f}元（{pct:.1f}%）")
        lines.append("")
    else:
        lines += ["**执行后持仓**：空仓（现金）", ""]
    return finish()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='ETF 跨资产轮动 — 账户建议、独立模拟与报告',
    )
    add_standard_flags(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    setup_logging(log_prefix="etf_rotation", debug=args.debug)

    logger.info("=" * 60)
    logger.info("ETF 跨资产轮动 — 账户建议与独立模拟启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run)
    if skip:
        logger.info("今天不是 A 股交易日，跳过执行")
        return 0
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不写台账")

    try:
        report = run(dry_run=dry_run)
    except Exception as e:
        logger.exception(f"运行失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    save_report(report, "etf_rotation")

    if not args.no_notify:
        notify(report)

    logger.info("运行完成")
    return 0


if __name__ == "__main__":
    # 硬退出理由（SDK 非守护回调线程）单点在 cron_entry.hard_exit。
    hard_exit(main())
