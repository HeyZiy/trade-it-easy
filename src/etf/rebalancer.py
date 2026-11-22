# -*- coding: utf-8 -*-
"""
===================================
ETF 再平衡引擎
==================================

职责（纯计算核，零 I/O）：
1. 中性基准 → 目标配比（无偏移，不做动态择时）
2. 偏离（allocation_deviation）→ 整手指令（orders_for_deviations）；
   行情价格经 prices 参数注入，取数由消费方完成。
   生产路径唯一消费方是 src/account_advice.suggestions：按偏离生成补仓/减仓
   建议，执行由人工确认记账，仅建议不自动执行。

**与市场状态完全无关**（不读环境参数，防护腿同规则减仓）。
依据：组合层面 trending_down 无前瞻信息（后 20 日胜率
49.9% / 均值 +0.34%，近十年 +0.68%，000300 代理），核心仓只抄基准不抄择时。

整手口径单点在 src/mx/executor：买侧 round_lot（向下取整后强凑≥100）、
卖侧 floor_lot(min(应卖, 持仓))（不足一手跳过该笔，残腿进 odd_lots 供报告列手动待办）。
"""

import logging
from dataclasses import dataclass
from math import isclose
from typing import Dict, List, Optional, Tuple

from src.etf.config import (
    MIN_TRADE_AMOUNT, REBALANCE_RELATIVE_THRESHOLD, REBALANCE_SINGLE_THRESHOLD,
)
from src.mx.executor import floor_lot, round_lot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllocationDeviation:
    current_value: float | None
    target_value: float | None
    gap: float | None
    absolute: float | None
    relative: float | None

    @property
    def relative_review(self) -> bool:
        return self.relative is not None and (
            self.relative >= REBALANCE_RELATIVE_THRESHOLD
            or isclose(self.relative, REBALANCE_RELATIVE_THRESHOLD, rel_tol=1e-12)
        )

    @property
    def absolute_review(self) -> bool:
        return self.absolute is not None and self.absolute >= REBALANCE_SINGLE_THRESHOLD

    @property
    def trade_eligible(self) -> bool:
        # 出指令门＝破复核带（5pp 或 25%）且缺口够最小经济交易额；
        # 带既分级也拦执行，故 trade_eligible 蕴含 absolute_review/relative_review。
        return (self.absolute_review or self.relative_review) and \
            self.gap is not None and abs(self.gap) >= MIN_TRADE_AMOUNT


def allocation_deviation(current_value: float | None, weight: float,
                         budget: float | None) -> AllocationDeviation:
    target_value = round(budget * weight, 2) if budget is not None else None
    if current_value is None or budget is None or budget <= 0:
        return AllocationDeviation(current_value, target_value, None, None, None)

    current_value = round(float(current_value), 2)
    gap = round(current_value - target_value, 2)
    absolute = abs(gap) / budget
    relative = absolute / weight if weight > 0 else None
    return AllocationDeviation(current_value, target_value, gap, absolute, relative)


@dataclass(frozen=True)
class RebalanceOrder:
    code: str
    name: str
    action: str        # "buy" 或 "sell"
    amount: float      # 交易金额（元）
    quantity: int       # 交易股数
    current_pct: float  # 当前占比
    target_pct: float   # 目标占比
    reason: str         # 原因

    @property
    def qty(self) -> int:
        """执行层统一股数口径（与 EtfOrder.qty 同名，混合订单批次无需 getattr 探测）。"""
        return self.quantity


