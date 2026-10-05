# -*- coding: utf-8 -*-
"""
===================================
ETF 再平衡引擎
==================================

职责（纯计算核，零 I/O）：
1. 中性基准 → 目标配比（无偏移，不做动态择时，见 strategy/etf_allocation.md）
2. 比较实际持仓 vs 目标 → 生成调仓指令（旧钱唯一动作）；行情价格经
   compare(prices=...) 参数注入，取价由入口编排层（etf_observe）完成
3. 触发判定（should_rebalance）与订单一起装配为 RebalancePlan 纯值产物，
   执行由 etf_observe 统一批次完成（本模块只出指令）

**与市场状态完全无关**（不读环境参数，防护腿同规则减仓）。
依据：组合层面 trending_down 无前瞻信息（后 20 日胜率
49.9% / 均值 +0.34%，近十年 +0.68%，000300 代理），核心仓只抄基准不抄择时。

整手口径单点在 src/mx/executor：买侧 round_lot（向下取整后强凑≥100）、
卖侧 floor_lot(min(应卖, 持仓))（不足一手跳过该笔，残腿进 odd_lots 供报告列手动待办）。
新钱投放参考（全市场估值分位）由 etf_observe 报告生成，仅建议不自动执行。
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.etf.config import (
    NEUTRAL_BASELINE,
    get_rotation_universe_codes,
    MIN_TRADE_DEVIATION, REBALANCE_SINGLE_THRESHOLD,
    REBALANCE_TOTAL_THRESHOLD,
)
from src.mx.executor import floor_lot, round_lot

logger = logging.getLogger(__name__)


@dataclass
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
        """执行层统一股数口径（与 RotationOrder.qty 同名，混合订单批次无需 getattr 探测）。"""
        return self.quantity


@dataclass(frozen=True)
class RebalancePlan:
    """核心仓再平衡纯值产物（可独立校验，不含 client/rebalancer 等活对象）。

    - `orders`：本次批次指令（执行门＝有指令即执行）。
    - `should`：**触发层**结论（单类 >5% 或 总 >15%），只作报告分级，不拦执行。
    - `reason`：**执行层**结论，与实际动作逐字一致（不取触发层文案）。
    - `odd_lots`：应卖但持仓不足一手的残腿 [{code, name, count}]，供报告列手动待办。
    """
    orders: List[RebalanceOrder]
    total_deviation: float
    should: bool
    reason: str
    odd_lots: List[dict]


class ETFRebalancer:
    """ETF 再平衡引擎（纯计算：构造零依赖，行情经参数注入）"""

    def __init__(self):
        self.baseline = NEUTRAL_BASELINE

    # ── 计算目标配比 ──

    def calculate_target(self) -> Dict[str, float]:
        """目标配比 = 中性基准权重（无偏移；估值观点在基准里，环境不进目标）

        Returns:
            {code: target_weight} 目标权重（0.0 ~ 1.0）
        """
        return {a.code: a.neutral_weight for a in self.baseline}

    # ── 比较持仓 ──

    def _build_current_map(self, positions: List[dict], capital: float) -> Dict[str, dict]:
        """将持仓列表转为 {code: {market_value, current_pct}}（占比按给定资金口径）"""
        result = {}
        for p in positions:
            code = p.get("code", "")
            mv = float(p.get("market_value", 0) or 0)
            pct = mv / capital if capital > 0 else 0.0
            result[code] = {
                "market_value": mv,
                "current_pct": pct,
                "count": p.get("count", 0),
                "current_price": p.get("current_price", 0),
                "name": p.get("name", ""),
            }
        return result

    def split_rotation_positions(self, positions: List[dict]) -> Tuple[List[dict], float, List[dict]]:
        """拆分核心持仓与卫星（非核心）持仓。

        卫星持仓独立预算，不参与核心仓偏离计算；
        核心资金 = 总资产 − 卫星持仓市值。

        Returns:
            (core_positions, rotation_mv, rotation_positions)
        """
        rotation_codes = get_rotation_universe_codes()
        core_positions, rotation_positions = [], []
        rotation_mv = 0.0
        for p in positions:
            if p.get("code", "") in rotation_codes:
                rotation_mv += float(p.get("market_value", 0) or 0)
                rotation_positions.append(p)
            else:
                core_positions.append(p)
        return core_positions, rotation_mv, rotation_positions

    def compare(self, target: Dict[str, float], positions: List[dict],
                total_assets: float,
                prices: Optional[Dict[str, float]] = None,
                ) -> Tuple[List[RebalanceOrder], float, List[dict]]:
        """比较目标 vs 实际，生成调仓指令（旧钱唯一动作：阈值再平衡）

        资金口径：核心资金 = 总资产（资金占比即总占比）。卫星仓与其他账户持仓
        不在基准代码集内，不产生偏离；其资金被"现金（以及其他账户）"桶吸收。

        prices 为未持仓/缺价标的的行情补价（调用方取齐后注入，本模块不取数）。
        本方法只撒网：单笔按 MIN_TRADE_DEVIATION 过滤碎股偏差；触发阈值
        （单类 5% / 总 15%）在 should_rebalance 决定整批是否执行。

        **与市场状态完全无关**：组合层面 trending_down 无前瞻信息，
        核心仓只抄基准、不抄择时（strategy/etf_allocation.md 第二节）。

        Returns:
            (orders, total_deviation, odd_lots)
            调仓指令列表 + 总偏离度 + 应卖但持仓不足一手的残腿清单
        """
        current = self._build_current_map(positions, total_assets)
        prices = prices or {}
        orders: List[RebalanceOrder] = []
        odd_lots: List[dict] = []
        total_deviation = 0.0

        for asset in self.baseline:
            code = asset.code
            if code == "CASH":
                continue
            target_pct = target.get(code, 0.0)
            cur = current.get(code) or {}
            cur_pct = cur.get("current_pct", 0.0)
            deviation = target_pct - cur_pct
            total_deviation += abs(deviation)

            if abs(deviation) < MIN_TRADE_DEVIATION:
                continue

            amount = deviation * total_assets
            cur_price = cur.get("current_price", 0) or 0
            if cur_price <= 0:
                cur_price = prices.get(code, 0) or 0

            if deviation > 0:
                # 需要加仓（买侧口径：向下取整后强凑≥100，单点在 executor.round_lot）
                quantity = round_lot(abs(amount) / cur_price) if cur_price > 0 else 0
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="buy",
                        amount=abs(amount), quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"低于目标{deviation*100:.1f}%"
                    ))
            else:
                # 需要减仓（卖侧口径：钳制到实际持仓后向下取整，单点在 executor.floor_lot；
                # 不足一手的残腿不出卖单）
                cur_count = int(cur.get("count", 0) or 0)
                need = int(abs(amount) / cur_price) if cur_price > 0 else 0
                quantity = floor_lot(min(need, cur_count))
                if quantity >= 100:
                    orders.append(RebalanceOrder(
                        code=code, name=asset.name, action="sell",
                        amount=abs(amount), quantity=quantity,
                        current_pct=cur_pct, target_pct=target_pct,
                        reason=f"高于目标{abs(deviation)*100:.1f}%"
                    ))
                elif 0 < cur_count < 100:
                    odd_lots.append({"code": code, "name": asset.name, "count": cur_count})

        # 按 volatility_rank 排序（卖出优先高波动，买入优先低波动）
        vol_map = {a.code: a.volatility_rank for a in self.baseline}
        sells = sorted([o for o in orders if o.action == "sell"], key=lambda o: vol_map.get(o.code, 99), reverse=True)
        buys = sorted([o for o in orders if o.action == "buy"], key=lambda o: vol_map.get(o.code, 99))
        orders = sells + buys

        return orders, total_deviation, odd_lots

    # ── 判断是否触发再平衡（触发层；决策口径见 build_plan 与 config 阈值注释）──

    def should_rebalance(self, orders: List[RebalanceOrder],
                         total_deviation: float) -> Tuple[bool, str]:
        """触发层结论：**只看偏离量级**，与"能否出单"分开。

        - 总偏离 > 15% → True；最大单类偏离 > 5% → True；否则 False。
        - 无指令（< 2% 碎股噪声，或标的缺价）不改变量级判断：总偏离够大仍报 True，
          理由里写明"无单可出"，避免"总偏离 71% 却 should=False"这类自相矛盾。
        - 本结论只作报告分级，不拦执行（执行门＝有指令即执行，见 build_plan）。
        文案一律带数。
        """
        threshold = REBALANCE_SINGLE_THRESHOLD
        max_single = max((abs(o.target_pct - o.current_pct) for o in orders), default=0.0)

        if total_deviation > REBALANCE_TOTAL_THRESHOLD:
            return True, (f"总偏离 {total_deviation:.1%} > {REBALANCE_TOTAL_THRESHOLD:.0%}"
                          f"（最大单类 {max_single:.1%}）")
        if max_single > threshold:
            return True, f"最大单类偏离 {max_single:.1%} > {threshold:.0%}"
        if not orders:
            return False, (f"无单可出（无单笔偏离 ≥{MIN_TRADE_DEVIATION:.0%} 或标的缺价；"
                           f"总偏离 {total_deviation:.1%}）")
        return False, (f"最大单类 {max_single:.1%} ≤ {threshold:.0%} "
                       f"且总偏离 {total_deviation:.1%} ≤ {REBALANCE_TOTAL_THRESHOLD:.0%}")

    def _execution_reason(self, orders: List[RebalanceOrder], total_deviation: float,
                          should: bool, trigger_reason: str) -> str:
        """报告结论 = **执行层事实**，与实际动作逐字一致；触发层结论作为分级信息附后。

        执行门口径单点在 build_plan：有指令即执行（单笔 ≥ MIN_TRADE_DEVIATION 才出指令）。
        """
        if not orders:
            base = (f"无指令（无单笔偏离 ≥{MIN_TRADE_DEVIATION:.0%}，或标的缺价；"
                    f"总偏离 {total_deviation:.1%}）")
            return base if not should else f"{base}；触发层：{trigger_reason}"
        return (f"{len(orders)} 笔指令（执行门＝有指令即执行，单笔 ≥{MIN_TRADE_DEVIATION:.0%} 才出指令）；"
                f"触发层：{trigger_reason}")

    def build_plan(self, target: Dict[str, float], positions: List[dict],
                   total_assets: float,
                   prices: Optional[Dict[str, float]] = None) -> RebalancePlan:
        """compare + 触发层 + 执行层结论 一次装配为纯值 RebalancePlan。

        口径：
        - **执行门 = 有指令即执行**（消费方 etf_observe 读 `orders` 非空）；
        - `should`/触发层结论只作报告分级，不拦执行；
        - `reason` 描述**执行层事实**，与实际动作逐字一致。
        待决（未决）：是否把执行门改为 `should`（即 5%/15% 真的拦执行）——
        那会让调仓显著变稀（单类 2%~5% 的批次不再执行），属策略语义变更，见 config 阈值注释。
        """
        orders, total_deviation, odd_lots = self.compare(
            target, positions, total_assets, prices)
        should, trigger_reason = self.should_rebalance(orders, total_deviation)
        reason = self._execution_reason(orders, total_deviation, should, trigger_reason)
        return RebalancePlan(orders=orders, total_deviation=total_deviation,
                             should=should, reason=reason, odd_lots=odd_lots)
