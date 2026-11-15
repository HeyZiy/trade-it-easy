# -*- coding: utf-8 -*-
"""RSRS 阻力支撑相对强度族扫描：收盘确认，下一交易日开盘成交。

维度对照：python -m research.studies.timing.common.rsrs。
主组为 18 日回归窗口、600 日标准分、阈值 0.7（光大证券原始参数），
其余每组只改一个维度；结果不回写一级或二级配置。

窗口内以 low 为自变量、high 为因变量做 OLS，斜率即阻力相对支撑强度；
斜率与 R² 直接解正规方程（与 statsmodels 等价）。标准分 z 用 600 日
滚动均值与标准差；右偏标准分 = (z + z³滚动均值) × R²，加权版 = z × R²。
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import donchian as dc

base = dc.base
OUT = base.RESULTS / "supplementary" / "rsrs"
# (回归窗口, 标准分阈值, 计分方式)；zscore 原始标准分，r2 加权，skew 右偏标准分。
CASES = {
    "RSRS18_z0.7": (18, 0.7, "zscore"),
    "RSRS18_z0.5": (18, 0.5, "zscore"),
    "RSRS18_z0.9": (18, 0.9, "zscore"),
    "RSRS12_z0.7": (12, 0.7, "zscore"),
    "RSRS20_z0.7": (20, 0.7, "zscore"),
    "RSRS24_z0.7": (24, 0.7, "zscore"),
    "RSRS18_z0.7_R2加权": (18, 0.7, "r2"),
    "RSRS18_z0.7_右偏": (18, 0.7, "skew"),
}
ROLL_CASES = tuple(CASES)


def rolling_ols(history, window):
    """逐日取窗口内 high 对 low 的 OLS 斜率与拟合优度；只用截至当日的数据。"""
    if not isinstance(window, int) or window < 2:
        raise ValueError("回归窗口须为不小于2的整数")
    high = history["high"].to_numpy()
    low = history["low"].to_numpy()
    n = len(history)
    beta = np.full(n, np.nan)
    r2 = np.full(n, np.nan)
    for i in range(window - 1, n):
        x = low[i - window + 1:i + 1]
        y = high[i - window + 1:i + 1]
        dx = x - x.mean()
        dy = y - y.mean()
        sxx = float(dx @ dx)
        if sxx <= 0:
            continue
        sxy = float(dx @ dy)
        beta[i] = sxy / sxx
        syy = float(dy @ dy)
        r2[i] = (sxy / np.sqrt(sxx * syy)) ** 2 if syy > 0 else np.nan
    return (pd.Series(beta, index=history.index, name="beta"),
            pd.Series(r2, index=history.index, name="r2"))


def rsrs_signals(history, window=18, z_window=600, threshold=0.7, score="zscore"):
    if not isinstance(z_window, int) or z_window < window:
        raise ValueError("标准分窗口须为不小于回归窗口的整数")
    if not 0 < threshold < 10:
        raise ValueError("标准分阈值须在 (0, 10) 内")
    if score not in ("zscore", "r2", "skew"):
        raise ValueError("计分方式须为 zscore/r2/skew")
    beta, r2 = rolling_ols(history, window)
    z = (beta - beta.rolling(z_window, min_periods=z_window).mean()) / \
        beta.rolling(z_window, min_periods=z_window).std()
    if score == "zscore":
        score_s = z
    else:
        adjusted = z + z.pow(3).rolling(z_window, min_periods=z_window).mean() \
            if score == "skew" else z
        score_s = adjusted * r2
    valid = score_s.notna()
    target = pd.Series(np.nan, index=history.index)
    state = None
    for i, value in enumerate(score_s):
        if not valid.iloc[i] or not np.isfinite(value):
            continue
        want = 1.0 if value > threshold else (0.0 if value < -threshold else None)
        if want is not None and want != state:
            target.iloc[i] = want
            state = want
    execution = pd.Series(history.index, index=history.index).shift(-1)
    return pd.DataFrame({"beta": beta, "r2": r2, "z": z, "score": score_s,
                         "target": target, "execution_date": execution}
                        ).rename_axis("signal_date")


def evaluate(history):
    px = history.loc[base.window_start(history):]
    curves, ledgers, targets, signals, rows = {}, {}, {}, {}, {}
    for name in ("长拿", *CASES):
        ledgers[name] = []
        if name == "长拿":
            curves[name], count = base.sim_buyhold(px, ledger=ledgers[name])
        else:
            window, threshold, score = CASES[name]
            signals[name] = rsrs_signals(history, window=window, threshold=threshold, score=score)
            targets[name] = dc.targets_from_signals(signals[name], start=px.index[0])
            curves[name], count = base.sim_target(px, targets[name], ledger=ledgers[name])
        rows[name] = base.metrics(curves[name].nav, curves[name].exposure, count)
    met = pd.DataFrame(rows).T.rename_axis("strategy")
    met = base.attach_round_trips(met, ledgers)
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
        print(code, "多起点计算完成", flush=True)
    pd.concat(all_metrics, ignore_index=True).round(4).to_csv(
        OUT / "metrics_all.csv", index=False, encoding="utf-8-sig")
    pd.concat(all_cohorts, ignore_index=True).round(6).to_csv(
        OUT / "rolling_cohorts.csv", index=False, encoding="utf-8-sig")
    roll = pd.concat(all_summary, ignore_index=True)
    roll.round(4).to_csv(OUT / "rolling_summary.csv", index=False, encoding="utf-8-sig")
    print("\n固定3年窗口\n" + roll[roll.years == 3].round(2).to_markdown(index=False))
    files = [Path(__file__), Path(dc.__file__), Path(base.__file__), Path(base.experience.__file__),
             Path(base.asset_config.__file__),
             *(base.ETF_DATA / f"etf_hfq_{asset.etf_code}.csv" for asset in base.ASSETS)]
    manifest = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "capital": base.CAPITAL, "cost": base.COST, "lot": base.LOT, "ann": base.ANN,
                "cutoff": "2026-09-30", "cases": CASES, "main_case": "RSRS18_z0.7",
                "regression": "OLS_high_on_low_numpy", "z_window": 600, "z_min_periods": 600,
                "confirmation": "daily_close", "execution": "next_trading_open",
                "initial_state": "cash", "scores": {
                    "zscore": "(beta-rolling_mean)/rolling_std",
                    "r2": "z*R2", "skew": "(z+z^3_rolling_mean)*R2"},
                "rolling": {"start_frequency": "monthly", "holding_years": [1, 3, 5],
                            "fresh_account_each_start": True, "overlapping_windows": True},
                "windows": {asset.etf_code: {"start": str(base.window_start(base.load_etf(asset.etf_code)).date()),
                                             "end": str(base.load_etf(asset.etf_code).index[-1].date())} for asset in base.ASSETS},
                "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
