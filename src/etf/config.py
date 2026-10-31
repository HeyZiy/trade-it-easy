# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 中性基准
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

from src.account_sources import AccountSource
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


def asset_settings(code):
    from src.portfolio_config import load
    return next((row for row in load()["core"] if row["code"] == code), {})


def is_dividend_style(code):
    return asset_settings(code).get("dividend_style", False)


# 减仓优先级：按 volatility_rank 从高到低（创业板先减，国债/现金后减）。
# 核心仓 = 纯机械，与市场状态完全无关——防护腿（gold/bond）同规则减仓，不因市场状态豁免
# （组合层面 trending_down 的前瞻收益≈抛硬币：后 20 日胜率 49.9%、均值 +0.34%，
# 000300 代理全样本；近十年 +0.68%，按状态清仓在近十年为负贡献 −1.47% vs 死拿 +1.69%）。
# 任何以市场状态为条件的仓位动作都不成立。

# 再平衡阈值——两层口径：
#   触发层（should_rebalance）：单类偏离 > 5% 或总偏离 > 15% → 只作**报告分级**；
#   执行层（compare → orders）：单笔按 MIN_TRADE_DEVIATION 过滤碎股偏差；
#   **执行门 = 有指令即执行**（消费方读 orders 非空）——触发层当前不拦执行。
# 未决事项：是否把执行门改为触发层的 should。改了会让调仓显著变稀
# （单类 2%~5% 的批次不再执行），属策略语义变更，需先定再动。
REBALANCE_SINGLE_THRESHOLD = 0.05     # 单类偏离触发阈值（报告分级）
REBALANCE_TOTAL_THRESHOLD = 0.15      # 所有偏离绝对值之和 > 15% 强制触发（报告分级）
MIN_TRADE_DEVIATION = 0.02            # 碎股偏差过滤：< 2% 不出指令


# ── 全市场 PE 分位 → 估值档位（唯一 producer：src/etf/allocation 市场概览/持仓对照两处展示共用）。
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


# ── 账户数据源描述符（收盘成绩等消费方经 src/account_sources 枚举）──

ACCOUNT_SOURCE = AccountSource(
    ledger_path=f"data/{FILENAME}",
    label="ETF账户",
    benchmark={"name": "ETF期初权重买入持有（现金零收益，不再平衡）",
               "account": "etf"},
)
