# -*- coding: utf-8 -*-
"""
===================================
多资产配置 — 中性基准
===================================

配置只从账户JSON读取；本模块保留资产类型、计算参数与展示辅助函数。

类别归属：
  equity    — 权益类（进攻弹性来源）
  bond      — 债券类（安全垫）
  gold      — 黄金（极端风险对冲，不减仓）
  cash      — 现金/货币（弹药 + 流动性缓冲）
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, List

from data_provider.security_names import security_name
from src.etf_account import FILENAME

# ── 类别枚举 ──

class AssetType:
    EQUITY = "equity"
    BOND = "bond"
    GOLD = "gold"
    CASH = "cash"


@dataclass
class AssetAllocation:
    code: str           # 证券代码（6 位）
    asset_type: str     # AssetType
    neutral_weight: float  # 中性基准权重（0.0 ~ 1.0）
    volatility_rank: int   # 波动率排名（1=最高波动，用于减仓优先级）
    tracked_index: str | None = None
    valuation_kind: str = "none"
    dividend_style: bool = False

    @property
    def name(self):
        return security_name(self.code)


# 当前权重与标的元数据只读取 data/etf_config.json。
# config/etf_config.example.json 仅用于显式初始化与测试，不作运行时回退。
def get_neutral_baseline():
    from src.portfolio_config import load, allocations
    return allocations(load())


def example_baseline():
    from src.portfolio_config import example, allocations
    return allocations(example())


# 减仓优先级：按 volatility_rank 从高到低（创业板先减，国债/现金后减）。
# 核心仓 = 纯机械，与市场状态完全无关——防护腿（gold/bond）同规则减仓，不因市场状态豁免
# （组合层面 trending_down 的前瞻收益≈抛硬币：后 20 日胜率 49.9%、均值 +0.34%，
# 000300 代理全样本；近十年 +0.68%，按状态清仓在近十年为负贡献 −1.47% vs 死拿 +1.69%）。
# 任何以市场状态为条件的仓位动作都不成立。

# 再平衡阈值——经典 5/25 带 + 费率最小交易额（2026-11-17 裁决，取代平 2pp 出指令门）：
#   复核带（既报告分级，也拦执行）：单类绝对偏离 ≥5pp 或相对偏离 ≥25%，先达先触发。
#   出指令：破复核带 且 缺口 ≥ MIN_TRADE_AMOUNT 且可换算整手（见 rebalancer.trade_eligible）。
#   总偏离 >15pp 强制触发（报告分级）。
REBALANCE_SINGLE_THRESHOLD = 0.05     # 单类绝对偏离复核带
REBALANCE_RELATIVE_THRESHOLD = 0.25   # 单类相对偏离复核带
REBALANCE_TOTAL_THRESHOLD = 0.15      # 所有偏离绝对值之和 >15% 强制触发（报告分级）
# 最小经济交易额（元），纯费率推导、不含收益预期：
#   ETF 佣金万 0.5、最低 0.2 元、无印花税（data/etf_config.json trade_fees.etf），
#   交易额 A≤4000 元时双边佣金触底 0.4 元，费率 0.4/A；要求双边 ≤10bp → A≥400 元。
#   A>4000 元后费率恒为 1bp。场外基金申购费 0，当前基准各基金腿复核带缺口均 >400 元，门不绑定。
#   作用：微仓（w≤2%）破带后缺口够 400 元即可补回，修复平 2pp 造成的买方永久冻结。
MIN_TRADE_AMOUNT = 400.0              # 缺口低于此值不出指令（碎额噪声过滤）


# ── 全市场 PE 分位 → 估值档位（唯一 producer：src/etf/allocation 市场宏观节展示）。
#    只吃市场级分位（amazing_factors.get_market_pe）；标的级估值无分位口径，见 TRACKED_INDEX 注。
def pe_level(pe_pct: float) -> str:
    """全市场 PE 近 5 年分位(%) → 五档估值水平标签。"""
    if pe_pct < 20:
        return "极度低估"
    if pe_pct < 40:
        return "低估"
    if pe_pct < 60:
        return "合理"
    if pe_pct < 80:
        return "偏贵"
    return "高估"
