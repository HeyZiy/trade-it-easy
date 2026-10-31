# -*- coding: utf-8 -*-
"""
===================================
名义影子成交台账 — 流水唯一 owner
===================================

单文件 append-only JSONL（data/trade_ledger.jsonl）为持仓/资金唯一事实来源；
视图全部从流水推导，不存可变状态表。写侧两个生产者（cron 影子成交确认、
book.py 手动建仓），读侧一个动词 derive()——产出与妙想 get_positions() **同形**
的持仓 dict（code/name/count/avail_count/cost_price/current_price/market_value/
profit/profit_pct/pos_pct，另附归因用的 account）+ entry_map + 组合敞口，
判定核零改动消费。

口径：
- 单池名义期初 NOMINAL_EQUITY，真实资金数字在系统内不存在；呈现全百分比；
- T+1：date < as_of 的买入才计 avail_count；
- 整手：qty 必为 100 整数倍，append 处校验收敛（判定口径单点仍在 mx/executor）;
- 现金 = 名义期初 − Σ买 + Σ卖 − 已记费用 + 到账分红；旧流水费用默认为0；
- 分红date为除息日，到账前单列应收，pay_date起转为可用现金；无外部调入调出条目；
- account 标签（core/satellite/quality_pool/etf_rotation）仅服务复盘归因与
  持仓归属拆分，不分账；独立策略账户用独立流水文件（如质量池、跨资产动量轮动）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from data_provider import canonical_stock_code

logger = logging.getLogger(__name__)

# ── 常量（名义口径） ──

NOMINAL_EQUITY = 1_000_000.0          # 单池名义期初（元，纯名义）
LOT = 100                              # A 股一手
DEFAULT_LEDGER_PATH = Path("data/trade_ledger.jsonl")

SIDES = ("buy", "sell", "dividend")
ACCOUNTS = ("core", "satellite", "quality_pool", "etf_rotation")
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
    side: str                 # buy | sell | dividend
    qty: int                  # 名义股数，100 整数倍
    price: float              # 名义成交价（影子成交=判定时点现价）
    account: str = "core"     # 归因标签：core/satellite/quality_pool
    reasons: List[str] = field(default_factory=list)
    source: str = "cron"      # cron | book
    manual: bool = False      # True=实盘底仓补记（入场日=该日，不追溯）
    fee: float = 0.0          # 实际模拟费用；旧流水默认0，不追溯补扣
    event_id: str = ""        # 分红事件唯一标识，防止重复入账
    pay_date: str = ""        # 分红到账日；date为除息日，到账前记应收

    def __post_init__(self):
        if self.side not in SIDES:
            raise LedgerError(f"side 须为 {SIDES}，收到 {self.side!r}")
        if self.account not in ACCOUNTS:
            raise LedgerError(f"account 须为 {ACCOUNTS}，收到 {self.account!r}")
        if not _DATE_RE.match(self.date):
            raise LedgerError(f"date 须为 YYYY-MM-DD，收到 {self.date!r}")
        if int(self.qty) != self.qty or self.qty <= 0 or self.qty % LOT != 0:
            raise LedgerError(f"qty 须为正 100 整数倍，收到 {self.qty!r}")
        if not math.isfinite(self.price) or self.price <= 0:
            raise LedgerError(f"price 须为正，收到 {self.price!r}")
        if not math.isfinite(self.fee) or self.fee < 0:
            raise LedgerError("fee 须为有限非负数")
        if self.side == "dividend":
            if not self.event_id or not self.pay_date or self.pay_date < self.date:
                raise LedgerError("分红须有唯一事件标识及不早于除息日的到账日")
            datetime.strptime(self.pay_date, "%Y-%m-%d")
            if self.fee:
                raise LedgerError("分红不得记成交费用")
        elif self.event_id or self.pay_date:
            raise LedgerError("买卖成交不得附带分红字段")


# ── 写侧 ──

def append_trade(record: TradeRecord,
                 path: Path | str = DEFAULT_LEDGER_PATH) -> TradeRecord:
    """校验后追加一行 JSONL。record.code 先经 canonical_stock_code 归一。"""
    from src.real_account import ACCOUNT_PATH
    if Path(path) == DEFAULT_LEDGER_PATH and ACCOUNT_PATH.exists():
        raise LedgerError("核心配置已使用实际账户，请在网页确认成交记账")
    record = TradeRecord(**{**asdict(record),
                            "code": canonical_stock_code(record.code)})
    p = Path(path)
    if record.event_id:
        for existing in load_trades(p):
            if existing.event_id == record.event_id:
                if existing != record:
                    raise LedgerError("同一分红事件内容不一致，须核对后处理")
                return existing
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

def held_counts_of(positions: list) -> dict:
    """妙想同形持仓 dict 列表 → {code: 持仓股数}（读侧解析单点，勿在入口重写）。"""
    return {p["code"]: int(p["count"]) for p in positions}


@dataclass
class LedgerSnapshot:
    """一次推导的完整视图：持仓供料 + 入场日 + 资金敞口。"""
    def held_counts(self) -> dict:
        """{code: 持仓股数}——妙想同形持仓 dict 的读侧标准解析单点。

        入口脚本原各自写一遍字典推导（妙想-dict 形状漏出接口的代价），
        收编此处后形状知识不再过 seam。"""
        return held_counts_of(self.positions)
    positions: List[dict]
    entry_map: Dict[str, str]
    cash: float
    invested: float
    total_assets: float
    receivables: float = 0.0


def _empty(code: str, name: str) -> dict:
    return {"code": code, "name": name, "count": 0, "avail": 0,
            "cost": 0.0, "buy_total": 0, "last_buy": None, "account": "core"}


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
    receivables = 0.0
    seen_events = set()
    for t in sorted(trades, key=lambda x: x.date):
        if t.date > as_of:
            continue
        if t.side == "dividend":
            if t.event_id in seen_events:
                raise LedgerError("分红事件重复，流水不自洽")
            seen_events.add(t.event_id)
            amount = round(t.qty * t.price, 2)
            if t.pay_date <= as_of:
                cash += amount
            else:
                receivables += amount
            continue
        s = state.setdefault(t.code, _empty(t.code, t.name))
        if t.name:
            s["name"] = t.name
        if t.side == "buy":
            avg = s["cost"] * s["count"] + t.price * t.qty + t.fee
            s["count"] += t.qty
            s["cost"] = avg / s["count"]
            if t.date < as_of:
                s["avail"] += t.qty
            s["buy_total"] += t.qty
            # 归属取末次买入标签（卫星标的由池选定，与核心基准代码不相交，不存在混买）
            s["account"] = t.account
            s["last_buy"] = t.date if (s["last_buy"] is None
                                       or t.date >= s["last_buy"]) else s["last_buy"]
            cash -= t.price * t.qty + t.fee
        else:  # sell
            if t.qty > s["count"]:
                raise LedgerError(
                    f"卖出 {t.code} {t.date} {t.qty}股 > 台账持仓 {s['count']}股，流水不自洽")
            s["count"] -= t.qty
            s["avail"] = max(0, s["avail"] - t.qty)
            cash += t.price * t.qty - t.fee
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
            "account": s["account"],
        })
        if s["last_buy"]:
            entry_map[code] = s["last_buy"]

    total_assets = cash + invested + receivables
    if total_assets > 0:
        for pos in positions:
            pos["pos_pct"] = pos["market_value"] / total_assets * 100
    positions.sort(key=lambda p: -p["market_value"])
    return LedgerSnapshot(positions=positions, entry_map=entry_map,
                          cash=cash, invested=invested,
                          total_assets=total_assets, receivables=receivables)


def derive_from_file(as_of: str, prices: Optional[Dict[str, float]] = None,
                     path: Path | str = DEFAULT_LEDGER_PATH) -> LedgerSnapshot:
    """load + derive 的便捷入口（消费方统一走这里，不自行拼文件路径）。"""
    return derive(load_trades(path), as_of, prices)
