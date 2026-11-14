"""在同一批中期动量候选里比较量价因子的后续收益。"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from research.studies.etf.v1_local import MomentumLocal, STUDY


def screen(cache_dir=None):
    bt = MomentumLocal(cache_dir=cache_dir)
    close = pd.DataFrame(np.where(bt.valid, bt.adjusted_close, np.nan),
                         index=bt.cal, columns=bt.columns)
    opening = bt.data._frame("hfq_open").reindex(columns=close.columns)
    amount = bt.amount.reindex(columns=close.columns)
    returns = np.log(close).diff()
    momentum = (close.pct_change(60, fill_method=None)
                + close.pct_change(120, fill_method=None)) / 2
    factors = {
        "trend_efficiency": np.log(close).diff(60) / returns.abs().rolling(60).sum(),
        "near_high": close / close.rolling(250).max(),
        "amount_expansion": amount.rolling(20).mean() / amount.shift(20).rolling(60).mean(),
    }
    liquid = amount.shift(1).rolling(20).mean().ge(bt.k.LIQ_AMT20_MIN)
    mature = close.notna().rolling(bt.k.MIN_BARS).sum().eq(bt.k.MIN_BARS)
    eligible = liquid & mature & momentum.gt(0)
    rows = []
    first = bt.cal.searchsorted("2016-01-01")
    for loc in range(first, len(bt.cal) - 21, 20):
        date = bt.cal[loc]
        leaders = momentum.iloc[loc].where(eligible.iloc[loc]).nlargest(12).dropna()
        for name, factor in factors.items():
            values = factor.iloc[loc].reindex(leaders.index)
            values = values.where(np.isfinite(values)).dropna()
            if len(values) < 6:
                continue
            base = leaders.reindex(values.index).nlargest(3).index
            top = values.nlargest(3).index
            bottom = values.nsmallest(3).index
            codes = base.union(top).union(bottom)
            start = opening.iloc[loc + 1].reindex(codes)
            end = opening.iloc[loc + 21].reindex(codes)
            if not (start.gt(0) & end.gt(0) & np.isfinite(start) & np.isfinite(end)).all():
                continue
            future = end / start - 1
            rows.append(dict(
                date=date, factor=name, candidates=len(values), baseline=future[base].mean(),
                top=future[top].mean(), bottom=future[bottom].mean(),
                top_minus_base=future[top].mean() - future[base].mean(),
                top_minus_bottom=future[top].mean() - future[bottom].mean(),
            ))
    return pd.DataFrame(rows)


def summarize(rows):
    periods = {
        "2016–2020": ("2016-01-01", "2020-12-31"),
        "2021–2023": ("2021-01-01", "2023-12-31"),
        "2024–2026": ("2024-01-01", "2026-09-30"),
        "all": ("2016-01-01", "2026-09-30"),
    }
    results = []
    for period, (start, end) in periods.items():
        for factor, sample in rows[rows.date.between(start, end)].groupby("factor"):
            results.append(dict(
                period=period, factor=factor, cohorts=len(sample),
                top_minus_base_pp=sample.top_minus_base.mean() * 100,
                top_minus_bottom_pp=sample.top_minus_bottom.mean() * 100,
                win_rate_pct=sample.top_minus_base.gt(0).mean() * 100,
            ))
    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir")
    parser.add_argument("--out", type=Path, default=STUDY / "results" / "factor_screen")
    args = parser.parse_args()
    rows = screen(args.cache_dir)
    summary = summarize(rows)
    args.out.mkdir(parents=True, exist_ok=True)
    rows.to_csv(args.out / "cohorts.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(args.out / "summary.csv", index=False, encoding="utf-8-sig")
    print(summary.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
