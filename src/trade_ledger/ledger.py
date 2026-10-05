# -*- coding: utf-8 -*-
"""
===================================
名义影子成交台账 — 流水唯一 owner
===================================

单文件 append-only JSONL（data/trade_ledger.jsonl）为持仓/资金唯一事实来源；
视图全部从流水推导，不存可变状态表。写侧两个生产者（cron 影子成交确认、
book.py 手动建仓），读侧一个动词 derive()——产出与妙想 get_positions() **同形**
的持仓 dict（code/name/count/avail_count/cost_price/current_price/market_value/
profit/profit_pct/pos_pct）+ entry_map + 组合敞口，判定核零改动消费。

口径（docs/trade_ledger.md 决策，改动须同步该文档）：
- 单池名义期初 NOMINAL_EQUITY，真实资金数字在系统内不存在；呈现全百分比；
- T+1：date < as_of 的买入才计 avail_count；
- 整手：qty 必为 100 整数倍，append 处校验收敛（判定口径单点仍在 mx/executor）;
- 现金 = 名义期初 − Σ买 + Σ卖，无手续费、无调入调出条目；
- account 标签（core/satellite/quality_pool）仅服务复盘归因，不分账。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from data_provider import canonical_stock_code

logger = logging.getLogger(__name__)

# ── 常量（名义口径，改动须同步 docs/trade_ledger.md） ──

NOMINAL_EQUITY = 1_000_000.0          # 单池名义期初（元，纯名义）
LOT = 100                              # A 股一手
DEFAULT_LEDGER_PATH = Path("data/trade_ledger.jsonl")

SIDES = ("buy", "sell")
ACCOUNTS = ("core", "satellite", "quality_pool")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class LedgerError(ValueError):
    """台账契约违规：流水坏行/字段非法/推导不自洽。宁可炸，不可默默错账。"""


# ── 流水记录 ──

@dataclass(frozen=True)
class TradeRecord:
    """一笔成交（影子或手动补记）。字段协议见类注解，扩展走"加列容忍旧行"。"""
    date: str                 # YYYY-MM-DD 交易日
    code: str                 # canonical 归一后的代码
    name: str
    side: str                 # buy | sell
    qty: int                  # 名义股数，100 整数倍
    price: float              # 名义成交价（影子成交=判定时点现价）
    account: str = "core"     # 归因标签：core/satellite/quality_pool
    reasons: List[str] = field(default_factory=list)
    source: str = "cron"      # cron | book
    manual: bool = False      # True=实盘底仓补记（入场日=该日，不追溯）

    def __post_init__(self):
        if self.side not in SIDES:
            raise LedgerError(f"side 须为 {SIDES}，收到 {self.side!r}")
        if self.account not in ACCOUNTS:
            raise LedgerError(f"account 须为 {ACCOUNTS}，收到 {self.account!r}")
        if not _DATE_RE.match(self.date):
            raise LedgerError(f"date 须为 YYYY-MM-DD，收到 {self.date!r}")
        if int(self.qty) != self.qty or self.qty <= 0 or self.qty % LOT != 0:
            raise LedgerError(f"qty 须为正 100 整数倍，收到 {self.qty!r}")
        if self.price <= 0:
            raise LedgerError(f"price 须为正，收到 {self.price!r}")


# ── 写侧 ──

def append_trade(record: TradeRecord,
                 path: Path | str = DEFAULT_LEDGER_PATH) -> TradeRecord:
    """校验后追加一行 JSONL。record.code 先经 canonical_stock_code 归一。"""
    record = TradeRecord(**{**asdict(record),
                            "code": canonical_stock_code(record.code)})
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(record), ensure_ascii=False)
    with p.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    logger.info("台账记账 %s %s %s [%s] %s股 @%.2f",
                record.date, record.side, record.code, record.account,
                record.qty, record.price)
    return record


def load_trades(path: Path | str = DEFAULT_LEDGER_PATH) -> List[TradeRecord]:
    """读全部流水。坏行直接抛 LedgerError（台账是唯一事实来源，拒绝静默跳行）；
    未知字段容忍（加列演进），缺失必填字段炸。"""
    p = Path(path)
    if not p.exists():
        return []
    trades: List[TradeRecord] = []
    known = {f for f in TradeRecord.__dataclass_fields__}
    for i, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LedgerError(f"台账第 {i} 行 JSON 损坏: {e}") from e
        extra = set(obj) - known
        if extra:
            logger.debug("台账第 %d 行含未知字段（容忍）: %s", i, sorted(extra))
            obj = {k: v for k, v in obj.items() if k in known}
        try:
            obj["reasons"] = list(obj.get("reasons", []))
            trades.append(TradeRecord(**obj))
        except (TypeError, LedgerError) as e:
            raise LedgerError(f"台账第 {i} 行不合契约: {e}") from e
    return sorted(trades, key=lambda t: t.date)  # 稳定排序，补记旧日期仍按时间走


# ── 读侧推导 ──

@dataclass
class LedgerSnapshot:
    """一次推导的完整视图：持仓供料 + 入场日 + 资金敞口。"""
    positions: List[dict]
    entry_map: Dict[str, str]
    cash: float
    invested: float
    total_assets: float


def _empty(code: str, name: str) -> dict:
    return {"code": code, "name": name, "count": 0, "avail": 0,
            "cost": 0.0, "buy_total": 0, "last_buy": None}


def derive(trades: List[TradeRecord], as_of: str,
           prices: Optional[Dict[str, float]] = None) -> LedgerSnapshot:
    """流水 → 当日判定视图。纯函数，零 I/O。

    Args:
        trades: load_trades 输出（内部再按 date 稳定排序）
        as_of: 判定日 YYYY-MM-DD；T+1 口径 date < as_of 才可卖
        prices: {code: 现价}，缺失按 0（市值 0，与妙取数失败口径一致）
    """
    prices = prices or {}
    if not _DATE_RE.match(as_of):
        raise LedgerError(f"as_of 须为 YYYY-MM-DD，收到 {as_of!r}")

    state: Dict[str, dict] = {}
    cash = NOMINAL_EQUITY
    for t in sorted(trades, key=lambda x: x.date):
        s = state.setdefault(t.code, _empty(t.code, t.name))
        if t.name:
            s["name"] = t.name
        if t.side == "buy":
            avg = s["cost"] * s["count"] + t.price * t.qty
            s["count"] += t.qty
            s["cost"] = avg / s["count"]
            if t.date < as_of:
                s["avail"] += t.qty
            s["buy_total"] += t.qty
            s["last_buy"] = t.date if (s["last_buy"] is None
                                       or t.date >= s["last_buy"]) else s["last_buy"]
            cash -= t.price * t.qty
        else:  # sell
            if t.qty > s["count"]:
                raise LedgerError(
                    f"卖出 {t.code} {t.date} {t.qty}股 > 台账持仓 {s['count']}股，流水不自洽")
            s["count"] -= t.qty
            s["avail"] = max(0, s["avail"] - t.qty)
            cash += t.price * t.qty
        if s["count"] == 0:
            s["avail"] = 0
            s["cost"] = 0.0
            s["last_buy"] = None
            s["buy_total"] = 0

    positions: List[dict] = []
    entry_map: Dict[str, str] = {}
    invested = 0.0
    for code, s in state.items():
        if s["count"] <= 0:
            continue
        cur = float(prices.get(code, 0.0) or 0.0)
        mv = s["count"] * cur
        invested += mv
        cost = s["cost"]
        profit_pct = (cur / cost - 1) * 100 if cost > 0 and cur > 0 else 0.0
        positions.append({
            "code": code, "name": s["name"],
            "count": s["count"], "avail_count": s["avail"],
            "cost_price": round(cost, 4), "current_price": cur,
            "market_value": mv, "profit": mv - cost * s["count"],
            "profit_pct": profit_pct, "pos_pct": 0.0,  # 下方统一回填
        })
        if s["last_buy"]:
            entry_map[code] = s["last_buy"]

    total_assets = cash + invested
    if total_assets > 0:
        for pos in positions:
            pos["pos_pct"] = pos["market_value"] / total_assets * 100
    positions.sort(key=lambda p: -p["market_value"])
    return LedgerSnapshot(positions=positions, entry_map=entry_map,
                          cash=cash, invested=invested,
                          total_assets=total_assets)


def derive_from_file(as_of: str, prices: Optional[Dict[str, float]] = None,
                     path: Path | str = DEFAULT_LEDGER_PATH) -> LedgerSnapshot:
    """load + derive 的便捷入口（消费方统一走这里，不自行拼文件路径）。"""
    return derive(load_trades(path), as_of, prices)
