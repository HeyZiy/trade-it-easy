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
# 核心仓位：长期持有，来源=有知有行基准（适配规则见 strategy/etf_allocation.md 第二节），
# 半年人工对齐一次。权重为总资产占比；"现金"桶吸收卫星仓与其他账户的资金。
# 卫星仓（行业动量轮动，标的集见 get_rotation_universe_codes：动态规则池快照
# data/industry_momentum_pool.json ∪ 旧名单兼容存量持仓）独立于核心基准，
# 其持仓被"现金（以及其他账户）"桶吸收，不产生核心偏离。

CORE_BASELINE: List[AssetAllocation] = [
    # ── A股宽基 ──
    AssetAllocation("563360", "A500ETF",                AssetType.EQUITY, 0.17, 8),
    AssetAllocation("159680", "中证1000增强ETF",         AssetType.EQUITY, 0.01, 6),
    AssetAllocation("515180", "红利ETF",                AssetType.EQUITY, 0.14, 10),
    # ── 海外 ──
    AssetAllocation("513100", "纳指ETF",                AssetType.EQUITY, 0.05, 9),
    AssetAllocation("513500", "标普500ETF",              AssetType.EQUITY, 0.05, 9),
    AssetAllocation("513380", "恒生科技ETF",            AssetType.EQUITY, 0.10, 5),
    # ── 行业/主题 ──
    AssetAllocation("159938", "医药ETF",                AssetType.EQUITY, 0.04, 4),
    AssetAllocation("516560", "养老ETF",                AssetType.EQUITY, 0.02, 7),
    AssetAllocation("159928", "消费ETF",                AssetType.EQUITY, 0.08, 7),
    # ── 黄金 ──
    AssetAllocation("159934", "黄金ETF",                AssetType.GOLD,   0.05, 11),
    # ── 现金（国债逆回购，自动理财，不买货基） ──
    AssetAllocation("CASH",   "现金/逆回购",              AssetType.CASH,   0.29, 13),
]

# 再平衡模块使用核心仓位
NEUTRAL_BASELINE = CORE_BASELINE

# ── 核心仓 ETF 跟踪指数（估值锚对准买入标的本身） ──
# 口径 = 中证指数官网估值（PE/股息率，见 amazing_factors.get_csindex_valuation），
# 指数代码已逐只经官网实测核对（2026-09）。海外 ETF（513100/513500/513380）无 csindex
# 数据；黄金为无现金流资产，不适用估值锚。红利类估值以股息率为主锚（股息是现金流本体）。
TRACKED_INDEX: Dict[str, str] = {
    "563360": "000510",  # A500ETF → 中证A500指数
    "159680": "000852",  # 中证1000增强ETF → 中证1000指数
    "515180": "000922",  # 红利ETF → 中证红利指数
    "159938": "000991",  # 医药ETF → 中证全指医药卫生指数
    "516560": "399812",  # 养老ETF → 中证养老产业指数
    "159928": "000932",  # 消费ETF → 中证主要消费指数
}

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


def get_rotation_universe_codes() -> set:
    """卫星仓（非核心）标的代码集（剔除核心基准代码，避免与核心仓资金口径重叠）。

    = 日频轮动池快照（data/industry_momentum_pool.json，动态规则池每 20 交易日
      重建）∪ 旧固定 34 池（data/l2_etf_map.json，选池已废弃、仅存量持仓归属）
      ∪ 旧行业清单（ETF_INDUSTRY_MAP，兼容历史持仓的归属判定）
      − 核心基准代码。
    核心仓再平衡以"核心资金 = 总资产 − 卫星持仓市值"为口径，
    卫星标的独立预算、独立进出，不参与核心偏离计算。
    """
    try:
        from src.etf.industry_momentum import load_l2_map, load_pool_state

        l2_codes = {str(m["etf_code"]).zfill(6) for m in load_l2_map()}
        pool_codes = {str(m.get("code", "")).zfill(6)
                      for m in load_pool_state().get("members", [])}
    except Exception:
        l2_codes, pool_codes = set(), set()
    try:
        from src.etf.amazing_factors import ETF_INDUSTRY_MAP
    except Exception:
        ETF_INDUSTRY_MAP = {}
    baseline_codes = {a.code for a in CORE_BASELINE}
    return (set(ETF_INDUSTRY_MAP) | l2_codes | pool_codes) - baseline_codes




# ── 全市场 PE 分位 → 估值档位（唯一 producer：etf_observe 市场概览/持仓速览两处展示共用）。
#    amazing_factors.rank_buy_priorities 的 ⭐ 文案是买入侧研究展示口径，非本分档的副本，不并入。
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
