# -*- coding: utf-8 -*-
"""趋势分引擎（src/etf/momentum.py）直测：公式、失败语义、跳水、订单类。

引擎是两个动量策略（跨资产轮动 / 行业动量卫星）的打分单点；规范语义 =
数据不足/价格非法返回 nan、跳水返回 0.0、不四舍五入。消费方各自的
口径差（行业侧 nan→0.0 + round 4）在消费方自己的测试里钉，不在此处。
"""
import math

import numpy as np
import pytest

from src.etf.momentum import DIVE_RATIO, SCORE_DAYS, EtfOrder, momentum_score


def _geo(n: int = 25, step: float = 1.001, base: float = 10.0) -> list:
    """严格几何级数收盘（对数价格共线 → 加权 R²=1）。"""
    return [base * step ** k for k in range(n)]


# 159530，截至 2025-05-06 的 25 根历史收盘加当日收盘。
# 回归斜率为正，旧 R² 却为负；取倒数后旧实现反而给下降趋势正分。
SCORE_SIGN_CASE = [
    1.355, 1.352, 1.339, 1.321, 1.313, 1.323, 1.290, 1.161, 1.102,
    1.133, 1.172, 1.198, 1.212, 1.207, 1.185, 1.180, 1.177, 1.219,
    1.208, 1.263, 1.246, 1.246, 1.221, 1.239, 1.284, 1.318,
]


class TestFormula:
    @pytest.mark.parametrize("step", [1.002, 1.001, 0.999])
    def test_geometric_series_r2_one_matches_exp_formula(self, step):
        """等比价格对数线性 → R²=1，分数精确等于 exp(slope×250)−1。"""
        closes = _geo(25, step)
        last = closes[-1] * step                    # 第 26 点继续等比
        assert momentum_score(closes, last) == pytest.approx(step ** 250 - 1.0,
                                                             rel=1e-9)

    def test_negative_score_preserved(self):
        """负分=下降趋势：清零只给跳水，阴跌保留负分供排序。"""
        closes = [10.0 * 0.999 ** k for k in range(25)]
        s = momentum_score(closes, closes[-1] * 0.999)
        assert s < 0.0

    def test_flat_series_scores_effectively_zero(self):
        """横盘：斜率≈0 → 分数为浮点尘埃（<1e-12；行业侧 round 后记 0）。"""
        assert abs(momentum_score([10.0] * 25, 10.0)) < 1e-12

    @pytest.mark.parametrize("inverse", [False, True])
    def test_weighted_r2_cannot_reverse_trend_sign(self, inverse):
        """真实收盘边界例：引擎原始分取整后 = ±0.0001（行业侧 round 口径锚）。"""
        prices = [1 / p for p in SCORE_SIGN_CASE] if inverse else SCORE_SIGN_CASE
        s = momentum_score(prices[:-1], prices[-1])
        assert round(s, 4) == (-0.0001 if inverse else 0.0001)

    def test_random_walk_score_finite(self):
        rng = np.random.default_rng(20261018)
        walk = list(10.0 * np.cumprod(1.0 + rng.normal(0.0012, 0.009, size=26)))
        assert math.isfinite(momentum_score(walk[:-1], walk[-1]))

    def test_no_rounding_at_fourth_decimal(self):
        """不四舍五入：小正值保持原样（消费方 0/边界严格比较不因取整翻转）。"""
        closes = _geo(25, 1.0000004)                # 年化 ≈ 0.0001 量级
        s = momentum_score(closes, closes[-1] * 1.0000004)
        assert s != round(s, 4)


class TestFailureSemantics:
    def test_insufficient_bars_returns_nan(self):
        assert math.isnan(momentum_score(_geo(24), 10.0))       # 24+1 < 26 点
        assert math.isnan(momentum_score([], 10.0))
        # 边界：25 根历史 + 当日现价 = 26 点，够数
        assert math.isfinite(momentum_score(_geo(25), 10.0 * 1.001 ** 25))

    def test_nan_and_nonpositive_prices_return_nan(self):
        closes = _geo(25)
        for tail in (closes[:-1] + [float("nan")],   # 历史含 nan
                     closes[:-1] + [0.0],            # 历史含 0
                     closes[:-1] + [-1.0]):          # 历史含负价
            assert math.isnan(momentum_score(tail, closes[-1]))
        for last in (float("nan"), 0.0, -3.0,
                     float("inf"), float("-inf")):
            assert math.isnan(momentum_score(closes, last))


class TestDive:
    def test_any_of_recent_3_days_dive_clears_to_zero(self):
        """近 3 个日环比（今日、昨日、前日的环比）任一 <0.95 → score=0。"""
        step = 1.001

        # ① 今日跳水 -10%
        closes = _geo(25)
        assert momentum_score(closes, closes[-1] * 0.90) == 0.0

        # ② 昨日环比 -6%：随后今日恢复等比
        closes = _geo(25)
        closes[-1] = closes[-2] * 0.94
        assert momentum_score(closes, closes[-1] * step) == 0.0

        # ③ 前日环比 -6%：后两日继续等比（避免再触发其它日）
        closes = _geo(25)
        closes[-2] = closes[-3] * 0.94
        closes[-1] = closes[-2] * step
        assert momentum_score(closes, closes[-1] * step) == 0.0

    def test_ratio_exactly_above_threshold_not_cleared(self):
        """恰好 0.96（不 <0.95）不清零：分数有限且非 0。"""
        step = 1.001
        closes = _geo(25)
        closes[-2] = closes[-3] * 0.96
        s = momentum_score(closes, closes[-1] * step)
        assert math.isfinite(s) and s != 0.0


class TestSharedConstants:
    def test_score_days_and_dive_ratio_match_research_lines(self):
        """两策略与研究线同值：25 根 + 26 点；跳水阈值 0.95。"""
        assert SCORE_DAYS == 25
        assert DIVE_RATIO == 0.95


class TestEtfOrder:
    def test_qty_property_returns_shares(self):
        order = EtfOrder(code="510300", name="沪深300", action="buy",
                         shares=9900, price=10.0, amount=99000.0, reason="test")
        assert order.qty == 9900
