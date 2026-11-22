# -*- coding: utf-8 -*-
"""唐奇安、ATR 通道与 RSRS 主组对比：全期与滚动窗口。

运行 python -m research.studies.timing.experiments.compare_timing。
只读取三个补充实验已落盘的指标与滚动汇总做拼接，不重算信号；
对比口径（成本、成交、窗口）由共享引擎统一，见各族脚本。
"""
from pathlib import Path

import pandas as pd

from research.studies.timing.common import backtest as base

RES = base.RESULTS / "supplementary"
FAMILIES = {
    "唐奇安": "donchian",
    "ATR通道": "atr_channel",
    "RSRS": "rsrs",
}
MAIN_CASE = {
    "唐奇安": "唐奇安55/20",
    "ATR通道": "MA20_ATR20×1.5_下轨退出",
    "RSRS": "RSRS18_z0.7",
}
OUT = RES / "timing_compare"

RET, DD, SHARPE, CALMAR = "年化收益%", "最大回撤%", "Sharpe", "Calmar"
EXCESS, IMPROVE = "较长拿年化差pp", "较长拿回撤改善pp"


def load_family(family, subdir):
    met = pd.read_csv(RES / subdir / "metrics_all.csv", encoding="utf-8-sig")
    roll = pd.read_csv(RES / subdir / "rolling_summary.csv", encoding="utf-8-sig")
    met["族"] = family
    roll["族"] = family
    return met, roll


def full_period():
    frames = [load_family(fam, sub)[0] for fam, sub in FAMILIES.items()]
    all_met = pd.concat(frames, ignore_index=True)
    main = all_met[all_met["strategy"].isin(["长拿", *MAIN_CASE.values()])].copy()
    return main


def summarize_full(main):
    cols = [RET, DD, SHARPE, CALMAR, EXCESS, IMPROVE]
    pivot = main.pivot_table(index="code", columns="strategy", values=cols, aggfunc="first")
    stats = main.groupby("strategy")[cols].median()
    return pivot, stats


def rolling_three_years():
    frames = [load_family(fam, sub)[1] for fam, sub in FAMILIES.items()]
    roll = pd.concat(frames, ignore_index=True)
    r3 = roll[roll["years"] == 3].copy()
    r3["strategy"] = r3["族"] + "·" + r3["strategy"]
    stats = r3.groupby("strategy")[[
        "窗口数", "跑赢长拿比例%", "超额年化中位数pp", "超额年化P10pp",
        "回撤改善比例%", "收益回撤均改善比例%", "收益回撤均恶化比例%",
    ]].mean()
    return r3, stats


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    main_full = full_period()
    pivot, stats = summarize_full(main_full)
    main_full.to_csv(OUT / "metrics_full_period.csv", index=False, encoding="utf-8-sig")
    pivot.round(4).to_csv(OUT / "pivot_full_period.csv", encoding="utf-8-sig")
    stats.round(2).to_csv(OUT / "medians_full_period.csv", encoding="utf-8-sig")
    print(f"全期·跨标的中位数（{main_full.code.nunique()}只ETF）\n" + stats.round(2).to_markdown())
    print("\n全期·分标的\n" + pivot[RET].round(2).to_markdown() +
          "\n\n全期·分标的回撤\n" + pivot[DD].round(2).to_markdown())
    r3, roll_stats = rolling_three_years()
    r3.to_csv(OUT / "rolling_3y.csv", index=False, encoding="utf-8-sig")
    roll_stats.round(2).to_csv(OUT / "rolling_3y_stats.csv", encoding="utf-8-sig")
    print("\n3年滚动窗口·跨标的均值\n" + roll_stats.round(2).to_markdown())


if __name__ == "__main__":
    main()
