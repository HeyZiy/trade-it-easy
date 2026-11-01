# -*- coding: utf-8 -*-
"""20日均线±1.5ATR通道的独立对照；收盘确认，下一交易日开盘成交。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import donchian as dc

base = dc.base
OUT = base.STUDY / "results" / "supplementary" / "atr_channel"
# (均线周期, ATR周期, ATR倍数, 出场线, 均线算法, ATR平滑算法)
CASES = {
    "MA20_ATR20×1.5_下轨退出": (20, 20, 1.5, "lower", "sma", "wilder"),
    "MA20_ATR20×1.5_均线退出": (20, 20, 1.5, "middle", "sma", "wilder"),
    "MA20_ATR20×1.0_下轨退出": (20, 20, 1., "lower", "sma", "wilder"),
    "MA20_ATR20×2.0_下轨退出": (20, 20, 2., "lower", "sma", "wilder"),
    "MA15_ATR20×1.5_下轨退出": (15, 20, 1.5, "lower", "sma", "wilder"),
    "MA25_ATR20×1.5_下轨退出": (25, 20, 1.5, "lower", "sma", "wilder"),
    "MA20_ATR10×1.5_下轨退出": (20, 10, 1.5, "lower", "sma", "wilder"),
    "MA20_ATR14×1.5_下轨退出": (20, 14, 1.5, "lower", "sma", "wilder"),
    "MA20_简单ATR20×1.5_下轨退出": (20, 20, 1.5, "lower", "sma", "sma"),
    "EMA20_ATR20×1.5_下轨退出": (20, 20, 1.5, "lower", "ema", "wilder"),
}
ROLL_CASES = tuple(CASES)


def average_true_range(history, period=20, method="wilder"):
    if not isinstance(period, int) or period < 1 or method not in ("wilder", "sma"):
        raise ValueError("ATR周期须为正整数，算法须为 wilder/sma")
    previous = history.close.shift(1)
    tr = pd.concat([history.high - history.low, (history.high - previous).abs(),
                    (history.low - previous).abs()], axis=1).max(axis=1)
    tr = tr.where(history[["high", "low", "close"]].notna().all(axis=1))
    seed = tr.rolling(period, min_periods=period).mean()
    if method == "sma":
        return seed
    # Wilder首值为前period个TR均值，随后ATR=(前ATR*(period-1)+当日TR)/period。
    values = np.full(len(history), np.nan)
    raw = tr.to_numpy()
    seeds = seed.to_numpy()
    for i in range(period - 1, len(history)):
        if i == 0 or np.isnan(values[i - 1]):
            values[i] = seeds[i]
        elif not np.isnan(raw[i]):
            values[i] = (values[i - 1] * (period - 1) + raw[i]) / period
    return pd.Series(values, index=history.index)


def channel_signals(history, ma_period=20, atr_period=20, multiple=1.5,
                    exit_line="lower", ma_method="sma", atr_method="wilder"):
    if not isinstance(ma_period, int) or ma_period < 1 or multiple <= 0:
        raise ValueError("均线周期须为正整数，ATR倍数须为正")
    if exit_line not in ("lower", "middle") or ma_method not in ("sma", "ema"):
        raise ValueError("出场线须为 lower/middle，均线算法须为 sma/ema")
    if ma_method == "sma":
        middle = history.close.rolling(ma_period, min_periods=ma_period).mean()
    else:
        middle = history.close.ewm(span=ma_period, adjust=False, min_periods=ma_period).mean()
    atr = average_true_range(history, atr_period, atr_method)
    upper, lower = middle + multiple * atr, middle - multiple * atr
    exit_price = lower if exit_line == "lower" else middle
    valid = history.close.notna() & upper.notna() & exit_price.notna()
    signal = pd.Series(np.nan, index=history.index)
    signal.loc[valid & (history.close > upper)] = 1.
    signal.loc[valid & (history.close < exit_price)] = 0.
    # 均线和ATR允许使用当天完整日线，因为信号在收盘后才生成。
    return pd.DataFrame({"close": history.close, "middle": middle, "atr": atr,
                         "upper": upper, "lower": lower, "exit_price": exit_price,
                         "target": signal,
                         "execution_date": pd.Series(history.index, index=history.index).shift(-1)}
                        ).rename_axis("signal_date")


def evaluate(history):
    px = history.loc[base.window_start(history):]
    curves, ledgers, targets, signals, rows = {}, {}, {}, {}, {}
    for name in ("长拿", *CASES):
        ledgers[name] = []
        if name == "长拿":
            curves[name], count = base.sim_buyhold(px, ledger=ledgers[name])
        else:
            signals[name] = channel_signals(history, *CASES[name])
            targets[name] = dc.targets_from_signals(signals[name], start=px.index[0])
            curves[name], count = base.sim_target(px, targets[name], ledger=ledgers[name])
        rows[name] = base.metrics(curves[name].nav, curves[name].exposure, count)
    met = pd.DataFrame(rows).T.rename_axis("strategy")
    met = base.experience.add_benchmark_comparison(met, curves)
    ann = base.annual_returns({name: curve.nav for name, curve in curves.items()})
    return met, ann, curves, ledgers, targets, signals


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_metrics, all_cohorts, all_summary = [], [], []
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
        all_metrics.append(met.reset_index().assign(code=code))
        print(f"\n{asset.name}\n" + met.round(2).to_markdown())
        for years in (1, 3, 5):
            cohorts = dc.rolling_cohorts(history, targets, years, cases=ROLL_CASES)
            all_cohorts.append(cohorts.assign(code=code))
            all_summary.append(dc.rolling_summary(cohorts).reset_index().assign(code=code, years=years))
        print(code, "多起点计算完成")
    pd.concat(all_metrics, ignore_index=True).round(4).to_csv(
        OUT / "metrics_all.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_cohorts, ignore_index=True).round(6).to_csv(
        OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    roll = pd.concat(all_summary, ignore_index=True)
    roll.round(4).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    print("\n固定3年窗口\n" + roll[roll.years == 3].round(2).to_markdown(index=False))
    files = [Path(__file__), Path(dc.__file__), Path(base.__file__), Path(base.experience.__file__), Path(base.asset_config.__file__),
             *(base.PE_CACHE / f"etf_hfq_{asset.etf_code}.csv" for asset in base.ASSETS)]
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
                "cutoff": "2026-09-30", "cases": CASES,
                "main_case": next(iter(CASES)), "confirmation": "daily_close",
                "execution": "next_trading_open", "initial_state": "cash",
                "atr_seed": "mean_of_first_n_true_ranges",
                "atr_wilder_update": "(previous_atr*(n-1)+current_tr)/n",
                "rolling": {"start_frequency": "monthly", "holding_years": [1, 3, 5],
                            "fresh_account_each_start": True, "overlapping_windows": True},
                "windows": {asset.etf_code: {"start": str(base.window_start(base.load_etf(asset.etf_code)).date()),
                                             "end": str(base.load_etf(asset.etf_code).index[-1].date())} for asset in base.ASSETS},
                "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
