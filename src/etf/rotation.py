# -*- coding: utf-8 -*-
"""
===================================
ETF 跨资产动量轮动 — 单只全仓引擎
===================================

把研究线 `research/studies/etf_rotation/unified_coverage.py`（聚宽单文件策略）
移植为本地日频模拟账户的判定核，交易语义保持一致：

  1. 标的池：固定 15 只 ETF，跨国内权益 / 海外权益 / 商品 / 利率四类资产；
     池是研究版本的一部分，不按回测收益增删（改池须同步策略文档与研究记录）。
  2. 打分：25 根已完成日线收盘 + 当日现价共 26 点，对对数价格做加权线性回归
     （权重 w 从 1 线性升到 2），score = 年化涨幅 × 加权 R²；
     近 3 个日环比任一 < 0.95（单日跳水）则 score 记 0。
  3. 合格候选：0 < score < 5（严格两端，上限 5 是研究线的实际过滤规则）。
  4. 仓位：只在合格候选中排名第一的一只上全仓；无合格候选则空仓。
  5. 换仓：现持仓不再是合格候选第一名时，卖出并买入新的第一名；
     仍为第一名则不调整数量（不做再平衡、不加仓、不止损）。

与聚宽版的已知口径差（实现即事实，写进策略文档）：
  - 决策时点：研究线在聚宽 10:40 用分钟现价决策；本地入口按运行时刻取实时报价，
    部署在 10:40 即为同一口径，回退最新收盘时会在报告里计数。
  - 复权：研究线用聚宽 fq='pre'；本地用 data_provider 新浪日线 + 份额折算前复权
    （`adjust_series` 只处理 >25% 的折算/拆分跳变），不处理现金分红。
  - 成交：本地是名义影子成交台账（无手续费、无滑点纪录），买侧下单量按
    万二佣金/最低 5 元做资金可行性预检，保证报告里的指令在真实账户可执行。

判定核（momentum_score / build_rows / plan_rotation）零 I/O、无状态：
每天从日线与现价重算，跨日只依赖台账流水（持仓与现金的唯一事实来源）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

from src.etf.momentum import (DIVE_RATIO, SCORE_DAYS, EtfOrder,
                              momentum_score)
from src.mx.executor import floor_lot
from src.sim_accounts import SimAccount
from src.trading_calendar import latest_trading_day_on_or_before

# ── 研究线参数（research/studies/etf_rotation/unified_coverage.py；
#    趋势分参数 SCORE_DAYS/DIVE_RATIO 居 src/etf/momentum.py 单点）──

SCORE_LOW = 0.0          # 合格下限（严格大于）
SCORE_HIGH = 5.0         # 合格上限（严格小于）
STALE_DAYS = 10          # 交易日历不可用时：最后日线距今 > N 自然日 → 剔除

# ── 模拟账户与执行口径 ──

ACCOUNT = "etf_rotation"                       # 台账归因标签（独立策略账户）
LEDGER_PATH = "data/trade_ledger_etf_rotation.jsonl"   # 独立流水，与主台账互不可见
SIM_ACCOUNT = SimAccount(                      # 收盘成绩消费方经 src/sim_accounts 枚举
    ledger_path=LEDGER_PATH, label="ETF跨资产动量轮动",
    benchmark={"name": "沪深300价格指数", "index": "000300"})
COMMISSION = 0.0002      # 资金可行性预检用：佣金万二（台账本身不记费用）
MIN_COMMISSION = 5.0     # 资金可行性预检用：最低佣金 5 元
SCORE_HINT = "10:40"     # 研究线的决策时点；部署 cron 应对齐（报告里展示）

# 15 只池成员：(代码, 名称, 资产类别)。名称用短名，报告可读；代码为 6 位裸码。
POOL: Tuple[Tuple[str, str, str], ...] = (
    ("510300", "沪深300", "国内权益"),
    ("159915", "创业板", "国内权益"),
    ("515180", "红利", "国内权益"),
    ("512100", "中证1000", "国内权益"),
    ("588000", "科创50", "国内权益"),
    ("513500", "标普500", "海外权益"),
    ("513100", "纳指100", "海外权益"),
    ("159920", "恒生", "海外权益"),
    ("513520", "日经225", "海外权益"),
    ("513030", "德国DAX", "海外权益"),
    ("518880", "黄金", "商品"),
    ("159980", "有色期货", "商品"),
    ("159985", "豆粕期货", "商品"),
    ("511090", "30年国债", "利率"),
    ("511010", "5年国债", "利率"),
)

NAME_BY_CODE: Dict[str, str] = {code: name for code, name, _ in POOL}
GROUP_BY_CODE: Dict[str, str] = {code: group for code, _, group in POOL}


# ── 数据对象 ──

@dataclass
class RotationRow:
    """单只 ETF 的当日截面事实；判定核只读此表。"""
    code: str
    name: str
    group: str
    close: float          # 最近一根已完成日线的前复权收盘
    price: float          # 当日现价（实时报价；缺失回退最新收盘并计数）
    score: float          # 动量评分；数据不足/非法为 nan，跳水为 0
    eligible: bool = False
    rank: int = 0         # 合格候选内排名（1 = 最强）；不合格为 0


# 旧名重导出：根入口 etf_rotation.py 与台账测试仍按 RotationOrder 引用；
# 引擎订单类为 momentum.EtfOrder，本模块内直接用新名，改名随入口侧下次改动。
RotationOrder = EtfOrder


# ── 判定核（零 I/O；momentum_score/SCORE_DAYS/DIVE_RATIO 引自趋势分引擎）──


def is_eligible(score: float) -> bool:
    """合格候选：有限分且 0 < score < 5（严格两端）。"""
    return math.isfinite(score) and SCORE_LOW < score < SCORE_HIGH


def affordable_shares(cash: float, price: float) -> int:
    """资金可行性预检：在扣除佣金（万二、最低 5 元）后能买的最大整手股数。

    台账本身不记费用，这里只保证报告里的买入指令在真实账户不会因费用被拒。
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
    """单只日线 → (25 根已完成收盘, 当日 bar 收盘 fallback, 跳过原因)。

    决策日当天的 bar 不进历史窗口（研究线用 25 根此前日线 + 当日现价）；
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

    # 只有 25 个历史点可进入打分；交易日历可用时还要求最后一根就是上一交易日。
    # 先按"上一交易日"截断，再检查短缺，避免把更早的历史补成 25 根。
    if prev_trading_day is not None:
        history = history[history.index <= pd.Timestamp(prev_trading_day)]
    if len(history) < SCORE_DAYS:
        return None, intraday_close, "short"
    tail = [float(v) for v in history.values[-SCORE_DAYS:]]
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
               pool: Sequence[Tuple[str, str, str]] = POOL
               ) -> Tuple[List[RotationRow], dict]:
    """当日截面：打分 + 合格判定 + 排名（合格候选按 score 降序，1 = 最强）。

    prices[code] = 当日现价（实时报价）；缺失回退决策日 bar 收盘，再退最近
    已完成收盘，并计入 diag["realtime_missing"]。
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
        score = momentum_score(tail, price)
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


