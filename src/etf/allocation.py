# -*- coding: utf-8 -*-
"""周度批次——ETF 核心仓统一调仓闭环（规格唯一来源 strategy/etf_allocation.md）。

单动词 interface：run(dry_run) -> str，完成账户快照 → 再平衡建议 → 周报。
核心实际账户由人工确认成交；未初始化实际账户时保留模拟记账兼容路径。

数据 seam（测试注入假数据的唯一入口，家法同 quality_pool/feeds.py）：
每个外部取数一个 *_raw 薄包装（函数内懒导入），测试经 monkeypatch 替换本模块的
*_raw 函数注入假数据，不 patch 更深的数据源模块：
  _raw_market_pe / _raw_treasury_yield_y10 / _raw_erp_percentile  市场估值三件套
  _raw_valuation_rows    标的级估值行（跟踪指数 PE/股息率）
  _raw_snapshot          名义台账读侧（持仓+价格快照）
  _raw_trend_bars        唐奇安观察日线（东财后复权）
  _raw_daily_close       补价日线收盘
  _raw_realtime_quotes   缺价实时兜底（批量）
执行侧不设 seam：批次语义（收敛/安全校验/先卖后买/逐单记账）全权委托
trade_ledger.execute_batch，测试按现有家法 monkeypatch src.trade_ledger.execute_batch。

行为口径（自 etf_observe 入口原样搬入）：
  执行门 = "orders 非空即执行"；plan.should 为触发层结论，仅进报告
  （改门属策略语义变更，见 rebalancer.py 待决注释）。
  估值口径分两层不互相兜底；任何估值不进自动决策——核心仓目标恒等于中性基准。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.etf.config import CORE_BASELINE, DIVIDEND_STYLE_CODES, AssetType, pe_level
from src.trade_ledger.performance import render_summary

logger = logging.getLogger(__name__)


def _baseline():
    from src.real_account import ACCOUNT_PATH, baseline, load
    return baseline(load(ACCOUNT_PATH)) if ACCOUNT_PATH.exists() else CORE_BASELINE

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
    """优先读取核心实际账户，未初始化时兼容模拟台账。"""
    from src.real_account import ACCOUNT_PATH, strategy_snapshot
    if ACCOUNT_PATH.exists():
        return strategy_snapshot(ACCOUNT_PATH)
    from src.trade_ledger import snapshot_with_prices
    return snapshot_with_prices()


def _raw_trend_bars(code, end):
    """唐奇安观察日线，供应商后复权（data_provider.bars.get_etf_daily_em）。"""
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

    market_pe/y10/erp_pct 单点取齐（市场概览/持仓对照/新钱参考共用同一估值口径，
    避免同一报告里各节各自取数导致口径不一致）；
    alloc 为账户+决策快照（None = 台账不可读，持仓对照节渲染跳过文案，不再重算链路）。
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
    """逐只观察自身ETF，供应商复权OHLC；失败不替换成原始价或其他ETF。"""
    from src.etf.trend import donchian_observation

    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    observations = {}
    for asset in _baseline():
        if asset.code == "CASH":
            continue
        try:
            bars = _raw_trend_bars(asset.code, now.strftime("%Y-%m-%d"))
            observations[asset.code] = donchian_observation(bars, now=now)
        except Exception as exc:
            logger.warning("唐奇安观察 %s 不可用: %s", asset.code, exc)
            observations[asset.code] = {"state": None, "error": "行情不可用"}
    return observations


def _valuation_cell(row: dict) -> str:
    """现有估值事实，不从绝对PE编造历史分位或买卖判断。"""
    parts = []
    if row.get("pe") is not None:
        parts.append(f"PE {row['pe']:.1f}")
    if row.get("div_yield") is not None:
        parts.append(f"股息 {row['div_yield']:.1f}%")
    spread = row.get("div_yield_spread")
    if row.get("code") in DIVIDEND_STYLE_CODES and spread is not None:
        note = "（偏低）" if spread >= 1.5 else ""
        parts.append(f"利差 {spread:+.1f}pt{note}")
    return " / ".join(parts) or "—"


# ── 市场概览 ──

def _market_overview(ctx: WeekContext) -> str:
    """市场估值概览（纯渲染 ctx，不给操作建议；整体配比观点归基准）。"""
    lines = ["## 市场估值概览", ""]

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


# ── 买入优先级 ──

def _valuation_reference(ctx: WeekContext) -> str:
    """各标的估值参考（锚=跟踪指数自身 PE/股息率；跨标的不可比，仅展示）"""
    lines = ["## 各标的估值参考（锚对准跟踪指数，PE 跨标的不可比）", ""]

    rows = ctx.valuations

    lines.append("| ETF | 估值锚 | PE | 股息率 | 备注 |")
    lines.append("|-----|--------|-----|--------|------|")

    for r in rows:
        source = r.get("source_name", "") or "—"
        pe = f"{r['pe']:.1f}" if r.get("pe") is not None else "—"
        dy = f"{r['div_yield']:.1f}%" if r.get("div_yield") is not None else "—"
        note = r.get("level_text", "") or "—"
        lines.append(f"| {r['name']}（{r['code']}） | {source} | {pe} | {dy} | {note} |")

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

    持仓与资金优先读取实际账户，未初始化时从模拟流水推导。
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
    rebalancer = ETFRebalancer()
    rebalancer.baseline = _baseline()
    target = rebalancer.calculate_target()

    core_positions, rotation_mv, rotation_positions = rebalancer.split_rotation_positions(positions)
    # 台账缺价的持仓 + 未持仓基准 ETF 一并补价（compare 对无价标的自然跳过）
    prices = _fetch_missing_prices(rebalancer.baseline, positions)
    plan = rebalancer.build_plan(target, positions, total_assets, prices=prices)

    return {
        "cash": snap.cash,
        "positions": positions,
        "total_assets": total_assets,
        "core_positions": core_positions,
        "rotation_mv": rotation_mv,
        "rotation_positions": rotation_positions,
        "target": target,
        "plan": plan,
        "prices": prices,
    }


def _holding_overview(ctx: WeekContext) -> str:
    """持仓 vs 目标对照 + 调仓建议（纯渲染 ctx.alloc；只出建议，不自动执行）"""
    lines = ["## 执行前持仓对照与调仓依据", ""]

    alloc = ctx.alloc
    if alloc is None:
        lines.append("- 成交台账不可用，跳过持仓对照")
        return "\n".join(lines)

    positions = alloc["positions"]
    total_assets = alloc["total_assets"]
    rotation_mv = alloc["rotation_mv"]
    rotation_positions = alloc["rotation_positions"]
    target = alloc["target"]
    plan = alloc["plan"]
    orders = plan.orders

    pe_pct = ctx.market_pe.get("pe_pct") if ctx.market_pe else None
    if pe_pct is not None:
        level = pe_level(pe_pct)
        gate_line = f"**PE 分位**: {pe_pct:.0f}%（{level}，仅作参考）"
    else:
        gate_line = "**PE 分位**: 数据不可用"

    assets_line = f"**总资产**: {total_assets:,.0f} 元"
    if rotation_mv > 0:
        assets_line += f" | **卫星/其他账户持仓**: {rotation_mv:,.0f} 元（由现金桶吸收）"
    lines.append(f"{assets_line} | {gate_line}")
    lines.append(f"**本次批次**: {plan.reason}")
    lines.append("")

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
                 "收盘突破此前20日最高价入场、跌破此前10日最低价离场，通道内延续历史状态。"
                 "当日新信号对应下一交易日开盘，不代表实际持仓或本周新增买卖指令。")
    lines.append("> ★ 恒生科技：当前样本中唐奇安20/10改善了持有体验，为重点观察规则。")
    lines.append("> 估值展示跟踪指数PE/股息率；红利沿用股息率减10年国债利差≥1.5pt视同低估的现有口径。"
                 "其余标的暂无高低判定，—表示无数据或不适用。两列独立参考，不合成评分，不参与调仓建议。")
    dated = {}
    unavailable = []
    for asset in _baseline():
        if asset.code == "CASH":
            continue
        observation = ctx.trends.get(asset.code, {})
        if observation.get("asof"):
            dated.setdefault(observation["asof"], []).append(asset.code)
        if observation.get("state") is None:
            reason = observation.get("error", "尚无明确突破" if observation else "未取到行情")
            unavailable.append(f"{asset.name}（{reason}）")
    for date, codes in sorted(dated.items()):
        lines.append(f"> 唐奇安行情截至 {date} 收盘：{'、'.join(codes)}。")
    if unavailable:
        lines.append(f"> 唐奇安暂无判断：{'、'.join(unavailable)}。")

    # 卫星持仓与基准外持仓提示（由"现金（以及其他账户）"桶吸收，不产生核心偏离）
    if rotation_positions:
        lines.append("")
        lines.append("> 卫星持仓（现金桶吸收，不参与核心再平衡）："
                     + "、".join(f"{p.get('name', '')}({p.get('code', '')})" for p in rotation_positions))
    baseline_codes = {a.code for a in _baseline()}
    extra = [f"{p.get('name', '')}({p.get('code', '')})" for p in alloc["core_positions"]
             if p.get("code") not in baseline_codes]
    if extra:
        lines.append("")
        lines.append(f"> 基准外持仓：{'、'.join(extra)}（未纳入对照）")

    if orders:
        lines.append("")
        lines.append("**建议调仓指令（仅供参考，不自动执行）**")
        lines.append("")
        for o in orders:
            lines.append(f"- {o.action.upper()} {o.name}({o.code}) {o.quantity}股 ≈ {o.amount:,.0f}元 — {o.reason}")
    else:
        lines.append("")
        lines.append("无调仓需求，保持当前配置。")

    # 残腿：应卖但持仓不足一手，交易所只收整手——不出必拒卖单，列手动待办
    for o in plan.odd_lots:
        lines.append(f"- ⚠️ {o['name']}({o['code']}) 高于目标应减，但持仓 {o['count']} 股不足一手，"
                     f"无法整手卖出，需手动处理")

    lines.append("")
    return "\n".join(lines)


def _deploy_cash_section(ctx: WeekContext) -> str:
    """新钱投放参考（每周固定输出）：只报告一个全市场估值分位，并入快慢人工把握。

    缺口补入不在本节：新钱入金后各资产自然欠配，按基准目标权重自行计算缺口买入即可
    （目标权重见持仓对照表）；名义台账口径的缺口由旧钱再平衡处理。
    新钱为真实资金、独立于名义台账，到账后人工在真实账户操作。
    """
    from src.real_account import ACCOUNT_PATH
    actual = ACCOUNT_PATH.exists()
    lines = ["## 新钱投放参考（估值分位，仅数据）", ""]
    market_pe = ctx.market_pe
    if market_pe:
        level = pe_level(market_pe["pe_pct"])
        lines.append(f"全市场 PE 近 5 年分位 **{market_pe['pe_pct']:.0f}%**（{level}）。"
                     "并入快慢据此自行把握，本节不做节奏判定。")
    else:
        lines.append("全市场估值数据不可用。")
    lines.append("")
    lines.append("> 入金后先在网页记录资金转入，再参考重新计算的配置缺口。" if actual else
                 "> 入金后按基准目标权重补入：缺口 = 目标权重 x 总资产 - 实际市值。")
    lines.append("> 券商成交后在网页填写实际份额、均价和手续费，更新持仓与现金。" if actual else
                 "> 到账后在真实账户人工操作，不自动执行。")
    lines.append("")
    return "\n".join(lines)


# ── 实际账户建议说明与模拟模式兼容入口 ──

def _execute_batch(alloc: dict, ledger_path=None, dry_run: bool = False) -> str:
    """实际账户只返回建议说明，由用户确认成交记账。

    未初始化实际账户时兼容模拟批次，委托 trade_ledger.execute_batch 完成
    收敛、安全校验、先卖后买及逐单记账；dry_run 只渲染，不写模拟流水。
    """
    from src.trade_ledger import BatchOrder, execute_batch

    from src.real_account import ACCOUNT_PATH
    if ACCOUNT_PATH.exists() and ledger_path is None:
        return "## 本次建议\n\n调仓建议已展示在网页；实际份额和现金仅在确认成交后更新。"

    plan = alloc["plan"]

    # 新钱投放只出建议（台账无法感知实际入金，不进自动记账批次，人工照建议操作）
    orders = [o for o in plan.orders if o.action in ("sell", "buy")]

    lines = ["## 本次执行", ""]

    if not orders:
        lines.append("无调仓指令，本次不执行。")
        return "\n".join(lines)

    if dry_run:
        lines.append(f"**本次执行**：试运行，计划 {len(orders)} 笔，未记账。")
        lines.append("**dry-run：以下指令未记账**")
        lines.append("")
        lines.extend(f"- [DRY] {o.action.upper()} {o.name}({o.code})"
                     f" {o.qty}股 ≈ {o.amount:,.0f}元（{o.reason}）"
                     for o in orders)
        return "\n".join(lines)

    price_map = {p.get("code", ""): float(p.get("current_price", 0) or 0)
                 for p in alloc["positions"]}

    def _order_price(order):
        """成交价兜底链：持仓现价 → 补价快照 → 金额/股数推算。"""
        price = price_map.get(order.code) or alloc["prices"].get(order.code)
        if price:
            return round(price, 4)
        return round(order.amount / order.qty if order.qty else 0, 4)

    batch_orders = [BatchOrder(
        code=o.code, name=o.name, side=o.action, qty=o.qty,
        price=_order_price(o), reason=o.reason) for o in orders]

    res = execute_batch(batch_orders, account="core",
                        path=ledger_path)
    if res.abort:
        lines.append(f"❌ 中止执行：{res.abort}")
        return "\n".join(lines)

    results = [oc.outcome_line for oc in res.outcomes]
    ok_count = sum(1 for oc in res.outcomes if oc.status == "ok")

    lines.append(f"**本次执行**：成功记账 {ok_count}/{len(res.outcomes)} 笔；"
                 f"未完成 {len(res.outcomes) - ok_count} 笔。")
    lines.append("")
    lines.extend(f"- {r}" for r in results)
    lines.append("")
    return "\n".join(lines)


# ── 报告生成（单动词 interface）──

def run(dry_run: bool = False) -> str:
    """生成周报；实际账户只出建议，兼容未初始化实际账户时的模拟模式。

    取数一次装配进 WeekContext，各节纯渲染。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    ctx = _build_week_context()
    alloc = ctx.alloc

    from src.real_account import ACCOUNT_PATH, render_summary as actual_summary
    summary = actual_summary(ACCOUNT_PATH) if ACCOUNT_PATH.exists() else render_summary(label="核心配置")
    sections = [
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}{'（dry-run，未记账）' if dry_run else ''}",
        "",
        summary,
    ]

    # 执行门保持现状："orders 非空即执行"；plan.should 为触发层结论，当前仅进报告。
    if alloc and alloc["plan"].orders:
        sections.append(_execute_batch(alloc, dry_run=dry_run))
    else:
        if alloc is None:
            note = "持仓数据不可用，本次未执行。"
        else:
            note = "无调仓指令，保持当前持仓。"
        sections.append("## 本次执行\n\n" + note)
    sections.extend([_holding_overview(ctx), _market_overview(ctx),
                     _valuation_reference(ctx), _deploy_cash_section(ctx)])

    sections.extend([
        "---",
        "",
        "*免责声明：本报告仅供观察参考，不构成投资建议。*",
    ])

    return "\n\n".join(sections)
