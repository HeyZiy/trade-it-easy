# -*- coding: utf-8 -*-
"""
===================================
行业动量轮动 — 尾盘执行（每交易日 14:45~14:55）
===================================

每交易日尾盘运行，卫星仓日频截面轮动的执行入口：

1. 读名义成交台账推导持仓与资金（docs/trade_ledger.md：持仓事实来源，
   derive 供料与妙想持仓 dict 同形，判定核零改动）
2. industry_momentum.analyze_rotation：截面排名 → 卖出（跌出前 40%）
   → 买入（准入过滤后前 3 等权补足空槽）
3. 卖出优先于买入，命中指令经 execute_batch 记入名义台账（影子成交，
   信号当日收盘价成交形态；真实账户按报告手动执行）
4. 渲染成交报告 → 落盘 + 推送

执行约定：
- 通用批次安全校验（卖出股数 ≤ 台账持仓；买入总额 ≤ 台账现金 + 卖出回款）
  单点在 trade_ledger.execute_batch，任一失败整批中止；本入口只保留策略
  自有的卫星 10% 预算前置校验。
- 整手收敛/逐笔隔离记账同在 execute_batch（单笔 append 失败不阻断其余，
  报告标记记账失败）。
"""

import argparse
import io
import logging
import sys
from datetime import date, datetime
from typing import List

from src.config import setup_env
from src.logging_config import setup_logging
from src.trading_calendar import is_trading_day

setup_env()

from src.etf.industry_momentum import (           # noqa: E402
    SATELLITE_BUDGET_RATIO, RotationOrder, analyze_rotation,
)
from src.task_io import notify, save_report       # noqa: E402
from src.trade_ledger import (                    # noqa: E402
    derive, load_trades, resolve_prices,
)

logger = logging.getLogger(__name__)


def _execute(orders: List[RotationOrder], trade_date: str, *,
             ledger_path=None):
    """执行一批轮动指令：批次语义（收敛/安全校验/先卖后买/逐单记账）
    委托 trade_ledger.execute_batch，本函数只映射指令与渲染结果行。

    返回 (abort, lines)：abort 非空 = 通用安全校验未过，整批零记账。"""
    from src.trade_ledger import BatchOrder, execute_batch

    batch_orders = [BatchOrder(code=o.code, name=o.name, side=o.action,
                               qty=o.shares, price=o.price, reason=o.reason)
                    for o in orders]
    res = execute_batch(batch_orders, account="satellite",
                        trade_date=trade_date, path=ledger_path)
    lines = [oc.outcome_line for oc in res.outcomes]
    return res.abort, lines


def _pool_line(diag: dict) -> str:
    """池状态报告行：成员/截面/实时价缺失；重建日附各过滤关计数。"""
    line = (f"**池**: 成员 {diag.get('pool', 0)} 只 | 入截面 {diag.get('n', 0)} 只"
            f" | 实时价缺失 {diag.get('realtime_missing', 0)}")
    rb = diag.get("rebuild")
    if rb:
        line += (f"（今日重建：全表 {rb.get('universe', 0)} → 名称过 {rb.get('cands', 0)}"
                 f" → 成熟 {rb.get('mature', 0)} → 流动 {rb.get('liquid', 0)}"
                 f" → 去重后 {rb.get('members', 0)}）")
    return line


