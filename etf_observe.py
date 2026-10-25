# -*- coding: utf-8 -*-
"""
==================================
ETF 周度观察报告 — 估值导向
==================================

定位：每周一次，自动执行统一调仓批次——对名义成交台账记账
（影子成交，非真实下单；真实账户按报告百分比手动执行）。

持仓/资金事实来源 = data/trade_ledger.jsonl（docs/trade_ledger.md），
derive 供料与妙想持仓 dict 同形，rebalancer 决策核零改动。

报告结构：
  1. 市场估值概览（纯数据：全市场 PE / 收益率 / 风险溢价及其分位，不给操作建议）
  2. 各标的估值参考（锚对准跟踪指数当前 PE/股息率，跨标的不可比，仅展示）
  3. 新钱投放参考（全市场估值分位一个数，节奏人工决策，真实账户人工执行）
  4. 持仓对照与调仓建议（核心口径，旧钱只做阈值再平衡）

估值口径分两层，不互相兜底：市场级 = 全市场 PE/ERP 分位（第一、三节）；
标的级 = 跟踪指数自身 PE/股息率（估值参考节，无锚或取数失败即显示"无估值锚"）。
任何估值都不进自动决策——核心仓目标恒等于中性基准。
"""

import argparse
import io
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.config import setup_env
from src.cron_entry import add_standard_flags, hard_exit, resolve_run_gate
from src.logging_config import setup_logging

setup_env()

from src.etf.config import CORE_BASELINE, DIVIDEND_STYLE_CODES, AssetType, pe_level
from src.etf.amazing_factors import (
    get_market_pe, get_treasury_yield_y10, get_erp_percentile,
    etf_valuation_rows,
)
from src.task_io import notify, save_report
from src.trade_ledger.performance import render_summary

logger = logging.getLogger(__name__)

# ── 周度取数上下文（一次性装配，各节纯渲染）──

