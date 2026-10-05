# -*- coding: utf-8 -*-
"""
===================================
成交台账 CLI — 手动记账与查账（book.py）
===================================

名义影子台账的人工入口（规格见 docs/trade_ledger.md）：

- open：手动记建仓（实盘底仓补记）——给成本价与仓位权重%，
  换算名义整手股数写入流水（manual=True，入场日=该日不追溯）；
- view：渲染当前持仓/权重/名义现金，兼对账自检——
  流水截止日陈旧度提示。

约定：卖出/买侧影子成交由各任务 cron 自动记账，不经本 CLI；
真实资金数字不在系统内——权重% → 钱的乘法在实盘下单时自算。
"""

import argparse
import io
import logging
import sys
from datetime import date

from src.config import setup_env
from src.logging_config import setup_logging
from src.mx.executor import round_lot
from src.trade_ledger.ledger import (
    ACCOUNTS, NOMINAL_EQUITY, DEFAULT_LEDGER_PATH, TradeRecord,
    append_trade, derive, load_trades,
)

setup_env()
logger = logging.getLogger(__name__)


def cmd_open(args) -> int:
    weight_pct = args.weight
    if not 0 < weight_pct <= 100:
        print(f"❌ 权重须在 (0,100]，收到 {weight_pct}")
        return 1
    qty = round_lot(NOMINAL_EQUITY * weight_pct / 100 / args.price)
    if qty <= 0:
        print("❌ 权重换算不足一手，请提高权重或检查价格")
        return 1
    rec = TradeRecord(
        date=args.date or date.today().isoformat(),
        code=args.code, name=args.name, side="buy", qty=qty,
        price=args.price, account=args.account,
        reasons=["manual_base"], source="book", manual=True,
    )
    append_trade(rec, path=args.path)
    print(f"✅ 建仓 {rec.name}({rec.code}) {qty}股 @ {rec.price} "
          f"≈ 权重 {weight_pct}%（名义），account={rec.account}")
    return 0


def cmd_view(args) -> int:
    trades = load_trades(args.path)
    as_of = args.as_of or date.today().isoformat()
    snap = derive(trades, as_of=as_of)  # 无现价：市值按 0，权重列按成本口径
    acct_filter = args.account
    shown = [p for p in snap.positions
             if acct_filter is None or _acct_of(trades, p["code"]) == acct_filter]

    print(f"# 成交台账视图（名义口径，as_of={as_of}）")
    last = trades[-1].date if trades else None
    today = date.today().isoformat()
    if last is None:
        print("- 流水为空：空仓起步（用 book.py open 补记实盘底仓）")
    else:
        stale = "（⚠️ 早于今日，本地副本可能陈旧——记得 rsync 回流）" if last < today else ""
        print(f"- 流水 {len(trades)} 笔，截止 {last}{stale}")
    print(f"- 名义现金 {snap.cash:,.0f} / 期初 {NOMINAL_EQUITY:,.0f}")

    if shown:
        print(f"\n| 代码 | 名称 | 股数 | 可卖(T+1) | 成本 | 入场日 | 成本权重 |")
        print("|---|---|---|---|---|---|---|")
        denom = sum(p["cost_price"] * p["count"] for p in shown) or 1.0
        for p in shown:
            w = p["cost_price"] * p["count"] / denom * 100
            entry = snap.entry_map.get(p["code"], "—")
            print(f"| {p['code']} | {p['name']} | {p['count']} | "
                  f"{p['avail_count']} | {p['cost_price']:.2f} | {entry} | {w:.1f}% |")
    else:
        print("- 当前无持仓")

    return 0


def _acct_of(trades, code: str) -> str:
    """持仓归因标签 = 该 code 最新一笔流水的 account（单标签假设）。"""
    for t in reversed(trades):
        if t.code == code:
            return t.account
    return ""


def main() -> int:
    # Windows 控制台 GBK 兜底（同 etf_observe 先例）：CLI 输出含中文与符号
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    parser = argparse.ArgumentParser(description="名义影子成交台账 CLI")
    parser.add_argument("--path", default=str(DEFAULT_LEDGER_PATH),
                        help="流水 JSONL 路径（默认 data/trade_ledger.jsonl）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("open", help="手动记建仓（实盘底仓补记）")
    po.add_argument("code")
    po.add_argument("name")
    po.add_argument("--price", type=float, required=True, help="成本价（近似即可）")
    po.add_argument("--weight", type=float, required=True, help="仓位权重 %%（0-100）")
    po.add_argument("--account", choices=ACCOUNTS, default="core")
    po.add_argument("--date", default=None, help="建仓日，默认今天")

    pv = sub.add_parser("view", help="渲染当前持仓与自检")
    pv.add_argument("--account", choices=ACCOUNTS, default=None)
    pv.add_argument("--as-of", default=None, help="推导截止日，默认今天")

    args = parser.parse_args()
    setup_logging()
    if args.cmd == "open":
        return cmd_open(args)
    return cmd_view(args)


if __name__ == "__main__":
    sys.exit(main())
