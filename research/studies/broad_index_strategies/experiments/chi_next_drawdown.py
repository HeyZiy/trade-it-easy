"""创业板长回撤诊断：固定规则、同时间比较、逐日损益与持仓段核对。

运行 python -m research.studies.broad_index_strategies.experiments.chi_next_drawdown
只读已有缓存，不试新参数；损益拆分是记账描述，不是改成交规则的反事实。
"""
import numpy as np
import pandas as pd

from .. import run_level1 as study

base = study.base
OUT = base.STUDY / "results" / "chi_next_drawdown"
RULES = ("长拿", "唐奇安20/10", "唐奇安55/20", study.ATR_CASE)


def daily_pnl(px, curve, ledger):
    """实际股数拆隔夜、日内和费用；三项逐日严格核对账户净值变化。"""
    previous_shares = curve.shares.shift(1, fill_value=0.)
    previous_close = px.close.shift(1)
    previous_close.iloc[0] = px.open.iloc[0]
    fees = pd.Series(0., index=px.index)
    for trade in ledger:
        fees.loc[trade["date"]] += trade["fee"]
    out = pd.DataFrame({
        "隔夜损益": previous_shares * (px.open - previous_close),
        "日内损益": curve.shares * (px.close - px.open),
        "费用": fees,
        "净值变化": curve.nav - curve.nav.shift(1, fill_value=base.CAPITAL),
        "退出日隔夜损益": 0.,
    })
    for trade in ledger:
        if trade["side"] == "sell":
            out.loc[trade["date"], "退出日隔夜损益"] = out.loc[trade["date"], "隔夜损益"]
    np.testing.assert_allclose(out["隔夜损益"] + out["日内损益"] - fees,
                               out["净值变化"], atol=1e-7)
    return out


def position_segments(px, curve, ledger, peak, trough):
    """前高收盘到谷底收盘的持仓段，期初持仓与期末未卖出按边界收盘估值。"""
    peak, trough = pd.Timestamp(peak), pd.Timestamp(trough)
    capital = curve.loc[peak, "nav"]
    quantity = curve.loc[peak, "shares"]
    entry = peak
    basis = quantity * px.loc[peak, "close"]
    carry = bool(quantity)
    rows = []

    def finish(date, value, closed):
        pnl = value - basis
        rows.append({"开始": entry, "结束": date, "期初已有持仓": carry,
                     "卖出结束": closed, "股数": quantity, "段损益": pnl,
                     "较前高净值贡献pp": pnl / capital * 100,
                     "段收益%": pnl / basis * 100})

    for trade in ledger:
        date = pd.Timestamp(trade["date"])
        if not peak < date <= trough:
            continue
        if trade["side"] == "buy":
            assert quantity == 0
            quantity, entry, carry = trade["quantity"], date, False
            basis = quantity * trade["price"] + trade["fee"]
        else:
            assert quantity == trade["quantity"]
            finish(date, quantity * trade["price"] - trade["fee"], True)
            quantity = 0
    if quantity:
        finish(trough, quantity * px.loc[trough, "close"], False)
    out = pd.DataFrame(rows)
    pnl = out["段损益"].sum() if rows else 0.
    np.testing.assert_allclose(pnl, curve.loc[trough, "nav"] - capital, atol=1e-7)
    return out