def top_candidate(rows: Sequence[RotationRow]) -> Optional[RotationRow]:
    """合格候选第一名；没有合格候选返回 None（→ 空仓）。"""
    for row in rows:
        if row.eligible:
            return row
    return None


def plan_rotation(rows: Sequence[RotationRow], positions: List[dict],
                  prices: Dict[str, float], avail_cash: float, *,
                  account: str = ACCOUNT) -> dict:
    """单只全仓的调仓计划：卖出旧持仓 → 用全部可用资金买入目标 → 空仓。

    规则（unified_coverage.execute_trade）：
      - 无合格候选：清仓；
      - 现持仓 = 第一名：不动；
      - 否则：先卖出全部现持仓，再以卖出回款 + 原有现金买入第一名。
    卖出价用现价，取不到时回退持仓快照的 current_price；买侧股数按
    费用预检后的整手（affordable_shares）。
    """
    target = top_candidate(rows)
    held = [p for p in positions
            if p.get("account") == account and int(p.get("count") or 0) > 0]
    notes: List[str] = []
    orders: List[EtfOrder] = []
    if len(held) > 1:
        notes.append(f"⚠️ 台账中存在 {len(held)} 只持仓，本策略应为单只，"
                     f"按规则先全部卖出再买入第一名")
    holding = held[0] if held else None

    def _price_of(pos: dict) -> float:
        price = float(prices.get(pos.get("code")) or 0.0)
        if not (math.isfinite(price) and price > 0):
            price = float(pos.get("current_price") or 0.0)
        return price if math.isfinite(price) and price > 0 else 0.0

    # 先确定"卖出谁"：目标是现持仓 → 只处理台账异常多出来的持仓；否则清空全部。
    sells: List[EtfOrder] = []
    unsellable: List[str] = []
    if target is not None and holding is not None and holding["code"] == target.code:
        to_sell = [p for p in held if p is not holding]
        notes.append(f"继续持有 {target.name}({target.code})：仍为合格候选第一名")
        if to_sell:
            notes.append(f"⚠️ 台账另有 {len(to_sell)} 只非目标持仓（应为单只），本次卖出")
        sell_reason = f"超出单只规则的持仓，卖出"
    else:
        to_sell = list(held)
        if target is None and holding is None:
            notes.append("无合格候选，保持空仓")
        sell_reason = (f"排名退出，换入 {target.name}({target.code})"
                       if target is not None else "无合格候选，空仓")
    for pos in to_sell:
        price = _price_of(pos)
        qty = floor_lot(int(pos.get("count") or 0))
        if price <= 0:
            notes.append(f"{pos.get('name', '')}({pos.get('code', '')}) "
                         f"缺现价，无法卖出（仍持有）")
            unsellable.append(str(pos.get("code", "")))
            continue
        if qty <= 0:
            continue
        sells.append(EtfOrder(
            code=pos["code"], name=pos.get("name", "") or pos["code"],
            action="sell", shares=qty, price=price, amount=qty * price,
            reason=sell_reason))

    buys: List[EtfOrder] = []
    if target is not None and not (
            holding is not None and holding["code"] == target.code):
        if unsellable:
            # 旧持仓无法卖出时不得再买入，否则会同时持有两只（违反单只全仓）
            notes.append("存在无法估价的持仓，本次不买入（避免与旧持仓重叠）")
        else:
            buy_cash = float(avail_cash) + sum(o.amount for o in sells)
            shares = affordable_shares(buy_cash, target.price)
            if shares <= 0:
                notes.append(f"{target.name}({target.code}) 可用资金不足一手，未买入")
            else:
                flow = (f"先卖后买（卖出 {len(sells)} 笔）" if sells else "空仓建仓")
                buys.append(EtfOrder(
                    code=target.code, name=target.name, action="buy", shares=shares,
                    price=target.price, amount=shares * target.price,
                    reason=(f"合格候选第{target.rank}名（score={target.score:+.4f}）；"
                            f"{flow}")))
    orders = sells + buys
    return {"target": target, "holding": holding, "orders": orders,
            "sells": sells, "buys": buys, "notes": notes}


