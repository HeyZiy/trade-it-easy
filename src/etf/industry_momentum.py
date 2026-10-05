# -*- coding: utf-8 -*-
"""
===================================
行业动量轮动 — 卫星仓引擎（日频截面，v3_1 口径）
===================================

主账户卫星仓的战术策略：日频行业横截面动量，交易的是行业相对强弱的延续。
核心逻辑一句话——持有当下最强的行业，不再是强者就换：入场、持有、退出全部
使用同一个截面问题（"它还是不是最强之一"），没有突破条件、没有趋势过滤、
没有绝对收益止损（大盘回调时最强行业也会收益转负，但"仍是最强"就该继续持有）。

规则（strategy/industry_momentum.md，研究线定稿口径 v3_1 =
research/studies/industry_momentum/lM_v3_1.py，2024 窗 +82.67% / 超额夏普 +0.379）：
  1. 标的名单 = 动态规则池，每 20 交易日重建：全市场 ETF 表 → 513 前缀 +
     名称跨境词硬剔除 → 宽基/规模/风格/债券/货币/商品词表剔除（含单字'债'、
     '上海金'）→ 上市 ≥365 自然日 → 近 20 日均额 ≥5000 万 → 250 日收益相关
     ≥0.90 贪心去重（留流动性最高）。固定 34 池已废弃（纳入/遗漏两层后视）。
  2. 打分 = 近 25 根日收盘 + 当日现价共 26 点的对数价格加权线性回归
     （w=linspace(1,2)，后期权重大）→ score = 年化 × 加权 R²；近 3 个日环比
     任一 <0.95（跳水）则 score 清零。不用裸涨幅（ret20 把噪声当趋势）。
  3. 买 = score>0（负分=下降趋势不接）且复合拥挤度 <90（缺数据放行），评分
     降序补足空槽等权（每只 = min(可用现金/空槽数, 卫星总值/3)）；当期卖出
     标的当期不占槽；卖 = 评分排名跌出前 40%（不设绝对收益止损）。卖出优先。
  4. 复合拥挤度 = 两分量均值：成交额占池内比重 250 日分位 + 自身收盘价
     250 日分位（PE 分位分量已删：数据侧后视风险 + 与价格分位语义重复）。
  5. 无市场门控——截面退出 + 准入过滤即引擎的全部防御。
  6. 执行：本引擎只出指令；尾盘入口 industry_momentum.py（14:45~14:55）经
     trade_ledger.execute_batch 记名义台账（account=satellite，信号当日现价
     成交形态）。

设计：
- 判定核（momentum_score / filter_by_name / build_pool / adjust_series /
  build_rows / build_sell_orders / build_buy_orders）零 I/O、无状态——
  场景测试直接注入，不碰网络与台账；
- 取数收在编排层（fetch_universe / fetch_bars / fetch_prices /
  rotation_snapshot）：全市场名单与带 amount 日线走 data_provider 新浪单源，
  当日现价走实时报价（缺数据回退最新收盘并在 diag 计数）；
- 池快照持久化 data/industry_momentum_pool.json（rebuilt_on + 成员）：
  距上次重建 ≥20 个交易日（交易日历；不可用时回退自然日 ≥28 天）则重建，
  重建日取数较重（全候选全历史），日常只取池成员；
- 价格口径：日线为未复权原始价，份额折算/拆分由 adjust_series 前复权
  （单日 |ret|>25% 视为折算，A 股 ETF 涨跌停 ±10/20% 不可能到达），
  amount 为元口径不调整——与研究线本地复刻同一处理。
"""

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from src.mx.executor import round_lot
from src.trading_calendar import get_trading_dates

logger = logging.getLogger(__name__)

# ── 规则常量（与回测定稿口径 research/studies/industry_momentum/lM_v3_1.py 一一对应）──

