"""真实账户的跨资产轮动评分、目标与调仓计划；只给建议，不执行交易。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from data_provider.etf import EtfBars, etf_daily_bars
from data_provider.security_names import security_name
from src.etf.momentum import DIVE_RATIO, EtfOrder, weighted_trend_score
from src.mx.executor import floor_lot
from src.trading_calendar import latest_trading_day_on_or_before


MOMENTUM_WINDOWS = (15, 20, 25, 30)
LOOKBACK_DAYS = max(MOMENTUM_WINDOWS)
TARGET_COUNT = 2
SCORE_LOW = 0.0          # 合格下限（严格大于）
SCORE_HIGH = 5.0         # 合格上限（严格小于）
STALE_DAYS = 10          # 交易日历不可用时：最后日线距今 > N 自然日 → 剔除

ACCOUNT = "etf_rotation"
COMMISSION = 0.0002      # 轮动建议预估佣金
MIN_COMMISSION = 5.0     # 新成交最低佣金5元


def pool_from_config(config) -> Tuple[Tuple[str, str, str], ...]:
    """生效池唯一来源：账户配置 rotation.pool；未设置时拒绝运行。"""
    members = (config.get("rotation") or {}).get("pool")
    if not members:
        raise ValueError("账户配置缺少轮动标的池 rotation.pool，请在配置页设置")
    return tuple((m["code"], security_name(m["code"]), m["group"]) for m in members)


# ── 数据对象 ──

@dataclass
class RotationRow:
    """单只 ETF 的当日截面事实；判定核只读此表。"""
    code: str
    name: str
    group: str
    close: float          # 最近一根已完成日线的复权收盘（锚定最新原始价）
    price: float          # 实时报价；缺失时收盘价仅供截面展示，入口不执行
    score: float          # 动量评分；数据不足/非法为 nan，跳水为 0
    eligible: bool = False
    rank: int = 0         # 合格候选内排名（1 = 最强）；不合格为 0


# ── 判定核（零 I/O）──


def ensemble_score(closes: Sequence[float], last_price: float) -> float:
    prices = [*closes[-LOOKBACK_DAYS:], last_price]
    if len(prices) != LOOKBACK_DAYS + 1 or not all(
            math.isfinite(price) and price > 0 for price in prices):
        return math.nan
    if any(current / previous < DIVE_RATIO
           for previous, current in zip(prices[-4:-1], prices[-3:])):
        return 0.0
    try:
        return math.fsum(weighted_trend_score(prices[-(window + 1):])
                         for window in MOMENTUM_WINDOWS) / len(MOMENTUM_WINDOWS)
    except OverflowError:
        return math.nan


def is_eligible(score: float) -> bool:
    """合格候选：有限分且 0 < score < 5（严格两端）。"""
    return math.isfinite(score) and SCORE_LOW < score < SCORE_HIGH


def affordable_shares(cash: float, price: float) -> int:
    """资金可行性预检：在扣除佣金（万二、最低 5 元）后能买的最大整手股数。

    台账执行会扣除同口径费用；此处保证计划金额包含买入费用。
    """
    if not (math.isfinite(cash) and math.isfinite(price)) or cash <= 0 or price <= 0:
        return 0
    qty = int(cash // price // 100) * 100
    while qty > 0:
        cost = qty * price
        if cost + max(MIN_COMMISSION, cost * COMMISSION) <= cash + 1e-9:
            break
        qty -= 100
    return max(0, qty)


def _prepare_bars(df: pd.DataFrame, decision_date: date,
                  prev_trading_day: Optional[date]) -> Tuple[Optional[List[float]],
                                                            Optional[float], str]:
    """单只日线 → (30 根已完成收盘, 当日 bar 收盘 fallback, 跳过原因)。

    决策日当天的 bar 不进历史窗口（研究线用 30 根此前日线 + 当日现价）；
    若供应商日线盘中已包含当日 bar，其收盘作为现价缺失时的回退。
    日历可用时严格要求最后一根历史 bar = 上一交易日（与聚宽 skip_paused=False
    下缺行即 NaN 的口径一致）；日历不可用时退化为自然日陈旧检查。
    """
    if df is None or df.empty or "close" not in df.columns:
        return None, None, "no_bars"
    frame = df.copy()
    frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[~frame.index.isna()].sort_index()
    if frame.index.has_duplicates:
        return None, None, "duplicate"
    closes = pd.to_numeric(frame["close"], errors="coerce")

    decision = pd.Timestamp(decision_date)
    history = closes[frame.index < decision]
    today_bar = closes[frame.index == decision]
    intraday_close = float(today_bar.iloc[-1]) if len(today_bar) else None
    if intraday_close is not None and not (math.isfinite(intraday_close)
                                           and intraday_close > 0):
        intraday_close = None

    # 只有 30 个历史点可进入打分；交易日历可用时还要求最后一根就是上一交易日。
    # 先按"上一交易日"截断，再检查短缺，避免把更早的历史补成 30 根。
    if prev_trading_day is not None:
        history = history[history.index <= pd.Timestamp(prev_trading_day)]
    if len(history) < LOOKBACK_DAYS:
        return None, intraday_close, "short"
    tail = [float(v) for v in history.values[-LOOKBACK_DAYS:]]
    if not all(math.isfinite(v) and v > 0 for v in tail):
        return None, intraday_close, "bad_close"
    if prev_trading_day is not None:
        if history.index[-1].date() != prev_trading_day:
            return None, intraday_close, "stale"
    elif (decision_date - history.index[-1].date()).days > STALE_DAYS:
        return None, intraday_close, "stale"
    return tail, intraday_close, ""


def build_rows(bars: Dict[str, pd.DataFrame], prices: Dict[str, float], *,
               decision_date: date, prev_trading_day: Optional[date] = None,
               pool: Sequence[Tuple[str, str, str]]
               ) -> Tuple[List[RotationRow], dict]:
    """当日截面：打分 + 合格判定 + 排名（合格候选按 score 降序，1 = 最强）。

    prices[code] = 当日现价（实时报价）；缺失回退决策日 bar 收盘，再退最近
    已完成收盘，仅供展示，并计入diag["realtime_missing"]；入口据此暂停执行。
    """
    rows: List[RotationRow] = []
    skipped = {"no_bars": 0, "short": 0, "stale": 0, "bad_close": 0,
               "duplicate": 0}
    realtime_missing = 0
    for code, name, group in pool:
        tail, intraday_close, reason = _prepare_bars(
            bars.get(code), decision_date, prev_trading_day)
        if tail is None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        price = float(prices.get(code) or 0.0)
        if not (math.isfinite(price) and price > 0):
            price = intraday_close if intraday_close is not None else tail[-1]
            realtime_missing += 1
        score = ensemble_score(tail, price)
        rows.append(RotationRow(code=code, name=name, group=group,
                                close=tail[-1], price=price, score=score))

    for row in rows:
        row.eligible = is_eligible(row.score)
    eligible = [r for r in rows if r.eligible]
    eligible.sort(key=lambda r: (-r.score, r.code))
    for i, row in enumerate(eligible, 1):
        row.rank = i
    others = [r for r in rows if not r.eligible]
    others.sort(key=lambda r: (math.isnan(r.score), -(r.score if math.isfinite(r.score)
                                                      else -math.inf), r.code))
    rows = eligible + others
    diag = {
        "decision_date": decision_date.isoformat(),
        "prev_trading_day": prev_trading_day.isoformat() if prev_trading_day else None,
        "pool": len(pool), "n": len(rows), "eligible": len(eligible),
        "realtime_missing": realtime_missing, "skipped": skipped,
    }
    return rows, diag


def top_targets(rows: Sequence[RotationRow]) -> List[RotationRow]:
    return [row for row in rows if row.eligible][:TARGET_COUNT]


def _position_price(position: dict, prices: Dict[str, float]) -> float:
    for value in (prices.get(position["code"]), position.get("current_price")):
        price = float(value or 0)
        if math.isfinite(price) and price > 0:
            return price
    return 0.0


def _order_fee(amount: float) -> float:
    return round(max(MIN_COMMISSION, amount * COMMISSION), 2)


def plan_rotation(rows: Sequence[RotationRow], positions: List[dict],
                  prices: Dict[str, float], avail_cash: float, *,
                  account: str = ACCOUNT, budget: float = math.inf,
                  rebalance: Optional[Dict[str, int]] = None) -> dict:
    """仅在成员变化时配平等权，或继续完成已开始的调仓。"""
    targets = {row.code: row for row in top_targets(rows)}
    held = {pos["code"]: pos for pos in positions
            if pos.get("account") == account and int(pos.get("count") or 0) > 0}
    sells: List[EtfOrder] = []
    buys: List[EtfOrder] = []
    plan = {"targets": list(targets), "sells": sells, "buys": buys, "orders": [], "issue": None}
    if rebalance is None and held.keys() == targets.keys():
        return plan

    held_prices = {code: _position_price(pos, prices) for code, pos in held.items()}
    if any(price <= 0 for price in held_prices.values()):
        plan["issue"] = "持仓缺现价，等待更新"
        return plan

    held_shares = {code: int(position["count"]) for code, position in held.items()}
    target_count = len(targets)
    desired = rebalance
    if desired is None:
        equity = avail_cash + math.fsum(held_shares[code] * price for code, price in held_prices.items())
        allocation = min(equity, budget) / target_count if target_count else 0
        desired = {code: affordable_shares(allocation, row.price) for code, row in targets.items()}
    pool_codes = {row.code for row in rows}
    for code, position in held.items():
        quantity = held_shares[code]
        if code in targets:
            quantity = floor_lot(max(0, quantity - desired[code]))
            reason = "成员变更，等权配平"
        elif code not in pool_codes:
            reason = "已移出池子，兜底卖出"
        else:
            reason = "退出目标组合" if targets else "无合格候选，保持现金"
        if quantity > 0:
            price = held_prices[code]
            sells.append(EtfOrder(code, position.get("name") or code, "sell",
                                  quantity, price, quantity * price, reason))
    sells.sort(key=lambda order: -order.amount)
    cash = avail_cash + math.fsum(order.amount - _order_fee(order.amount) for order in sells)
    if cash < 0 and sells:
        plan["issue"] = "策略现金不足以扣除卖出费用"
        return plan
    budget_per_target = budget / target_count if target_count else 0
    for code, row in targets.items():
        target_quantity = desired[code]
        if math.isfinite(budget_per_target):
            target_quantity = min(target_quantity, affordable_shares(budget_per_target, row.price))
        quantity = min(floor_lot(max(0, target_quantity - held_shares.get(code, 0))),
                       affordable_shares(cash, row.price))
        if quantity <= 0:
            continue
        reason = ("完成本轮剩余买入" if rebalance is not None else
                  f"合格候选第{row.rank}名（score={row.score:+.4f}），等权配置")
        amount = quantity * row.price
        buys.append(EtfOrder(code, row.name, "buy", quantity, row.price, amount, reason))
        cash -= amount + _order_fee(amount)
    plan["orders"] = sells + buys
    return plan


# ── 编排（取数 + 决策组装，不执行交易）──

def fetch_bars(codes: Sequence[str], decision_date: date, *,
               prev_trading_day: Optional[date] = None) -> Dict[str, EtfBars]:
    def history_issue(history: pd.DataFrame) -> str:
        _, _, reason = _prepare_bars(history, decision_date, prev_trading_day)
        return reason

    return etf_daily_bars(
        codes, decision_date.isoformat(), policy="rotation", history_issue=history_issue,
        completed_through=prev_trading_day.isoformat() if prev_trading_day is not None else None,
    )


def fetch_prices(codes: Sequence[str]) -> Dict[str, float]:
    """池成员当日现价，单只缺价由入口阻止当次执行。"""
    from data_provider import realtime_prices

    return realtime_prices(codes)


def rotation_snapshot(decision_date: Optional[date] = None, *,
                      pool: Sequence[Tuple[str, str, str]],
                      held_codes: Sequence[str] = ()
                      ) -> Tuple[List[RotationRow], Dict[str, float], dict]:
    """当日截面 + 现价（取数在编排层，判定核零 I/O）。

    held_codes 为当前轮动持仓：不在池内的持仓不参与打分，但一并取现价，
    供建议层兜底卖出；缺价时建议层照常暂停。
    """
    decision_date = decision_date or date.today()
    previous = latest_trading_day_on_or_before(decision_date - timedelta(days=1),
                                               fallback=None)
    codes = [code for code, _, _ in pool]
    series = fetch_bars(codes, decision_date=decision_date, prev_trading_day=previous)
    bars = {code: item.history for code, item in series.items()}
    price_codes = [code for code in codes if code in bars]
    extra = [code for code in held_codes if code not in price_codes]
    prices = fetch_prices(price_codes + extra)
    rows, diag = build_rows(bars, prices, decision_date=decision_date,
                            prev_trading_day=previous, pool=pool)
    diag["bar_sources"] = {code: item.source for code, item in series.items()}
    diag["missing_bars"] = [code for code in codes if code not in bars]
    return rows, prices, diag
