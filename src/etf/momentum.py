# -*- coding: utf-8 -*-
"""场内基金加权趋势分和调仓建议的数据对象。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

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
        if min(prices[-1] / prices[-2], prices[-2] / prices[-3],
               prices[-3] / prices[-4]) < DIVE_RATIO:
            return 0.0
        return weighted_trend_score(prices)
    except Exception:
        return math.nan


def weighted_trend_score(prices):
    """对已经校验的正价格序列计算年化斜率 × 加权 R²。"""
    log_prices = np.log(prices)
    days = np.arange(len(log_prices))
    weights = np.linspace(1, 2, len(log_prices))
    slope, intercept = np.polyfit(days, log_prices, 1, w=weights)
    effective_weights = weights ** 2
    mean_log_price = np.average(log_prices, weights=effective_weights)
    ss_total = np.sum(effective_weights * (log_prices - mean_log_price) ** 2)
    if ss_total <= 0:
        return 0.0
    residuals = log_prices - (slope * days + intercept)
    ss_residual = np.sum(effective_weights * residuals ** 2)
    r_squared = np.clip(1 - ss_residual / ss_total, 0.0, 1.0)
    return float(math.expm1(slope * 250) * r_squared)