SATELLITE_BUDGET_RATIO = 0.10   # 卫星仓总仓位上限（总资产占比）
TOPN = 3                        # 等权持有只数
EXIT_RANK_PCT = 0.40            # 退出线：评分排名跌出前 40%
CROWD_PCT_MAX = 90.0            # 复合拥挤度准入上限（≥90 禁买，缺数据放行）
CROWD_LOOKBACK = 250            # 拥挤度分量的历史分位窗口（日）
CROWD_MIN_OBS = 60              # 分量最少观测数（不足则该分量缺失）
MIN_BARS = 250                  # 截面/入池门槛（与相关性去重窗口同宽）
STALE_DAYS = 10                 # 最后日线距今超过 N 个自然日 → 停牌/退市，剔除截面
SCORE_DAYS = 25                 # 打分取 25 根收盘 + 当日现价 = 26 点
DIVE_RATIO = 0.95               # 近 3 个日环比 <0.95 → 跳水，score 清零
LIQ_LOOKBACK = 20               # 流动性门槛窗口（日均额）
LIQ_AMT20_MIN = 50_000_000.0    # 近 20 日均成交额 ≥5000 万
MATURE_DAYS = 365               # 上市 ≥365 自然日（首根日线计）
CORR_LOOKBACK = 250             # 相关性去重窗口（日收益）
CORR_DEDUPE = 0.90              # 相关 ≥0.90 视为同一底层暴露
REBUILD_EVERY = 20              # 池重建周期（交易日）
REBUILD_FALLBACK_DAYS = 28      # 交易日历不可用时的自然日回退阈值
SPLIT_JUMP = 0.25               # 单日 |ret|>25% 视为份额折算/拆分（涨跌停 ±10/20%）

CROSS_BORDER_PREFIX = "513"     # 沪市跨境 ETF 代码段：代码级硬剔除，不依赖命名

# 名称剔除词：宽基/规模/风格/债券/货币/商品/跨境——行业与主题保留。
# 末尾 '债'（单字，盖国开债/地债，行业名无'债'字不误杀）与 '上海金'
# （不能用单字'金'，误杀稀有金属/稀土）= v3_1 堵商品/债漏网的单变量改动。
EXCLUDE_KW = ('沪深300', '中证500', '中证1000', '中证800', '中证全指', '中证2000',
              '中证A500', 'A500', 'A100', '上证50', '上证180', '上证380', '上证580',
              '上证指数', '科创50',
              '科创100', '科创综', '创业板50', '创业板综', '创业板指', '创业板',
              '双创', '北证', '深证100', '基本面50', '红利', '股息', '国债', '政金',
              '信用', '债券', '转债', '货币', '现金', '黄金', '白银', '原油', '豆粕',
              '商品', '纳斯达克', '纳指', '标普', '道琼', '日经', '德国', '法国',
              '亚太', '东南亚', '恒生', '香港', 'H股', '港股', '央企', '国企',
              '龙头', 'ESG', '养老', 'FOF', '联动', '增强', '价值', '成长',
              '质量', '低波', '动量', '多因子', '自由现金流',
              'HK', '225', '东证', '中韩', '美国', '恒指', '油气',
              '债', '上海金')

POOL_STATE_PATH = Path(__file__).parent.parent.parent / "data" / "industry_momentum_pool.json"


@dataclass
class RotationOrder:
    """卫星仓调仓订单。"""
    code: str
    name: str
    action: str      # buy / sell
    shares: int
    price: float
    amount: float
    reason: str

    @property
    def qty(self) -> int:
        """执行层统一股数口径（与 RebalanceOrder.qty 同名，混合订单批次无需 getattr 探测）。"""
        return self.shares


