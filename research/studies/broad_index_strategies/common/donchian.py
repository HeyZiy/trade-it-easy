# -*- coding: utf-8 -*-
"""唐奇安通道独立对照：历史通道、收盘确认、下一交易日开盘成交。

补充通道对照：python -m research.studies.broad_index_strategies.common.donchian。
复用原研究的行情、账户、成本和指标；不改变网格或四策略主表。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as base
from src.etf.trend import channel_signals

OUT = base.STUDY / "results" / "supplementary" / "donchian"
# 55/20 为主规则，20/10、20/20 为独立对照；其余检查邻近周期。
CASES = {
    "唐奇安55/20": (55, 20, "ohlc"),
    "唐奇安20/10": (20, 10, "ohlc"),
    "唐奇安20/20": (20, 20, "ohlc"),
    "唐奇安40/20": (40, 20, "ohlc"),
    "唐奇安55/10": (55, 10, "ohlc"),
    "唐奇安55/30": (55, 30, "ohlc"),
    "唐奇安70/20": (70, 20, "ohlc"),
    "收盘通道55/20": (55, 20, "close"),
    "收盘通道20/10": (20, 10, "close"),
}
ROLL_CASES = (*CASES, "日频MA200")


def targets_from_signals(signals, start=None):
    rows = signals.dropna(subset=["target", "execution_date"])
    if start is not None:
        rows = rows[rows.execution_date >= start]
    return dict(zip(rows.execution_date, rows.target))


def build_target(history, entry=55, exit=20, channel="ohlc", start=None):
    return targets_from_signals(channel_signals(history, entry, exit, channel), start)


def daily_ma_target(history, start=None, window=200):
    if not isinstance(window, int) or window < 1:
        raise ValueError("均线周期须为正整数")
    ma = history.close.rolling(window, min_periods=window).mean()
    valid = ma.notna() & history.close.notna()
    signals = pd.DataFrame({
        "target": (history.close > ma).astype(float).where(valid),
        "execution_date": pd.Series(history.index, index=history.index).shift(-1),
    })
    return targets_from_signals(signals, start)


def evaluate(history):
    px = history.loc[base.window_start(history):]
    curves, ledgers, targets, signals = {}, {}, {}, {}
    for name in ("长拿", "月末MA200", "日频MA200", *CASES):
        ledgers[name] = []
    curves["长拿"], count = base.sim_buyhold(px, ledger=ledgers["长拿"])
    metrics = {"长拿": base.metrics(curves["长拿"].nav, curves["长拿"].exposure, count)}
    targets["月末MA200"] = base.build_trend_target(history, start=px.index[0])
    targets["日频MA200"] = daily_ma_target(history, start=px.index[0])
    for name, (entry, exit, channel) in CASES.items():
        signals[name] = channel_signals(history, entry, exit, channel)
        targets[name] = targets_from_signals(signals[name], start=px.index[0])
    for name, target in targets.items():
        curves[name], count = base.sim_target(px, target, ledger=ledgers[name])
        metrics[name] = base.metrics(curves[name].nav, curves[name].exposure, count)
    met = pd.DataFrame(metrics).T.rename_axis("strategy")
    met = base.experience.add_benchmark_comparison(met, curves)
    ann = base.annual_returns({name: curve.nav for name, curve in curves.items()})
    return met, ann, curves, ledgers, targets, signals


def rolling_cohorts(history, targets, years=3, cases=None):
    """每月独立启动、固定日历年期限；同日起点空仓，窗口外历史仅用于指标。

    不延续启动前的虚拟持仓，不筛选有利日期，重叠窗口不作为独立样本。
    """
    px_all = history.loc[base.window_start(history):]
    rows = []
    cases = ROLL_CASES if cases is None else cases
    for start, end in base.experience.rolling_windows(px_all.index, years):
        px = history.loc[(history.index >= start) & (history.index < end)]
        hold, hold_count = base.sim_buyhold(px)
        hold_met = base.metrics(hold.nav, hold.exposure, hold_count)
        for name in cases:
            target = {t: want for t, want in targets[name].items() if start <= t < end}
            curve, count = base.sim_target(px, target)
            met = base.metrics(curve.nav, curve.exposure, count)
            rows.append(base.experience.cohort_row(name, px, years, met, hold_met, curve, hold))
    return pd.DataFrame(rows)


SUMMARY_COLS = base.experience.SUMMARY_COLS


def rolling_summary(cohorts):
    return base.experience.rolling_summary(cohorts)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    full_metrics, all_cohorts, all_roll = [], [], []
    for asset in base.ASSETS:
        code = asset.etf_code
        history = base.load_etf(code)
        met, ann, curves, ledgers, targets, signals = evaluate(history)
        met.round(4).to_csv(OUT / f"metrics_{code}.csv", encoding="utf-8-sig")
        ann.round(6).to_csv(OUT / f"annual_{code}.csv", encoding="utf-8-sig")
        relative = ann.add(1).div(ann["长拿"].add(1), axis=0).sub(1)
        relative.round(6).to_csv(OUT / f"annual_relative_{code}.csv", encoding="utf-8-sig")
        pd.concat(curves, names=["strategy", "date"]).reset_index().to_csv(
            OUT / f"daily_{code}.csv", index=False, encoding="utf-8-sig")
        records = [{"strategy": name, **trade} for name, ledger in ledgers.items() for trade in ledger]
        pd.DataFrame(records).sort_values("date", kind="stable").to_csv(
            OUT / f"trades_{code}.csv", index=False, encoding="utf-8-sig")
        pd.concat(signals, names=["strategy", "signal_date"]).reset_index().to_csv(
            OUT / f"signals_{code}.csv", index=False, encoding="utf-8-sig")
        full_metrics.append(met.reset_index().assign(code=code))
        # 1/3/5年使用相同月度启动方式，分别汇总，避免混淆持有期限。
        for years in (1, 3, 5):
            cohorts = rolling_cohorts(history, targets, years)
            all_cohorts.append(cohorts.assign(code=code))
            summary = rolling_summary(cohorts).reset_index().assign(code=code, years=years)
            all_roll.append(summary)
        print(f"\n{asset.name}\n" + met.round(2).to_markdown())
    pd.concat(full_metrics, ignore_index=True).round(4).to_csv(
        OUT / "metrics_all.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_cohorts, ignore_index=True).round(6).to_csv(
        OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    roll = pd.concat(all_roll, ignore_index=True)
    roll.round(4).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    print("\n多起点、固定3年持有期\n" + roll[roll.years == 3].round(2).to_markdown(index=False))
    files = [Path(__file__), Path(base.__file__), Path(base.experience.__file__), Path(base.asset_config.__file__),
             *(base.PE_CACHE / f"etf_hfq_{asset.etf_code}.csv" for asset in base.ASSETS)]
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
                "cutoff": "2026-09-30", "cases": CASES, "main_case": "唐奇安55/20",
                "confirmation": "daily_close_strictly_above_prior_upper_or_below_prior_lower",
                "execution": "next_trading_open", "initial_state": "cash",
                "rolling": {"start_frequency": "monthly", "holding_years": [1, 3, 5],
                            "fresh_account_each_start": True, "overlapping_windows": True},
                "windows": {asset.etf_code: {"start": str(base.window_start(base.load_etf(asset.etf_code)).date()),
                                             "end": str(base.load_etf(asset.etf_code).index[-1].date())} for asset in base.ASSETS},
                "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
