# -*- coding: utf-8 -*-
"""
===================================
长期配置 — 中性基准
===================================

配置只从账户JSON读取；本模块保留资产类别说明、计算参数与展示辅助函数。

类别归属：
  equity    — 权益类（进攻弹性来源）
  bond      — 债券类（安全垫）
  gold      — 黄金（极端风险对冲，不减仓）
  cash      — 现金/货币（弹药 + 流动性缓冲）
"""

from dataclasses import dataclass

from data_provider.security_names import security_name

# ── 类别枚举 ──

@dataclass
class AssetAllocation:
    code: str           # 证券代码（6 位）
    asset_type: str     # equity/bond/gold/cash
    neutral_weight: float  # 中性基准权重（0.0 ~ 1.0）
    volatility_rank: int   # 波动率排名（1=最高波动，用于减仓优先级）
    tracked_index: str | None = None
    valuation_kind: str = "none"
    dividend_style: bool = False

    @property
    def name(self):
        return security_name(self.code)


# 目标配比读取 data/etf_config.json。
# config/etf_config.example.json 仅用于显式初始化与测试，不作运行时回退。
def example_baseline():
    from src.portfolio_config import example, allocations
    return allocations(example())


# 减仓优先级：按 volatility_rank 从高到低（创业板先减，国债/现金后减）。
# 核心仓 = 纯机械，与市场状态完全无关——防护腿（gold/bond）同规则减仓，不因市场状态豁免
# （组合层面 trending_down 的前瞻收益≈抛硬币：后 20 日胜率 49.9%、均值 +0.34%，
# 000300 代理全样本；近十年 +0.68%，按状态清仓在近十年为负贡献 −1.47% vs 死拿 +1.69%）。
# 任何以市场状态为条件的仓位动作都不成立。

# 再平衡阈值——经典 5/25 带 + 费率最小交易额：
#   复核带（既报告分级也拦执行）：单类绝对偏离 ≥5pp 或相对偏离 ≥25%，先达先触发。
#   出指令：破复核带 且 缺口 ≥ MIN_TRADE_AMOUNT 且可换算整手（见 rebalancer.trade_eligible）。
REBALANCE_SINGLE_THRESHOLD = 0.05     # 单类绝对偏离复核带
REBALANCE_RELATIVE_THRESHOLD = 0.25   # 单类相对偏离复核带
# 最小经济交易额（元），纯费率推导、不含收益预期：
#   ETF 佣金万 0.5、最低 0.2 元、无印花税（data/etf_config.json trade_fees.etf），
#   交易额 A≤4000 元时双边佣金触底 0.4 元，费率 0.4/A；要求双边 ≤10bp → A≥400 元。
#   A>4000 元后费率恒为 1bp。场外基金申购费 0，当前基准各基金腿复核带缺口均 >400 元，门不绑定。
#   作用：微仓（w≤2%）破带后缺口够 400 元即可补回，修复平 2pp 造成的买方永久冻结。
MIN_TRADE_AMOUNT = 400.0              # 缺口低于此值不出指令（碎额噪声过滤）