@dataclass
class RotationRow:
    """截面单只 ETF 的当日事实：动量评分 + 拥挤度，判定核只读此表。"""
    code: str
    name: str
    close: float                        # 最新前复权收盘（实时价缺失时=price）
    price: float                        # 当日现价（实时报价，成交价口径）
    score: float                        # 动量评分 = 年化 × 加权 R²（跳水/失败=0）
    rank: int = 0                       # 1 = 最强
    n: int = 0                          # 截面成员数（退出线 = ceil(EXIT_RANK_PCT * n)）
    crowd: Optional[float] = None       # 复合拥挤度（缺数据放行）

    @property
    def buy_allowed(self) -> bool:
        """准入：拥挤度 <90（缺数据放行）；score>0 门槛在买入侧单独判。"""
        return self.crowd is None or self.crowd < CROWD_PCT_MAX

    @property
    def exit_rank(self) -> int:
        """退出线：排名 > 此值 → 卖出。"""
        return math.ceil(EXIT_RANK_PCT * self.n)


def load_l2_map() -> List[dict]:
    """旧固定 34 池名单（data/l2_etf_map.json）——选池已废弃，仅存量持仓归属用。

    动态规则池上线前的历史持仓可能来自此名单，核心仓资金口径的卫星标的
    判定（src/etf/config.py）仍需它做兼容；新持仓一律来自动态池快照。
    """
    path = Path(__file__).parent.parent.parent / "data" / "l2_etf_map.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── 判定核（零 I/O）──