@dataclass(frozen=True)
class WeekContext:
    """入口一次装配的取数快照；渲染节只读此上下文，不再各自发起同主题取数。

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
    market_pe = get_market_pe()
    y10 = get_treasury_yield_y10()
    try:
        erp = get_erp_percentile()
    except Exception:
        erp = None
    erp_pct = erp.get("erp_pct") if erp else None
    equity_etfs = [a for a in CORE_BASELINE if a.asset_type == AssetType.EQUITY]
    return WeekContext(market_pe=market_pe, y10=y10, erp_pct=erp_pct,
                       alloc=_compute_allocation(), trends=_fetch_trend_observations(),
                       valuations=etf_valuation_rows(equity_etfs))


def _fetch_trend_observations() -> dict:
    """逐只观察自身ETF，供应商复权OHLC；失败不替换成原始价或其他ETF。"""
    from data_provider.bars import get_etf_research_daily
    from src.etf.trend import donchian_observation

    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    observations = {}
    for asset in CORE_BASELINE:
        if asset.code == "CASH":
            continue
        try:
            bars = get_etf_research_daily(asset.code, now.strftime("%Y-%m-%d"))
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
        parts.append(f"利差 {spread:+.1f}pt" + ("（偏低）" if spread >= 1.5 else ""))
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
            lines.append(
                f"- **风险溢价**：{100/pe:.2f}% − 国债 {y10:.2f}% = **{spread:+.2f}%**{erp_note} "
                f"（{'承担风险有额外回报' if spread > 0 else '股票还不如国债'}）"
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

def _fetch_missing_prices(baseline: List, positions: List[dict], fm) -> dict:
    """为未持仓/缺价的基准 ETF 补价：日线收盘为主口径（盘后分析数据稳定），
    注入的行情 manager 实时报价兜底；失败不补（compare 对无价标的自然跳过）。"""
    held_price = {p.get("code", ""): (p.get("current_price", 0) or 0) for p in positions}
    needed = [a.code for a in baseline if a.code != "CASH" and held_price.get(a.code, 0) <= 0]
    if not needed:
        return {}
    from data_provider.bars import get_etf_daily

    logger.info(f"获取 {len(needed)} 只 ETF 的行情价格...")
    prices = {}
    for code in needed:
        price = 0.0
        try:
            df = get_etf_daily(code)
            if df is not None and not df.empty:
                price = float(df.iloc[-1]["close"])
        except Exception as e:
            logger.debug(f"get_etf_daily 获取 {code} 价格失败: {e}")
        if price <= 0 and fm is not None:
            try:
                quote = fm.get_realtime_quote(code)
                if quote and getattr(quote, "price", None):
                    price = float(quote.price)
            except Exception:
                pass
        if price > 0:
            prices[code] = price
    return prices


def _compute_allocation():
    """计算目标配比 + 再平衡 plan（供对照展示与自动执行复用）

    持仓/资金读名义成交台账推导（docs/trade_ledger.md：
    derive 供料与持仓 dict 同形）。
    目标 = 中性基准，无动态择时；**不读环境快照**（核心仓纯机械）。
    行情取数在此编排层一次取齐（prices），决策核 rebalancer 保持零 I/O。

    Returns:
        dict（台账恒可推导；读不了时返回 None → 持仓对照节渲染跳过文案）
    """
    try:
        from src.trade_ledger import snapshot_with_prices

        snap = snapshot_with_prices()
    except Exception as e:
        logger.warning(f"成交台账读取失败，跳过持仓对照: {e}", exc_info=True)
        return None

    from src.etf.rebalancer import ETFRebalancer
    from data_provider import get_fetcher

    positions = snap.positions
    total_assets = snap.total_assets
    # 入口统一构造数据 manager，供补价回退的实时行情使用（避免各模块重复初始化数据源）
    fm = get_fetcher()
    rebalancer = ETFRebalancer()
    target = rebalancer.calculate_target()

    core_positions, rotation_mv, rotation_positions = rebalancer.split_rotation_positions(positions)
    # 台账缺价的持仓 + 未持仓基准 ETF 一并补价（compare 对无价标的自然跳过）
    prices = _fetch_missing_prices(rebalancer.baseline, positions, fm)
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
    for asset in CORE_BASELINE:
        code = asset.code
        tgt = target.get(code, 0.0) * 100
        cur = current_map.get(code, 0.0)
        dev = tgt - cur
        order = order_map.get(code)
        action = ""
        if order:
            action = f"{'🟢买' if order.action == 'buy' else '🔴卖'} {order.quantity}股"
        trend = ctx.trends.get(code, {})
        state = trend.get("state")
        trend_text = "√" if state is True else "×" if state is False else "—"
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
                 "其余标的暂无高低判定，—表示无数据或不适用。两列独立参考，不合成评分，不参与名义调仓。")
    dated = {}
    unavailable = []
    for asset in CORE_BASELINE:
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
    baseline_codes = {a.code for a in CORE_BASELINE}
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
    lines = ["## 新钱投放参考（估值分位，仅数据）", ""]
    market_pe = ctx.market_pe
    if market_pe:
        level = pe_level(market_pe["pe_pct"])
        lines.append(f"全市场 PE 近 5 年分位 **{market_pe['pe_pct']:.0f}%**（{level}）。"
                     "并入快慢据此自行把握，本节不做节奏判定。")
    else:
        lines.append("全市场估值数据不可用。")
    lines.append("")
    lines.append("> 入金后按基准目标权重补入：缺口 = 目标权重 x 总资产 - 实际市值；名义台账的缺口见执行前持仓对照。")
    lines.append("> 到账后在真实账户人工操作（再平衡批次只记名义台账，与新钱无关），不自动执行。")
    lines.append("")
    return "\n".join(lines)


# ── 自动调仓执行 ──

def _execute_batch(alloc: dict, ledger_path=None, dry_run: bool = False) -> str:
    """执行核心调仓批次：批次语义（收敛/安全校验/先卖后买/逐单记账）
    全权委托 trade_ledger.execute_batch，本函数只装配指令与渲染结果。

    只记名义台账（docs/trade_ledger.md），
    真实账户由用户按报告百分比手动执行。
    dry_run=True 时同渲染指令、零台账写入。
    """
    from src.trade_ledger import BatchOrder, execute_batch

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
    batch_orders = [BatchOrder(
        code=o.code, name=o.name, side=o.action, qty=o.qty,
        price=round(price_map.get(o.code) or alloc["prices"].get(o.code)
                     or (o.amount / o.qty if o.qty else 0), 4),
        reason=o.reason) for o in orders]

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


# ── 报告生成 ──

def _generate_report(dry_run: bool = False) -> str:
    """生成完整周报（orders 非空时自动执行调仓并附结果；dry_run 只渲染不记账）

    取数一次装配进 WeekContext，各节纯渲染。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    ctx = _build_week_context()
    alloc = ctx.alloc

    sections = [
        f"# ETF 周度观察 — {datetime.now().strftime('%Y-%m-%d')}",
        "",
        f"**生成时间**: {now}{'（dry-run，未记账）' if dry_run else ''}",
        "",
        render_summary(label="ETF共享组合（核心配置＋行业动量）"),
    ]

    # 执行门保持现状："orders 非空即执行"；plan.should 为触发层结论，当前仅进报告。
    if alloc and alloc["plan"].orders:
        sections.append(_execute_batch(alloc, dry_run=dry_run))
    else:
        sections.append("## 本次执行\n\n" + ("持仓数据不可用，本次未执行。" if alloc is None
                        else "无调仓指令，保持当前持仓。"))
    sections.extend([_holding_overview(ctx), _market_overview(ctx),
                     _valuation_reference(ctx), _deploy_cash_section(ctx)])

    sections.extend([
        "---",
        "",
        "*免责声明：本报告仅供观察参考，不构成投资建议。*",
    ])

    return "\n\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description='ETF 周度观察报告')
    add_standard_flags(parser)
    args = parser.parse_args()

    setup_logging(log_prefix="etf_observe", debug=args.debug)

    # 交易日检查：非交易日且未 --force 时直接跳过（节假日周一）
    skip, dry_run = resolve_run_gate(force=args.force, dry_run=args.dry_run)
    if skip:
        logger.info("今天不是 A 股交易日，跳过执行")
        print("今天不是 A 股交易日，跳过执行")
        return 0
    if dry_run and not args.dry_run:
        logger.info("非交易日 --force：本次只读，不写台账")

    logger.info("=" * 60)
    logger.info("ETF 周度观察报告（自动调仓）")
    logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    try:
        report = _generate_report(dry_run=dry_run)
    except Exception as e:
        logger.exception(f"生成报告失败: {e}")
        return 1

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    print("\n" + report)
    save_report(report, "etf_weekly")

    if not args.no_notify:
        notify(report)

    return 0


if __name__ == "__main__":
    # 硬退出理由（SDK 非守护回调线程）单点在 cron_entry.hard_exit。
    hard_exit(main())