def run(dry_run: bool = False) -> str:
    """尾盘闭环：台账持仓 → 截面判定 → 安全校验 → 记账 → 报告。"""
    trade_date = date.today().isoformat()
    trades = load_trades()
    held = derive(trades, trade_date)
    prices = resolve_prices([p["code"] for p in held.positions])
    snap = derive(trades, trade_date, prices)
    positions = snap.positions
    total_assets = snap.total_assets
    avail_balance = snap.cash

    plan = analyze_rotation(positions, total_assets, avail_balance)
    rows = plan["rows"]
    sells, buys = plan["sells"], plan["buy_orders"]

    lines = [
        f"# 行业动量轮动 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        f"{'（dry-run，未记账）' if dry_run else ''}",
        "",
        f"**总资产(名义)**: {total_assets:,.0f} 元 | **可用资金(名义)**: {avail_balance:,.0f} 元"
        f" | **卫星持仓**: {plan['satellite_mv']:,.0f} 元"
        f"（预算上限 {total_assets * SATELLITE_BUDGET_RATIO:,.0f} 元）",
        _pool_line(plan["diag"]),
        "",
    ]

    # 截面前列（观察用）
    lines += ["## 一、截面前列（动量评分）", ""]
    for r in rows[:8]:
        mark = " 📌持有" if r.code in plan["held_codes"] else ""
        crowd = f"{r.crowd:.0f}%" if r.crowd is not None else "—"
        lines.append(f"- {r.rank}. {r.name}({r.code}) score {r.score:+.4f}"
                     f" | 拥挤度 {crowd}{mark}")
    lines.append("")

    # 策略自有前置校验（卫星槽位预算上限，policy 归本入口）；
    # 通用批次安全校验（卖量≤持仓、买额≤现金+回款）单点在 execute_batch
    sell_amount = sum(o.amount for o in sells)
    buy_amount = sum(o.amount for o in buys)
    budget = total_assets * SATELLITE_BUDGET_RATIO
    abort = None
    if buys:
        after_sell_mv = plan["satellite_mv"] - sell_amount
        if after_sell_mv + buy_amount > budget:
            abort = (f"卫星仓位 {after_sell_mv + buy_amount:,.0f} 元将超预算上限 {budget:,.0f} 元")
    if abort:
        lines += ["## ⛔ 安全校验未通过，本次不执行", "", f"- {abort}", ""]
        return "\n".join(lines)

    lines += ["## 二、执行结果", ""]
    if not sells and not buys:
        lines += ["无调仓指令，保持当前持仓。", ""]
        return "\n".join(lines)

    orders = sells + buys
    if dry_run:
        lines.append("**dry-run：以下指令未记账**")
        for o in orders:
            lines.append(f"- [DRY] {o.action.upper()} {o.name}({o.code})"
                         f" {o.shares}股 ≈ {o.amount:,.0f}元（{o.reason}）")
    else:
        abort, exec_lines = _execute(orders, trade_date)
        if abort:
            lines += [f"⛔ 安全校验未通过，本次不执行：{abort}", ""]
            return "\n".join(lines)
        lines += exec_lines
    lines.append("")

    # 执行后持仓：台账重推导（成交价补进现价表，不二次取数）
    if not dry_run:
        prices.update({o.code: o.price for o in orders if o.price > 0})
        fresh = derive(load_trades(), trade_date, prices).positions
    else:
        fresh = positions
    if fresh:
        lines += ["**执行后持仓**：", ""]
        for p in fresh:
            mv = float(p.get("market_value", 0) or 0)
            pct = mv / total_assets * 100 if total_assets > 0 else 0
            lines.append(f"- {p.get('name', '')}({p.get('code', '')})"
                         f" {p.get('count', 0)}股 ≈ {mv:,.0f}元（{pct:.1f}%）")
        lines.append("")
    return "\n".join(lines)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='行业动量轮动 — 尾盘执行（截面排名 + 台账记账 + 出报告）',
    )
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    parser.add_argument('--no-notify', action='store_true', help='不发送推送通知')
    parser.add_argument('--dry-run', action='store_true', help='只检测不记账（调试用）')
    parser.add_argument('--force', action='store_true', help='跳过交易日检查（手动调试用）')
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    setup_logging(log_prefix="industry_momentum", debug=args.debug)

    logger.info("=" * 60)
    logger.info("行业动量轮动 — 尾盘执行启动")
    logger.info(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    if not args.force and not is_trading_day():
        logger.info("今天不是 A 股交易日，跳过执行")
        return 0

    try:
        report = run(dry_run=args.dry_run)
    except Exception as e:
        logger.exception(f"运行失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    save_report(report, "industry_momentum")

    if not args.no_notify:
        notify(report)

    logger.info("运行完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
