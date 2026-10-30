# -*- coding: utf-8 -*-
"""
===================================
ETF 长期配置 — 中性基准
===================================

中性基准反映长期信念，半年至一年审视一次。每日 gate 状态只在基准上做战术偏移，
不改变基准本身。

类别归属：
  equity    — 权益类（进攻弹性来源）
  bond      — 债券类（安全垫）
  gold      — 黄金（极端风险对冲，不减仓）
  cash      — 现金/货币（弹药 + 流动性缓冲）
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, List

from src.sim_accounts import SimAccount
from src.trade_ledger.ledger import DEFAULT_LEDGER_PATH

# ── 类别枚举 ──

class AssetType:
    EQUITY = "equity"
    BOND = "bond"
    GOLD = "gold"
    CASH = "cash"


@dataclass
class AssetAllocation:
    code: str           # 证券代码（6 位）
    name: str           # 名称
    asset_type: str     # AssetType
    neutral_weight: float  # 中性基准权重（0.0 ~ 1.0）
    volatility_rank: int   # 波动率排名（1=最高波动，用于减仓优先级）


# ── 中性基准配置 ──
# 核心仓位：长期持有，半年人工对齐一次（当前表 2026-10 更新，
# 来源见 strategy/etf_allocation.md 第二节）。权重为总资产占比。
# 目标表里 ETF 合计 56.5%、现金子账户合计 26.8%（表内合计 83.3%），其余 16.7%
# 属卫星/其他账户；这部分统一由"现金（以及其他账户）"桶吸收，故 CASH 写 43.5%
# （= 26.8% 表内现金 + 16.7% 吸收），合计 100% ——模拟成绩的基准权重必须合计为 1。
# 行业动量使用独立模拟台账，不进入核心组合的持仓、资金和成绩。

CORE_BASELINE: List[AssetAllocation] = [
    # ── A股宽基 ──
    AssetAllocation("563360", "A500ETF",                AssetType.EQUITY, 0.085, 8),
    AssetAllocation("159680", "中证1000增强ETF",         AssetType.EQUITY, 0.01, 6),
    AssetAllocation("515180", "红利ETF",                AssetType.EQUITY, 0.07, 10),
    # ── 海外 ──
    AssetAllocation("513100", "纳指ETF",                AssetType.EQUITY, 0.04, 9),
    AssetAllocation("513500", "标普500ETF",              AssetType.EQUITY, 0.16, 9),
    AssetAllocation("513800", "南方东证ETF（TOPIX）",    AssetType.EQUITY, 0.03, 9),
    AssetAllocation("513180", "恒生科技ETF华夏",        AssetType.EQUITY, 0.05, 5),
    # ── 行业/主题 ──
    AssetAllocation("159938", "医药ETF",                AssetType.EQUITY, 0.03, 4),
    AssetAllocation("159928", "消费ETF",                AssetType.EQUITY, 0.04, 7),
    AssetAllocation("159915", "创业板ETF",              AssetType.EQUITY, 0.01, 6),
    # ── 黄金 ──
    AssetAllocation("159934", "黄金ETF",                AssetType.GOLD,   0.04, 11),
    # ── 现金（国债逆回购，自动理财，不买货基；含子账户 10.3% + 16.5% 与吸收的 16.7%） ──
    AssetAllocation("CASH",   "现金/逆回购",              AssetType.CASH,   0.435, 13),
]

# 再平衡模块使用核心仓位
NEUTRAL_BASELINE = CORE_BASELINE

# ── 核心仓 ETF 跟踪指数（估值锚对准买入标的本身） ──
# 口径 = 中证指数官网估值（PE/股息率，见 amazing_factors.get_csindex_valuation），
# 指数代码已逐只经官网实测核对（2026-09）。海外 ETF（513100/513500/513800/513180）
# 无 csindex 数据；黄金为无现金流资产，不适用估值锚。红利类估值以股息率为主锚
# （股息是现金流本体）。这是标的级估值的唯一口径：不做兜底——未配锚或官网未取到时
# 报告显示"无估值锚"，不用不可比的市场/行业分位冒充标的自身估值。
TRACKED_INDEX: Dict[str, str] = {
    "563360": "000510",  # A500ETF → 中证A500指数
    "159680": "000852",  # 中证1000增强ETF → 中证1000指数
    "515180": "000922",  # 红利ETF → 中证红利指数
    "159938": "000991",  # 医药ETF → 中证全指医药卫生指数
    "159928": "000932",  # 消费ETF → 中证主要消费指数
}

# 显式声明"取不到估值锚"的权益腿（与"忘了配锚"区分）：
# 159915 跟踪创业板指（399006），该指数由深证信息编制，中证官网无估值文件（2026-10 实测 404）。
NO_VALUATION_ANCHOR: FrozenSet[str] = frozenset({"159915"})

# 股息策略类 ETF：估值以股息率为主锚（股息是现金流本体，PE/全市场口径易反向），
# 展示用"股息率 − 10Y 国债利差"作跨资产参照（分位历史积累后可切换分位口径）。
DIVIDEND_STYLE_CODES: FrozenSet[str] = frozenset({"515180"})

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


def get_neutral_baseline() -> List[AssetAllocation]:
    return NEUTRAL_BASELINE


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


# ── 模拟账户描述符（收盘成绩等消费方经 src/sim_accounts 枚举）──

SIM_ACCOUNT = SimAccount(
    ledger_path=str(DEFAULT_LEDGER_PATH),
    label="核心配置",
    benchmark={"name": "ETF期初权重买入持有（现金零收益，不再平衡）",
               "weights": {a.code: a.neutral_weight for a in CORE_BASELINE}},
)
