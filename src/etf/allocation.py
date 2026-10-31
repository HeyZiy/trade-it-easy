# -*- coding: utf-8 -*-
"""周度批次——ETF 核心仓统一调仓闭环（规格唯一来源 strategy/etf_allocation.md）。

单动词 interface：run() -> str，完成账户快照 → 再平衡建议 → 周报。
ETF账户台账由用户确认成交记账，报告生成不写入成交。

数据 seam（测试注入假数据的唯一入口，家法同 quality_pool/feeds.py）：
每个外部取数一个 *_raw 薄包装（函数内懒导入），测试经 monkeypatch 替换本模块的
*_raw 函数注入假数据，不 patch 更深的数据源模块：
  _raw_market_pe / _raw_treasury_yield_y10 / _raw_erp_percentile  市场估值三件套
  _raw_valuation_rows    标的级估值行（跟踪指数 PE/股息率）
  _raw_snapshot          ETF账户台账读侧（持仓+价格快照）
  _raw_trend_bars_ad     唐奇安观察日线（AmazingData 累积复权，主源，批量）
  _raw_trend_bars        唐奇安观察日线（东财 hfq，备源）
  _raw_daily_close       补价日线收盘
  _raw_realtime_quotes   缺价实时兜底（批量）
建议只读账户状态，不写成交。plan.should 用于报告分级，展示单笔偏离达到阈值的建议。
  估值口径分两层不互相兜底；任何估值不进自动决策——核心仓目标恒等于中性基准。
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.etf.config import is_dividend_style, AssetType, pe_level

logger = logging.getLogger(__name__)


def _baseline():
    from src.etf_account import ACCOUNT_PATH, baseline, load
    return baseline(load(ACCOUNT_PATH))

# ── 数据 seam：每个外部取数一个薄包装（懒导入），测试只替换这里 ──

def _raw_market_pe():
    """全市场 PE 及近 5 年分位（src.etf.amazing_factors）。"""
    from src.etf.amazing_factors import get_market_pe
    return get_market_pe()


def _raw_treasury_yield_y10():
    """10 年期国债收益率 %（src.etf.amazing_factors）。"""
    from src.etf.amazing_factors import get_treasury_yield_y10
    return get_treasury_yield_y10()


def _raw_erp_percentile():
    """风险溢价自身近 5 年分位（src.etf.amazing_factors）。"""
    from src.etf.amazing_factors import get_erp_percentile
    return get_erp_percentile()


def _raw_valuation_rows(equity_etfs):
    """标的级估值行：跟踪指数 PE/股息率（src.etf.amazing_factors）。"""
    from src.etf.amazing_factors import etf_valuation_rows
    return etf_valuation_rows(equity_etfs)


def _raw_snapshot():
    """读取核心配置策略的持仓、现金与估值快照。"""
    from src.etf_account import ACCOUNT_PATH, strategy_snapshot
    return strategy_snapshot(ACCOUNT_PATH)


def _raw_trend_bars_ad(codes, end):
    """唐奇安观察日线，AmazingData 事件乘数累积复权（主源，批量取数）。"""
    from data_provider.fetchers.amazingdata_info import fetch_etf_hfq_bars
    return fetch_etf_hfq_bars(codes, end)


def _raw_trend_bars(code, end):
    """唐奇安观察日线，供应商后复权（data_provider.bars.get_etf_daily_em，备源）。"""
    from data_provider.bars import get_etf_daily_em
    return get_etf_daily_em(code, end)


def _raw_daily_close(code):
    """补价日线收盘（data_provider.bars.get_etf_daily）。"""
    from data_provider.bars import get_etf_daily
    return get_etf_daily(code)


def _raw_realtime_quotes(codes):
    """批量实时报价 → ({code: 现价}, {code: 简称}）（data_provider.manager）。"""
    from data_provider.manager import realtime_quotes
    return realtime_quotes(codes)


# ── 周度取数上下文（一次性装配，各节纯渲染）──

@dataclass(frozen=True)
class WeekContext:
    """一次装配的取数快照；渲染节只读此上下文，不再各自发起同主题取数。

    market_pe/y10/erp_pct 单点取齐（市场宏观节估值三件套）；
    alloc 为账户+决策快照（None = 台账不可读，策略参考节渲染跳过文案，不再重算链路）。
    """
    market_pe: Optional[dict]    # get_market_pe()：{pe, pe_pct, ...} 或 None
    y10: Optional[float]         # 10 年期国债收益率（%）
    erp_pct: Optional[float]     # 风险溢价自身近 5 年分位
    alloc: Optional[dict]        # _compute_allocation 快照，见其返回值（None = 台账不可读）
    trends: dict = field(default_factory=dict)  # 标的自身唐奇安20/10观察快照
    valuations: list = field(default_factory=list)  # 持仓表与估值明细共用


def _build_week_context() -> WeekContext:
    """一次性装配全部共享取数（估值/国债/ERP/账户快照）。
    """
    market_pe = _raw_market_pe()
    y10 = _raw_treasury_yield_y10()
    try:
        erp = _raw_erp_percentile()
    except Exception:
        erp = None
    erp_pct = erp.get("erp_pct") if erp else None
    equity_etfs = [a for a in _baseline() if a.asset_type == AssetType.EQUITY]
    return WeekContext(market_pe=market_pe, y10=y10, erp_pct=erp_pct,
                       alloc=_compute_allocation(), trends=_fetch_trend_observations(),
                       valuations=_raw_valuation_rows(equity_etfs))


def _fetch_trend_observations() -> dict:
    """逐只观察自身ETF：AmazingData 复权日线为主源，东财 hfq 为备源。

    两源复权基有细微差异（历史约 0.3–2.4% 的信号日不同），同一只在一次报告内
    只用单一来源、不拼接；两源都不可用才降级为"行情不可用"，不补旧价。
    """
    from src.etf.trend import donchian_observation

    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    today = now.strftime("%Y-%m-%d")
    codes = [asset.code for asset in _baseline() if asset.code != "CASH"]
    try:
        ad_bars = _raw_trend_bars_ad(codes, today)
    except Exception as exc:
        logger.warning("唐奇安观察 AmazingData 不可用，改用东财: %s", exc)
        ad_bars = {}
    observations = {}
    for code in codes:
        observation = None
        if code in ad_bars:
            try:
                observation = dict(donchian_observation(ad_bars[code], now=now),
                                   source="AmazingData")
            except Exception as exc:
                logger.warning("唐奇安观察 %s AmazingData 行情不可用: %s", code, exc)
        if observation is None:
            try:
                bars = _raw_trend_bars(code, today)
                observation = dict(donchian_observation(bars, now=now), source="东财")
            except Exception as exc:
                logger.warning("唐奇安观察 %s 不可用: %s", code, exc)
                observation = {"state": None, "error": "行情不可用"}
        observations[code] = observation
    counts = Counter(item.get("source", "不可用") for item in observations.values())
    logger.info("唐奇安观察来源：%s", "，".join(f"{name} {n} 只" for name, n in counts.items()))
    return observations


def _valuation_cell(row: dict) -> str:
    """现有估值事实＋锚来源前缀，不从绝对PE编造历史分位或买卖判断。
    row 为空＝基准外无估值行（如黄金）；有行但锚与数值全缺＝无。"""
    if not row:
        return "—"
    if not row.get("source_name") and row.get("pe") is None and row.get("div_yield") is None:
        return "无"
    parts = []
    if row.get("source_name"):
        parts.append(row["source_name"])
    if row.get("pe") is not None:
        parts.append(f"PE {row['pe']:.1f}")
    if row.get("div_yield") is not None:
        parts.append(f"股息 {row['div_yield']:.1f}%")
    spread = row.get("div_yield_spread")
    if is_dividend_style(row.get("code")) and spread is not None:
        note = "（偏低）" if spread >= 1.5 else ""
        parts.append(f"利差 {spread:+.1f}pt{note}")
    return " / ".join(parts)


# ── 市场宏观 ──

def _market_overview(ctx: WeekContext) -> str:
    """市场宏观（未完善）：全市场 PE / 收益率 / 风险溢价，纯数据不给操作建议。"""
    lines = ["## 市场宏观（未完善）", ""]

    market_pe = ctx.market_pe
    if market_pe:
        pe, pe_pct = market_pe["pe"], market_pe["pe_pct"]
        level = pe_level(pe_pct)
        lines.append(f"- **全市场 PE**：{pe:.1f} 倍 | 近 5 年 **{pe_pct:.0f}%** 分位 →  **{level}**")
        lines.append(f"- **股票内在收益率（1/PE）**：{100/pe:.2f}%")

        y10 = ctx.y10
        if y10:
            spread = 100/pe - y10
            # ERP 自身分位与 PE 分位同窗口可比：PE 分位受窗口锚定影响（含深熊底部则读高），
            # ERP 分位是跨资产口径的独立参照，两者并列展示供互相印证
            erp_note = ""
            if ctx.erp_pct is not None:
                erp_note = f"，自身近 5 年 **{ctx.erp_pct:.0f}%** 分位"
            spread_note = "承担风险有额外回报" if spread > 0 else "股票还不如国债"
            lines.append(
                f"- **风险溢价**：{100/pe:.2f}% − 国债 {y10:.2f}% = **{spread:+.2f}%**{erp_note} "
                f"（{spread_note}）"
            )
    else:
        lines.append("- **全市场 PE**：数据不可用")
        if ctx.y10:
            lines.append(f"- **10 年期国债收益率**：{ctx.y10:.2f}%")

    lines.append("")
    return "\n".join(lines)


# ── 持仓对照与调仓建议 ──

def _fetch_missing_prices(baseline: List, positions: List[dict]) -> dict:
    """为未持仓/缺价的基准 ETF 补价：日线收盘为主口径（盘后分析数据稳定），
    实时报价兜底（失败代码收拢一批，行情源仅在真缺价时初始化）；
    失败不补（compare 对无价标的自然跳过）。"""
    held_price = {p.get("code", ""): (p.get("current_price", 0) or 0) for p in positions}
    needed = [a.code for a in baseline if a.code != "CASH" and held_price.get(a.code, 0) <= 0]
    if not needed:
        return {}

    logger.info(f"获取 {len(needed)} 只 ETF 的行情价格...")
    prices = {}
    missed = []
    for code in needed:
        try:
            df = _raw_daily_close(code)
            if df is not None and not df.empty:
                price = float(df.iloc[-1]["close"])
                if price > 0:
                    prices[code] = price
                    continue
        except Exception as e:
            logger.debug(f"日线收盘获取 {code} 价格失败: {e}")
        missed.append(code)
    if missed:
        prices.update(_raw_realtime_quotes(missed)[0])
    return prices


def _compute_allocation():
    """计算目标配比与再平衡计划，供持仓对照和建议展示。

    持仓与资金只读取ETF账户台账；未初始化时停止生成持仓建议。
    目标 = 中性基准，无动态择时；**不读环境快照**（核心仓纯机械）。
    行情取数在此编排层一次取齐（prices），决策核 rebalancer 保持零 I/O。

    Returns:
        dict（台账恒可推导；读不了时返回 None → 持仓对照节渲染跳过文案）
    """
    try:
        snap = _raw_snapshot()
    except Exception as e:
        logger.warning(f"成交台账读取失败，跳过持仓对照: {e}", exc_info=True)
        return None

    from src.etf.rebalancer import ETFRebalancer

    positions = snap.positions
    total_assets = snap.total_assets
    rebalancer = ETFRebalancer(_baseline())
    target = rebalancer.calculate_target()

    core_positions, _, _ = rebalancer.split_rotation_positions(positions)
    # 台账缺价的持仓 + 未持仓基准 ETF 一并补价（compare 对无价标的自然跳过）
    prices = _fetch_missing_prices(rebalancer.baseline, positions)
    plan = rebalancer.build_plan(target, positions, total_assets, prices=prices)

    return {
        "cash": snap.cash,
        "positions": positions,
        "total_assets": total_assets,
        "core_positions": core_positions,
        "target": target,
        "plan": plan,
        "prices": prices,
    }


def _holding_overview(ctx: WeekContext) -> str:
    """策略参考表：目标/实际/偏离/建议指令，唐奇安与估值两列独立参考（纯渲染 ctx.alloc）。"""
    lines = ["## 策略参考", ""]

    alloc = ctx.alloc
    if alloc is None:
        lines.append("- 成交台账不可用，跳过持仓对照")
        return "\n".join(lines)

    positions = alloc["positions"]
    total_assets = alloc["total_assets"]
    target = alloc["target"]
    plan = alloc["plan"]
    orders = plan.orders

    # 当前持仓占比（资金占比即总占比；卫星等基准外持仓不产生偏离，由现金桶吸收）
    current_map: dict = {}
    for p in positions:
        mv = float(p.get("market_value", 0) or 0)
        if total_assets > 0:
            current_map[p.get("code", "")] = mv / total_assets * 100

    # 现金实际占比 = 剩余资金（台账持仓只含证券、不含现金项）
    held_sum = sum(v for k, v in current_map.items() if k != "CASH")
    current_map["CASH"] = max(0.0, 100.0 - held_sum)

    lines.append("| 资产 | 目标% | 实际% | 偏离 | 再平衡建议 | 唐奇安20/10 | 估值 |")
    lines.append("|------|-------|-------|------|------------|-------------|------|")

    order_map = {o.code: o for o in orders}
    valuation_map = {r["code"]: r for r in ctx.valuations}
    for asset in _baseline():
        code = asset.code
        tgt = target.get(code, 0.0) * 100
        cur = current_map.get(code, 0.0)
        dev = tgt - cur
        order = order_map.get(code)
        action = ""
        if order:
            side = "🟢买" if order.action == "buy" else "🔴卖"
            action = f"{side} {order.quantity}股"
        trend = ctx.trends.get(code, {})
        state = trend.get("state")
        if state is True:
            trend_text = "√"
        elif state is False:
            trend_text = "×"
        else:
            trend_text = "—"
        if code == "513180":
            trend_text = f"**{trend_text} ★**"
        valuation = _valuation_cell(valuation_map.get(code, {}))
        lines.append(f"| {asset.name}（{code}） | {tgt:.1f}% | {cur:.1f}% | {dev:+.1f}% | {action} | {trend_text} | {valuation} |")

    lines.append("")
    lines.append("> 唐奇安：√＝规则持有，×＝规则空仓，—＝数据不足或状态未确定；"
                 "收盘突破此前20日最高价入场、跌破此前10日最低价离场，通道内延续历史状态。")
    lines.append("> ★ 恒生科技：当前样本中唐奇安20/10改善了持有体验，为重点观察规则。")

    # 基准外持仓提示（未纳入对照表）
    baseline_codes = {a.code for a in _baseline()}
    extra = [f"{p.get('name', '')}({p.get('code', '')})" for p in alloc["core_positions"]
             if p.get("code") not in baseline_codes]
    if extra:
        lines.append("")
        lines.append(f"> 基准外持仓：{'、'.join(extra)}（未纳入对照）")

    # 残腿：应卖但持仓不足一手，交易所只收整手——不出必拒卖单，列手动待办
    for o in plan.odd_lots:
        lines.append(f"- ⚠️ {o['name']}({o['code']}) 高于目标应减，但持仓 {o['count']} 股不足一手，"
                     f"无法整手卖出，需手动处理")

    lines.append("")
    return "\n".join(lines)


# ── 建议说明 ──

def _recommendation_section(alloc: dict) -> str:
    orders = [o for o in alloc["plan"].orders if o.action in ("sell", "buy")]
    if not orders:
        return "## 本次建议\n\n无调仓需求，保持当前持仓。"
    lines = ["## 本次建议", ""]
    lines.extend(f"- {o.action.upper()} {o.name}({o.code}) 约 {o.qty}份 ≈ {o.amount:,.0f}元（{o.reason}）"
                 for o in orders)
    return "\n".join(lines)


# ── 报告生成（单动词 interface）──

def run() -> str:
    """生成周报与配置建议，不写入成交记录。

    取数一次装配进 WeekContext，各节纯渲染。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    ctx = _build_week_context()
    alloc = ctx.alloc

    sections = [
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}",
        "",
    ]

    if alloc is not None:
        sections.append(_recommendation_section(alloc))
    else:
        sections.append("## 本次建议\n\n持仓数据不可用，暂不生成建议。")
    sections.extend([_holding_overview(ctx), _market_overview(ctx)])

    sections.extend([
        "---",
        "",
        "*免责声明：本报告仅供观察参考，不构成投资建议。*",
    ])

    return "\n\n".join(sections)