# ── 编排（取数 + 决策组装，不执行交易）──

def fetch_bars(codes: Sequence[str]) -> Dict[str, pd.DataFrame]:
    """拉 ETF 日线全历史并逐只做份额折算前复权（data_provider 单点取数）。

    与行业动量轮动使用同一价格口径：新浪日线原始价 + `adjust_series`
    （>25% 折算/拆分跳变前复权）；不处理现金分红（策略文档记为已知口径差）。
    """
    from data_provider.bars import get_etf_daily

    frames: Dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            df = get_etf_daily(code, adjust="qfq")
        except Exception:
            import logging
            logging.getLogger(__name__).warning("ETF 日线获取失败 %s", code,
                                                exc_info=True)
            df = None
        if df is None or df.empty:
            continue
        frame = df[["date", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date"]).sort_values("date")
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        frames[code] = frame.set_index("date")
    return frames


def fetch_prices(codes: Sequence[str]) -> Dict[str, float]:
    """池成员当日现价（实时报价逐只拉取；单只失败静默跳过，由调用方回退）。"""
    from data_provider import get_fetcher

    fetcher = get_fetcher()
    prices: Dict[str, float] = {}
    for code in codes:
        try:
            quote = fetcher.get_realtime_quote(code)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("实时报价获取失败 %s", code,
                                                exc_info=True)
            continue
        if quote is not None and quote.has_basic_data():
            value = float(quote.price)
            if math.isfinite(value) and value > 0:
                prices[code] = value
    return prices


def rotation_snapshot(decision_date: Optional[date] = None
                      ) -> Tuple[List[RotationRow], Dict[str, float], dict]:
    """当日截面 + 现价（取数在编排层，判定核零 I/O）。"""
    decision_date = decision_date or date.today()
    previous = latest_trading_day_on_or_before(decision_date - timedelta(days=1),
                                               fallback=None)
    codes = [code for code, _, _ in POOL]
    bars = fetch_bars(codes)
    prices = fetch_prices([code for code in codes if code in bars])
    rows, diag = build_rows(bars, prices, decision_date=decision_date,
                            prev_trading_day=previous)
    return rows, prices, diag
