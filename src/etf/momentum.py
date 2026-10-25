# -*- coding: utf-8 -*-
"""
===================================
ETF 趋势分引擎 — 打分与订单的单点实现
===================================

CONTEXT.md「趋势分」：25 根已完成日线收盘 + 当日现价共 26 点，对对数价格做
加权线性回归（权重 1→2 线性升），分 = 年化涨幅 × 加权 R²；近 3 个日环比
任一 < 0.95（跳水）清零。公式与两条研究线一致（etf_rotation 的
unified_coverage.calculate_momentum_score、行业动量的 lM_v3_3_2/4），
消费方只调不抄。

消费方（src/etf/rotation.py 跨资产轮动、src/etf/industry_momentum.py
行业动量卫星）：
- 规范语义：数据不足/价格非法返回 nan（= 不入候选），跳水返回 0.0；
  不四舍五入（与 0/合格边界的严格比较和排名不因取整改变结论）。
- 历史口径差（行业侧继承研究线的 nan→0.0、round(…,4)）由消费方在缝上
  适配，不进引擎——改口径改适配层，公式只有一份。
- SCORE_DAYS / DIVE_RATIO 两个打分参数居此处单点；STALE_DAYS 等窗口
  常量语义随各消费方自己的窗口逻辑，留在消费方。

EtfOrder 为两个动量策略共用的调仓订单（入口映射为台账 BatchOrder）；
rebalancer 的 RebalanceOrder（阈值再平衡）不在此列。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

# ── 趋势分参数（两策略共用，与两条研究线同值）──

SCORE_DAYS = 25          # 打分回看天数：25 根已完成日线 + 当日现价 = 26 点
DIVE_RATIO = 0.95        # 近 3 个日环比任一 < 0.95 → score=0


@dataclass
class EtfOrder:
    """ETF 策略调仓指令（入口映射为台账 BatchOrder）。"""
    code: str
    name: str
    action: str           # buy / sell
    shares: int
    price: float
    amount: float
    reason: str

    @property
    def qty(self) -> int:
        """执行层统一股数口径（与 RebalanceOrder.qty/BatchOrder 对齐）。"""
        return self.shares


def momentum_score(close_tail: Sequence[float], last_price: float) -> float:
    """25 根已完成日线收盘 + 当日现价 → 加权回归趋势分（CONTEXT.md「趋势分」）。

    对数价格对 w=linspace(1,2) 加权线性回归 → 年化 = exp(斜率×250)-1 →
    score = 年化 × 加权 R²（R² 的残差平方和、总离差与均值都用 w² 加权，
    R² 限于 [0,1] 仅防浮点误差，不会反转趋势符号；负分保留供排序）。
    近 3 个日环比任一 < DIVE_RATIO 时返回 0.0；数据不足或价格非法返回 nan
    （nan 表示"不入候选"，0 只表示跳水，两者在下游判定等价但保留区分）。
    """
    try:
        prices = np.append(np.asarray(close_tail[-SCORE_DAYS:], dtype=float),
                           float(last_price))
        if len(prices) < SCORE_DAYS + 1 or np.any(prices <= 0) \
                or not np.isfinite(prices).all():
            return math.nan
        logp = np.log(prices)
        x = np.arange(len(logp), dtype=float)
        w = np.linspace(1.0, 2.0, len(logp))
        slope, intercept = np.polyfit(x, logp, 1, w=w)
        ann = math.exp(slope * 250.0) - 1.0
        effective_w = w ** 2
        mean_logp = float(np.average(logp, weights=effective_w))
        ss_res = float(np.sum(effective_w * (logp - (slope * x + intercept)) ** 2))
        ss_tot = float(np.sum(effective_w * (logp - mean_logp) ** 2))
        r2 = float(np.clip(1.0 - ss_res / ss_tot, 0.0, 1.0)) if ss_tot > 0 else 0.0
        score = ann * r2
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < DIVE_RATIO:
            return 0.0
        return float(score)
    except Exception:
        return math.nan
