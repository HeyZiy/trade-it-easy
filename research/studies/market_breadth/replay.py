"""Replay A-share industry breadth and generate a local market report."""

import argparse
from pathlib import Path

import pandas as pd

if __package__:
    from .report import write_report
else:
    from report import write_report


DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data" / "market_breadth"


def load_panel(data_dir):
    names = pd.read_csv(data_dir / "sw_names.csv", index_col=0).iloc[:, 0]
    if len(names) != 31 or not names.index.is_unique or not names.is_unique:
        raise ValueError("Expected 31 distinct Shenwan level-one indices")
    industries = {}
    for code, name in names.items():
        source = pd.read_csv(data_dir / f"{code}.csv", parse_dates=["bargaindate"])
        industries[name] = source.set_index("bargaindate")["closeindex"]
    a500 = pd.read_csv(data_dir / "a500.csv", parse_dates=["date"]).set_index("date")
    a500_close = pd.DataFrame({"A500": a500["close"]})
    panel = pd.DataFrame(industries).join(a500_close, how="left").sort_index()
    return panel, names.tolist()


def replay(panel, industries, start, end):
    start_week = pd.Timestamp(start).to_period("W-FRI").end_time.normalize()
    history_start = start_week - pd.Timedelta(weeks=6) + pd.Timedelta(days=1)
    end_day = pd.Timestamp(end)
    daily = panel.loc[history_start:end_day]
    if daily.empty or daily.isna().any().any():
        raise ValueError("Missing daily closes in the replay window")
    weeks = daily.resample("W-FRI").last().dropna().loc[:end_day]
    index = weeks["A500"]
    forward = pd.concat([(index.shift(-step) / index - 1) * 100 for step in range(1, 5)], axis=1)
    results = {}
    for window in (1, 4):
        returns = weeks.pct_change(window, fill_method=None).dropna() * 100
        industry_returns = returns[industries]
        up = industry_returns.gt(0).sum(axis=1)
        median = industry_returns.median(axis=1)
        results[window] = pd.DataFrame({
            "industries_up": up,
            "industries_up_change": up.diff().astype("Int64"),
            "industry_median_return_pct": median,
            "a500_return_pct": returns["A500"],
            "a500_minus_industry_median_pp": returns["A500"] - median,
            "narrow_rally": returns["A500"].gt(0) & up.le(len(industries) // 2),
            "broad_advance_despite_index_decline": returns["A500"].lt(0) & up.gt(len(industries) // 2),
            "a500_next_4w_return_pct": forward.iloc[:, -1].reindex(up.index),
            "a500_next_4w_worst_close_pct": forward.min(axis=1, skipna=False).clip(upper=0).reindex(up.index),
        }).loc[start_week:]
    return pd.concat(results, axis=1).rename_axis("week_ending")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", help="Last date allowed in the replay (default: latest common close)")
    args = parser.parse_args()
    panel, industries = load_panel(args.data_dir)
    latest_close = panel.dropna().index.max()
    if pd.isna(latest_close):
        parser.error("No common closing prices in the input data")
    end = min(pd.Timestamp(args.end), latest_close) if args.end else latest_close
    result = replay(panel, industries, args.start, end)
    if result.empty:
        parser.error("No completed weeks in the requested replay window")
    for window in (1, 4):
        weekly = result[window]
        target = args.data_dir / f"weekly_{window}w.csv"
        weekly.to_csv(target)
        print(f"{target}: {len(weekly)} weeks, {weekly['narrow_rally'].sum()} hits")
    target = args.data_dir / "a-share-breadth-report.html"
    write_report(result, target)
    print(target)


if __name__ == "__main__":
    main()