class ETFRebalancer:
    """ETF 再平衡引擎（纯计算：构造零依赖，行情经参数注入）"""

    def __init__(self, baseline):
        self.baseline = list(baseline)

    def calculate_target(self) -> Dict[str, float]:
        """目标配比 = 中性基准权重（无偏移；估值观点在基准里，环境不进目标）

        Returns:
            {code: target_weight} 目标权重（0.0 ~ 1.0）
        """
        return {a.code: a.neutral_weight for a in self.baseline}

    def split_rotation_positions(self, positions: List[dict]) -> Tuple[List[dict], float, List[dict]]:
        """拆分核心持仓与卫星（非核心）持仓。

        归属看台账自带的 strategy 标签（投资账户流水 carry，持仓行按
        core/quality_pool/etf_rotation 归属拆分），不用代码名单反推——名单会随池轮换失效。
        卫星持仓独立预算，不参与核心仓偏离计算；
        核心资金 = 总资产 − 卫星持仓市值。

        Returns:
            (core_positions, rotation_mv, rotation_positions)
        """
        core_positions, rotation_positions = [], []
        rotation_mv = 0.0
        for p in positions:
            if p.get("account", "core") != "core":
                rotation_mv += float(p.get("market_value", 0) or 0)
                rotation_positions.append(p)
            else:
                core_positions.append(p)
        return core_positions, rotation_mv, rotation_positions

    def compare(self, target: Dict[str, float], positions: List[dict],
                total_assets: float,
                prices: Optional[Dict[str, float]] = None,
                ) -> Tuple[List[RebalanceOrder], float, List[dict]]:
        """返回调仓指令、总偏离（报告参考）和不足一手的卖出残腿。"""
        orders, total_deviation, odd_lots, _ = self._evaluate(target, positions, total_assets, prices)
        return orders, total_deviation, odd_lots

    def _evaluate(self, target, positions, total_assets, prices):
        values = {row.get("code"): row.get("market_value") for row in positions}
        deviations = {
            asset.code: allocation_deviation(values.get(asset.code, 0), target.get(asset.code, 0.0), total_assets)
            for asset in self.baseline if asset.code != "CASH"
        }
        total_deviation = sum(row.absolute for row in deviations.values() if row.absolute is not None)
        orders, odd_lots = self.orders_for_deviations(target, positions, total_assets, deviations, prices)
        return orders, total_deviation, odd_lots, deviations

    def orders_for_deviations(self, target: Dict[str, float], positions: List[dict],
                              total_assets: float, deviations: Dict[str, AllocationDeviation],
                              prices: Optional[Dict[str, float]] = None,
                              ) -> Tuple[List[RebalanceOrder], List[dict]]:
        """用已计算的金额差生成整手指令。"""
        current = {row.get("code"): row for row in positions}
        prices = prices or {}
        orders: List[RebalanceOrder] = []
        odd_lots: List[dict] = []

        for asset in self.baseline:
            code = asset.code
            deviation = deviations.get(code)
            if code == "CASH" or deviation is None or not deviation.trade_eligible:
                continue
            target_pct = target.get(code, 0.0)
            cur = current.get(code) or {}
            cur_pct = deviation.current_value / total_assets
            amount = abs(deviation.gap)
            cur_price = cur.get("current_price", 0) or 0
            if cur_price <= 0:
                cur_price = prices.get(code, 0) or 0

            if deviation.gap < 0:
                # 需要加仓（买侧口径：向下取整后强凑≥100，单点在 executor.round_lot）
                quantity = round_lot(amount / cur_price) if cur_price > 0 else 0
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="buy",
                        amount=amount, quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"低于目标{deviation.absolute * 100:.1f}个百分点"
                    ))
            else:
                # 需要减仓（卖侧口径：钳制到实际持仓后向下取整，单点在 executor.floor_lot；
                # 不足一手的残腿不出卖单）
                cur_count = int(cur.get("count", 0) or 0)
                need = int(amount / cur_price) if cur_price > 0 else 0
                quantity = floor_lot(min(need, cur_count))
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="sell",
                        amount=amount, quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"高于目标{deviation.absolute * 100:.1f}个百分点"
                    ))
                elif 0 < cur_count < 100:
                    odd_lots.append({"code": code, "name": asset.name, "count": cur_count})

        # 按 volatility_rank 排序（卖出优先高波动，买入优先低波动）
        vol_map = {a.code: a.volatility_rank for a in self.baseline}
        sells = sorted([o for o in orders if o.action == "sell"], key=lambda o: vol_map.get(o.code, 99), reverse=True)
        buys = sorted([o for o in orders if o.action == "buy"], key=lambda o: vol_map.get(o.code, 99))
        orders = sells + buys

        return orders, odd_lots