def momentum_score(close_tail: Sequence[float], last_price: float) -> float:
    """25 根日收盘 + 当日现价共 26 点的对数价格加权回归动量（lM_v3_1 镜像）。

    对数价格对 w=linspace(1,2) 加权线性回归 → 年化 = exp(slope×250)−1 →
    score = 年化 × 加权 R²（R² 惩罚歪斜路径，只放行"走得直"的趋势）；
    近 3 个日环比任一 <0.95（跳水）清零；数据不足/非正价格返回 0。
    负分保留（排序供卖出用，买入侧 score>0 门槛）。
    """
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(last_price))
        if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0) \
                or not np.isfinite(prices).all():
            return 0.0
        logp = np.log(prices)
        x = np.arange(len(logp), dtype=float)
        w = np.linspace(1.0, 2.0, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250.0) - 1.0
        ss_res = float(np.sum(w * (logp - (slope * x + intercept)) ** 2))
        ss_tot = float(np.sum(w * (logp - float(np.mean(logp))) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        score = ann * r2
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < DIVE_RATIO:
            score = 0.0
        return round(float(score), 4)
    except Exception:
        return 0.0


def adjust_series(close: pd.Series) -> pd.Series:
    """份额折算/拆分前复权（研究线 adjust_splits 单列版）。

    单日 |ret| > 25%（A 股 ETF 涨跌停 ±10/20%，不可能到达）视为折算/合并：
    factor = cur/prev，此前价格全乘 factor（连续化），多次折算按时间顺序
    累积即前复权到样本末。amount 为元口径不调整，与本函数无关。
    """
    s = close.dropna().astype(float)
    if len(s) < 2:
        return close
    out = s.copy()
    r = s.pct_change()
    for d in r[r.abs() > SPLIT_JUMP].index:
        i = s.index.get_loc(d)
        factor = float(s.at[d]) / float(s.iloc[i - 1])
        out.loc[out.index < d] *= factor
    return out


def filter_by_name(etfs: List[dict]) -> List[dict]:
    """池第 1-2 关（纯函数）：513 前缀硬剔除 + 名称剔除词表。

    全部用当时数据，无后视。词表命中判定用列表推导——不用裸 any(生成器)，
    numpy 覆盖下 np.any 对生成器恒真（v2 空池事故真因）。
    """
    out: List[dict] = []
    for e in etfs:
        code = str(e.get("code", "")).zfill(6)
        name = str(e.get("name", ""))
        if not code or code.startswith(CROSS_BORDER_PREFIX):
            continue
        hits = [k for k in EXCLUDE_KW if k in name]
        if hits:
            continue
        out.append({"code": code, "name": name})
    return out


def build_pool(cands: List[dict], bars: Dict[str, pd.DataFrame],
               as_of: Optional[str] = None) -> Tuple[List[dict], dict]:
    """池第 3-6 关（纯函数）：成熟 → 流动性 → 250 日相关贪心去重。

    bars[code] = DataFrame(index=date_str, columns=[close(已前复权), amount(元)])。
    门槛：≥MIN_BARS 根日线、首根日线距 as_of ≥MATURE_DAYS 自然日、
    近 LIQ_LOOKBACK 日均额 ≥LIQ_AMT20_MIN；相关性 NaN 视为不可比 → 保留。
    停牌语义：按自身最近 LIQ_LOOKBACK 个有成交日均额计（平台口径为日历日
    含停牌零填充，此处略宽，长期停牌由截面 STALE_DAYS 兜底）。

    Returns:
        (members 按流动性降序 [{code,name}], stats 各环节计数)
    """
    ref = pd.Timestamp(as_of) if as_of else pd.Timestamp.now().normalize()
    mature: List[dict] = []
    for e in cands:
        df = bars.get(e["code"])
        if df is None or df.empty or "close" not in df.columns:
            continue
        close = df["close"].dropna()
        if len(close) < MIN_BARS:
            continue
        if (ref - pd.Timestamp(close.index[0])).days < MATURE_DAYS:
            continue
        mature.append(e)

    liq: Dict[str, float] = {}
    for e in mature:
        amt = bars[e["code"]]["amount"].dropna().astype(float)
        amt20 = float(amt.tail(LIQ_LOOKBACK).mean()) if len(amt) > 0 else 0.0
        if amt20 == amt20 and amt20 >= LIQ_AMT20_MIN:
            liq[e["code"]] = amt20

    kept: List[str] = []
    rets: Dict[str, pd.Series] = {}
    for e in sorted(mature, key=lambda x: -liq.get(x["code"], 0.0)):
        code = e["code"]
        if code not in liq:
            continue
        r = bars[code]["close"].pct_change().tail(CORR_LOOKBACK)
        dup = False
        for k in kept:
            corr = r.corr(rets[k])
            if corr == corr and corr >= CORR_DEDUPE:
                dup = True
                break
        if not dup:
            kept.append(code)
            rets[code] = r

    name_of = {e["code"]: e.get("name", e["code"]) for e in mature}
    members = [{"code": c, "name": name_of[c]} for c in kept]
    stats = {"cands": len(cands), "mature": len(mature),
             "liquid": len(liq), "members": len(kept)}
    return members, stats


def _pct_in_window(vals: Sequence[float], lookback: int) -> Optional[float]:
    """当前值在自身近 N 日窗口内的分位（0-100），不足 CROWD_MIN_OBS 观测返回 None。"""
    w = list(vals[-lookback:])
    if len(w) < CROWD_MIN_OBS:
        return None
    last = w[-1]
    return round(sum(1 for v in w if v < last) / float(len(w)) * 100, 1)


def build_rows(pool: List[dict], bars: Dict[str, pd.DataFrame],
               prices: Dict[str, float], as_of: Optional[str] = None
               ) -> Tuple[List[RotationRow], dict]:
    """构建当日截面：动量评分从强到弱排名 + 两分量复合拥挤度。

    prices[code] = 当日现价（实时报价；缺失回退最新收盘并计入 diag）。
    停牌/退市（最后日线距今 > STALE_DAYS）与不足 MIN_BARS 的池成员不入截面。
    拥挤度成交额占比分量的分母 = 池内有数据成员的当日成交额之和（缺数据
    成员贡献 0，与平台停牌零填充同效）；两分量齐才产出（≥2），否则 None 放行。

    Returns:
        (rows 按 score 降序（并列按 code 稳定）, diag 含各环节剔除计数)
    """
    ref = pd.Timestamp(as_of) if as_of else pd.Timestamp.now().normalize()
    skipped = {"no_bars": 0, "short": 0, "stale": 0, "bad_close": 0}
    realtime_missing = 0
    meta: List[tuple] = []
    amounts: Dict[str, pd.Series] = {}
    for m in pool:
        code = m["code"]
        df = bars.get(code)
        if df is None or df.empty or "close" not in df.columns:
            skipped["no_bars"] += 1
            continue
        sub = df if as_of is None else df[df.index <= as_of]
        if len(sub) < MIN_BARS:
            skipped["short"] += 1
            continue
        if (ref - pd.Timestamp(sub.index[-1])).days > STALE_DAYS:
            skipped["stale"] += 1
            continue
        close = float(sub["close"].iloc[-1])
        if close != close or close <= 0:
            skipped["bad_close"] += 1
            continue
        price = float(prices.get(code) or 0.0)
        if price != price or price <= 0:
            price = close
            realtime_missing += 1
        close_tail = sub["close"].astype(float).values
        amounts[code] = (sub["amount"].astype(float)
                         if "amount" in sub.columns else pd.Series(dtype=float))
        meta.append((code, m.get("name", code), close, price, close_tail))

    rowsum = None
    if amounts:
        rowsum = pd.DataFrame(amounts).fillna(0.0).sum(axis=1)

    rows: List[RotationRow] = []
    for code, name, close, price, close_tail in meta:
        comps: List[float] = []
        amt = amounts[code]
        if not amt.empty and rowsum is not None:
            share = [v / s for v, s in zip(amt.values,
                                           rowsum.reindex(amt.index).values)
                     if s and s > 0 and v == v and v > 0]
            sp = _pct_in_window(share, CROWD_LOOKBACK)
            if sp is not None:
                comps.append(sp)
        cp = _pct_in_window([v for v in close_tail if v == v], CROWD_LOOKBACK)
        if cp is not None:
            comps.append(cp)
        crowd = round(sum(comps) / len(comps), 1) if len(comps) >= 2 else None
        score = momentum_score(close_tail, price)
        rows.append(RotationRow(code=code, name=name, close=close, price=price,
                                score=score, crowd=crowd))

    rows.sort(key=lambda r: (-r.score, r.code))
    for i, r in enumerate(rows, 1):
        r.rank, r.n = i, len(rows)
    diag = {"pool": len(pool), "n": len(rows),
            "realtime_missing": realtime_missing, "skipped": skipped}
    return rows, diag


def build_sell_orders(rows: List[RotationRow], positions: List[dict]
                      ) -> Tuple[List[RotationOrder], List[str]]:
    """生成卫星卖出订单：评分排名跌出前 40% → 清仓。唯一退出规则。

    不设绝对收益止损——大盘回调时最强行业也会收益转负，但"仍是最强"就该
    继续持有。截面外持仓（停牌剔除/池重建后出池）不动——没有排名就没有
    退出判定，由人工处置。卖出股数收敛单点在 trade_ledger.execute_batch，
    本函数只报全仓股数。
    """
    row_map = {r.code: r for r in rows}
    orders: List[RotationOrder] = []
    notes: List[str] = []
    for p in positions:
        code = p.get("code", "")
        r = row_map.get(code)
        if r is None or r.rank <= r.exit_rank:
            continue
        count = int(p.get("count", 0) or 0)
        if count <= 0:
            continue
        name = p.get("name", "") or r.name
        price = float(p.get("current_price", 0) or 0) or r.price
        reason = (f"评分跌出前{EXIT_RANK_PCT:.0%}"
                  f"（第{r.rank}/{r.n}名，score={r.score:+.4f}）")
        orders.append(RotationOrder(code=code, name=name, action="sell",
                                    shares=count, price=price,
                                    amount=count * price, reason=reason))
        notes.append(f"{name}({code}) {reason}")
    return orders, notes


def build_buy_orders(rows: List[RotationRow], held_codes: set, total_assets: float,
                     satellite_mv: float, avail_cash: float
                     ) -> Tuple[List[RotationOrder], List[str]]:
    """生成卫星买入订单：准入过滤后按动量评分降序补足空槽等权。

    - 准入 = score>0（负分=下降趋势/跳水清零，不买）且复合拥挤度 <90
      （缺数据放行），已持有/截面外不重复买；
    - 每只金额 = min(可用现金/空槽数, 卫星账户总值/TOPN)，卫星账户总值口径 =
      min(总资产 × 预算上限, 卫星市值 + 可用现金)；卫星市值已触及预算上限则不买；
    - held_codes 只计池内持仓（池外旧持仓不占槽，由人工处置）；
    - 成交价口径 = 当日现价（实时报价，尾盘≈收盘）。
    """
    budget_cap = total_assets * SATELLITE_BUDGET_RATIO
    if satellite_mv >= budget_cap:
        return [], []

    universe_codes = {r.code for r in rows}
    held = held_codes & universe_codes
    slots = max(0, TOPN - len(held))
    if slots == 0:
        return [], []

    candidates = [r for r in rows
                  if r.code not in held and r.score > 0 and r.buy_allowed]
    selected = candidates[:slots]
    if not selected:
        return [], []

    sleeve = min(budget_cap, satellite_mv + avail_cash)
    per = min(avail_cash / len(selected), sleeve / TOPN)
    if per <= 0:
        return [], []

    orders: List[RotationOrder] = []
    notes: List[str] = []
    for r in selected:
        shares = round_lot(per / r.price)
        if shares <= 0:
            continue
        crowd_txt = f"{r.crowd:.0f}%" if r.crowd is not None else "—"
        reason = (f"动量评分第{r.rank}/{r.n}名（score={r.score:+.4f}），"
                  f"拥挤度{crowd_txt}")
        orders.append(RotationOrder(code=r.code, name=r.name, action="buy",
                                    shares=shares, price=r.price,
                                    amount=shares * r.price, reason=reason))
        notes.append(f"{r.name}({r.code}) {reason}")
    return orders, notes


# ── 池快照（每 20 交易日重建的持久化状态）──

def load_pool_state() -> dict:
    """读取池快照（data/industry_momentum_pool.json）；缺失/损坏返回空 dict。"""
    try:
        state = json.loads(POOL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    members = state.get("members")
    if not isinstance(members, list) or not members:
        return {}
    return state


def save_pool_state(members: List[dict], rebuilt_on: str) -> None:
    """写池快照。写入失败仅告警（下次运行快照缺失会强制重建，自愈）。"""
    try:
        POOL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        POOL_STATE_PATH.write_text(
            json.dumps({"rebuilt_on": rebuilt_on, "members": members},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError:
        logger.warning(f"池快照写入失败 {POOL_STATE_PATH}", exc_info=True)


def pool_needs_rebuild(state: dict, today: Optional[date] = None) -> bool:
    """距上次重建 ≥REBUILD_EVERY 个交易日 → 重建；交易日历不可用时回退
    自然日 ≥REBUILD_FALLBACK_DAYS（宁可多重建不漏重建）。空快照必重建。"""
    if not state or not state.get("rebuilt_on"):
        return True
    today = today or date.today()
    try:
        rebuilt = date.fromisoformat(str(state["rebuilt_on"]))
    except ValueError:
        return True
    tdays = get_trading_dates(rebuilt + timedelta(days=1), today)
    if tdays:
        return len(tdays) >= REBUILD_EVERY
    return (today - rebuilt).days >= REBUILD_FALLBACK_DAYS


# ── 编排（取数 + 决策组装，不执行交易）──

def fetch_universe() -> List[dict]:
    """全市场在市 ETF 名单（data_provider 新浪单源，时点表）。"""
    from data_provider.bars import get_etf_universe

    df = get_etf_universe()
    if df is None or df.empty:
        return []
    return [{"code": str(r.code), "name": str(r.name)}
            for r in df.itertuples(index=False)]


def fetch_bars(codes: List[str]) -> Dict[str, pd.DataFrame]:
    """拉池成员日线全历史（新浪 fund_etf_hist_sina）并逐只前复权。

    Returns:
        {code: DataFrame(index=date_str 升序, columns=[close(前复权), amount(元)])}；
        单只失败跳过（缺口由 build_rows 的 no_bars 计数感知）。
    """
    from data_provider.bars import get_etf_daily_full

    bars: Dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            df = get_etf_daily_full(code)
        except Exception:
            logger.warning(f"ETF 全历史获取失败 {code}", exc_info=True)
            df = None
        if df is None or df.empty:
            continue
        out = pd.DataFrame({"close": adjust_series(df["close"]),
                            "amount": df["amount"]})
        bars[code] = out.dropna(subset=["close"])
    return bars


def fetch_prices(codes: List[str]) -> Dict[str, float]:
    """池成员当日现价（实时报价逐只拉取；单只失败静默跳过，由调用方回退）。"""
    from data_provider import get_fetcher

    fm = get_fetcher()
    out: Dict[str, float] = {}
    for code in codes:
        try:
            q = fm.get_realtime_quote(code)
        except Exception:
            logger.warning(f"实时报价获取失败 {code}", exc_info=True)
            continue
        if q is not None and q.has_basic_data():
            out[code] = float(q.price)
    return out


def rotation_snapshot() -> Tuple[List[RotationRow], dict]:
    """当日截面（含取数与池维护）：rows + 诊断信息。

    池维护：快照缺失或距上次重建 ≥REBUILD_EVERY 个交易日 → 全市场重建
    （全表 → 名称过滤 → 成熟 → 流动 → 去重）并落盘；重建日取数较重。
    全市场名单取数失败时沿用现有快照（标 stale_state），无快照可用则抛错
    （宁可不判定也不静默空转）。
    """
    today = date.today()
    state = load_pool_state()
    diag: dict = {}
    if pool_needs_rebuild(state, today):
        etfs = fetch_universe()
        if not etfs and state:
            diag["stale_state"] = True
            logger.warning("全市场 ETF 表获取失败，沿用过期池快照")
        elif not etfs:
            raise RuntimeError("全市场 ETF 表获取失败且无池快照，无法判定")
        else:
            cands = filter_by_name(etfs)
            bars_c = fetch_bars([e["code"] for e in cands])
            members, stats = build_pool(cands, bars_c, as_of=today.isoformat())
            stats["universe"] = len(etfs)
            save_pool_state(members, today.isoformat())
            diag["rebuild"] = stats
            state = {"rebuilt_on": today.isoformat(), "members": members}
    members = [{"code": str(m.get("code", "")).zfill(6),
                "name": str(m.get("name", ""))}
               for m in state.get("members", [])]
    diag["pool"] = len(members)

    bars = fetch_bars([m["code"] for m in members])
    prices = fetch_prices([m["code"] for m in members])
    rows, rows_diag = build_rows(members, bars, prices)
    diag.update(rows_diag)
    return rows, diag


def analyze_rotation(positions: List[dict], total_assets: float,
                     avail_balance: float) -> dict:
    """卫星仓全流程：截面 → 卖出 → 买入。只出指令，执行归入口脚本。

    买入侧资金口径按"先卖后买"整批计：可用现金与卫星市值均已计入卖出回款，
    入口脚本的安全校验（卖出 ≤ 持仓、买入 ≤ 可用 + 回款、买入 ≤ 预算）兜底。
    """
    rows, diag = rotation_snapshot()

    universe_codes = {r.code for r in rows}
    satellite_mv = sum(float(p.get("market_value", 0) or 0)
                       for p in positions if p.get("code", "") in universe_codes)
    held_codes = {p.get("code") for p in positions
                  if p.get("code", "") in universe_codes
                  and int(p.get("count", 0) or 0) > 0}

    sells, sell_notes = build_sell_orders(rows, positions)
    sell_amount = sum(o.amount for o in sells)
    buys, buy_notes = build_buy_orders(rows, held_codes, total_assets,
                                       satellite_mv - sell_amount,
                                       avail_balance + sell_amount)

    return {
        "rows": rows,
        "diag": diag,
        "sells": sells,
        "buy_orders": buys,
        "sell_notes": sell_notes,
        "buy_notes": buy_notes,
        "satellite_mv": satellite_mv,
        "held_codes": held_codes,
        "total_assets": total_assets,
        "avail_balance": avail_balance,
    }