def replay(code):
    history = base.load_etf(code)
    px = history.loc[base.window_start(history):]
    signals = study.dc.channel_signals(history, entry=20, exit=10)
    ledger = []
    curve, _ = base.sim_target(px, study.dc.targets_from_signals(signals), ledger=ledger)
    return history, px, curve, ledger


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    histories = {code: base.load_etf(code) for code in ("159915", "513180")}
    rows = []
    for code, history in histories.items():
        runners = {k: v for k, v in study.strategy_runners(history).items() if k in RULES}
        for label, start in (("各自全期", base.window_start(history)),
                             ("共同窗口", pd.Timestamp("2022-03-18"))):
            metrics, _ = study.evaluate_window(history.loc[start:"2026-09-30"], runners)
            rows.append(metrics.reset_index().assign(code=code, 窗口=label, start=str(start.date())))
    comparisons = pd.concat(rows, ignore_index=True)
    comparisons.to_csv(OUT / "comparisons.csv", index=False, encoding="utf-8-sig")

    _, px, curve, ledger = replay("159915")
    episodes = base.experience.drawdown_periods(curve.nav, base.CAPITAL)
    episode = episodes.loc[episodes["段最大回撤%"].idxmin()]
    peak, trough = episode["前高日"], episode["谷底日"]
    attribution = daily_pnl(px, curve, ledger)
    part = attribution.loc[(attribution.index > peak) & (attribution.index <= trough)]
    capital = curve.loc[peak, "nav"]
    segments = position_segments(px, curve, ledger, peak, trough)
    segments.to_csv(OUT / "position_segments.csv", index=False, encoding="utf-8-sig")
    part.rename_axis("date").to_csv(OUT / "daily_attribution.csv", encoding="utf-8-sig")
    pd.DataFrame(ledger).to_csv(OUT / "trades.csv", index=False, encoding="utf-8-sig")
    hold, _ = base.sim_buyhold(px)
    annual = base.annual_returns({"长拿": hold.nav, "唐奇安20/10": curve.nav})
    annual.to_csv(OUT / "annual.csv", encoding="utf-8-sig")
    # 每段贡献以同一个前高账户净值为分母，能相加；不是把交易收益率直接相加。
    losses = segments.loc[segments["段损益"] < 0]
    wins = segments.loc[segments["段损益"] > 0]
    worst = segments.nsmallest(5, "段损益")
    worst_display = worst.drop(columns=["股数", "段损益"]).copy()
    for col in ("开始", "结束"):
        worst_display[col] = worst_display[col].dt.strftime("%Y-%m-%d")
    common = comparisons.loc[comparisons["窗口"] == "共同窗口"].copy()
    common["标的"] = common.code.map({"159915": "创业板", "513180": "恒生科技"})
    held = curve.loc[part.index, "shares"].gt(0).mean() * 100
    price_return = (px.loc[trough, "close"] / px.loc[peak, "close"] - 1) * 100
    cols = ["年化收益%", "最大回撤%", "最长水下交易日"]
    lines = ["# 创业板：为什么通道择时仍出现46%回撤？", "",
             "数据截至2026-09-30；只读取既有东财后复权缓存，单边成本0.06%，"
             "收盘确认、下一交易日开盘成交。沿用一级代表规则，不调参。", "",
             "## 同时间、同规则比较", "",
             "两只ETF均从2022-03-18以现金新建账户；窗口外历史只计算信号，不继承持仓。", "",
             common[["标的", "strategy", *cols]].round(2).to_markdown(index=False), "",
             "## 创业板全期最深回撤", "",
             f"唐奇安20/10前高 {peak.date()}，谷底 {trough.date()}，"
             f"恢复日 {episode['恢复日'].date()}；回撤 {episode['段最大回撤%']:.2f}%，"
             f"完整水下区间 {int(episode['水下交易日'])} 个交易日。", "",
             f"前高到谷底有 {len(segments)} 段持仓：{len(losses)} 段亏损、{len(wins)} 段盈利。"
             f"亏损段合计贡献 {losses['较前高净值贡献pp'].sum():.2f}pp，"
             f"盈利段合计 {wins['较前高净值贡献pp'].sum():+.2f}pp。", "",
             f"同期ETF收盘价变化 {price_return:.2f}%；策略收盘持仓天数占比 {held:.2f}%。"
             "这说明频繁空仓仍未消除亏损，不等同于同仓位对照已经证明择时无效。", "",
             "### 账户损益核对", "",
             "以下均除以前高账户净值，三项可相加；退出日隔夜为隔夜损益子集，不能重复相加。", "",
             pd.DataFrame({"贡献pp": {
                 "隔夜持仓损益": part["隔夜损益"].sum() / capital * 100,
                 "开盘后持仓损益": part["日内损益"].sum() / capital * 100,
                 "费用": -part["费用"].sum() / capital * 100,
                 "合计": part["净值变化"].sum() / capital * 100,
                 "其中：退出日隔夜损益": part["退出日隔夜损益"].sum() / capital * 100,
             }}).round(2).to_markdown(), "",
             "### 损失最大的五段持仓", "",
             worst_display.round(2).to_markdown(index=False), "",
             "### 年度路径（延续全期账户）", "",
             (annual.loc["2015":"2022"] * 100).round(2).to_markdown(), "",
             "## 当前解释", "",
             "1. 主要问题是多段亏损累计，而非一笔持仓跌穿46%。最差完整持仓段"
             "2016-02-23至2016-02-26亏损约9.92%；另有前高时已有持仓的边界损失。",
             "2. 费用约1个百分点，退出日隔夜合计为正；当前证据不支持把主要原因归结为"
             "手续费或收盘确认后卖出时的跳空。退出日合计为正也不代表每次跳空都无损。",
             "3. 恢复慢有两层：2016—2018连续年度亏损；2019、2020虽盈利，却明显落后长拿，"
             "因此不能只用避跌效果衡量完整持有体验。",
             "4. 同样的2022年以来样本中，创业板20/10也有改善。恒生科技的短样本尚不能证明"
             "它能够应付创业板早期这种反复失败的阶段。", "",
             "## 解释边界", "",
             "上述持仓段包含前高时已持有、谷底时尚未卖出的边界估值，不能全部称为完整交易。"
             "损益已逐日和逐段核对到账户；隔夜损失包含普通持仓隔夜与卖出日跳空，"
             "不能据此断言提前一天卖出就能避免。", "",
             "低胜率、反复亏损可描述为突破后的趋势缺少延续，但尚未测试额外过滤、止损或仓位规则。"
             "不以事后谷底截断窗口来挑参数，也不把同时间较好表现外推到未观察到的恒生科技早期历史。", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
