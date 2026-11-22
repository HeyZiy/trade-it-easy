# -*- coding: utf-8 -*-
"""市场观察周报数据层：全市场 PE 五档 + 股债性价比 + ERP 分位。

唯一消费者是根目录入口 etf_observe.py（周一 14:40 推送），只观察不交易。
"""

import logging
from datetime import datetime

from src.etf.config import pe_level

logger = logging.getLogger(__name__)


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


# ── 市场宏观 ──

def _market_overview(market_pe, y10, erp_pct) -> str:
    """展示全市场 PE、国债收益率和风险溢价。"""
    lines = ["## 市场观察", ""]

    if market_pe:
        pe, pe_pct = market_pe["pe"], market_pe["pe_pct"]
        level = pe_level(pe_pct)
        lines.append(f"- **全市场 PE**：{pe:.1f} 倍 | 近 5 年 **{pe_pct:.0f}%** 分位 →  **{level}**")
        lines.append(f"- **股票内在收益率（1/PE）**：{100/pe:.2f}%")

        if y10:
            spread = 100/pe - y10
            # ERP 自身分位与 PE 分位同窗口可比：PE 分位受窗口锚定影响（含深熊底部则读高），
            # ERP 分位是跨资产口径的独立参照，两者并列展示供互相印证
            erp_note = ""
            if erp_pct is not None:
                erp_note = f"，自身近 5 年 **{erp_pct:.0f}%** 分位"
            spread_note = "承担风险有额外回报" if spread > 0 else "股票还不如国债"
            lines.append(
                f"- **风险溢价**：{100/pe:.2f}% − 国债 {y10:.2f}% = **{spread:+.2f}%**{erp_note} "
                f"（{spread_note}）"
            )
    else:
        lines.append("- **全市场 PE**：数据不可用")
        if y10:
            lines.append(f"- **10 年期国债收益率**：{y10:.2f}%")

    lines.append("")
    return "\n".join(lines)


# ── 报告生成（单动词 interface）──

def run() -> str:
    """从市场数据生成周报。"""
    now = datetime.now()
    market_pe = _raw_market_pe()
    y10 = _raw_treasury_yield_y10()
    try:
        erp = _raw_erp_percentile()
    except Exception:
        erp = None

    return "\n\n".join((
        f"# 市场观察周报 — {now:%Y-%m-%d}",
        f"**生成时间**：{now:%Y-%m-%d %H:%M}",
        _market_overview(market_pe, y10, erp.get("erp_pct") if erp else None),
        "---",
        "*本报告仅供观察参考。*",
    ))
