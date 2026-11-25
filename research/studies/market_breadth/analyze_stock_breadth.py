"""Compare forward CSI 300 outcomes across weekly breadth observations."""

import argparse
from pathlib import Path

import pandas as pd

if __package__:
    from .replay import DEFAULT_DATA, load_panel
else:
    from replay import DEFAULT_DATA, load_panel


def prepare(path, industry_data):
    data = pd.read_csv(path, parse_dates=["week_ending", "trade_date", "reference_date"])
    data = data.sort_values("trade_date").set_index("trade_date", verify_integrity=True)
    if data.empty:
        raise ValueError("The JoinQuant export contains no observations")

    close = data.hs300_close
    data["index_4w_pct"] = (close / data.reference_date.map(close) - 1) * 100
    week_aligned = data.reference_date.eq(data.index.to_series().shift(4))
    # A missing observation must not silently lengthen the 4-week horizon.
    data["index_1w_pct"] = (close.pct_change(fill_method=None) * 100).where(week_aligned)
    forward = pd.concat([(close.shift(-step) / close - 1) * 100 for step in range(1, 5)], axis=1)
    complete = week_aligned.shift(-4, fill_value=False)
    data["next_4w_pct"] = forward.iloc[:, -1].where(complete)
    data["worst_close_pct"] = forward.min(axis=1, skipna=False).clip(upper=0).where(complete)
    data["stock_narrow"] = data.up_ratio.le(0.5)

    if industry_data is not None:
        panel, industries = load_panel(industry_data)
        now = panel[industries].reindex(data.index)
        before = panel[industries].reindex(data.reference_date).set_axis(data.index)
        valid = now.notna().all(axis=1) & before.notna().all(axis=1)
        data["industries_up"] = now.gt(before).sum(axis=1).where(valid)
    return data


def summarize(data):
    rows = []

    def add(period, context, breadth, part):
        outcome = part.next_4w_pct
        rows.append({
            "period": period, "index_this_week": context, "breadth": breadth,
            "weeks": len(part), "down_weeks": int(outcome.lt(0).sum()),
            "mean_next_4w_pct": outcome.mean(), "mean_worst_close_pct": part.worst_close_pct.mean(),
        })

    periods = {
        "all": data, "2010–2014": data.loc[:"2014"],
        "2015–2019": data.loc["2015":"2019"], "2020–2024": data.loc["2020":"2024"],
        "2025+": data.loc["2025":],
    }
    for period, sample in periods.items():
        directions = {
            "all": sample, "up": sample[sample.index_1w_pct.gt(0)],
            "flat/down": sample[sample.index_1w_pct.le(0)],
        }
        for direction, part in directions.items():
            add(period, direction, "stock <= 50%", part[part.stock_narrow])
            add(period, direction, "stock > 50%", part[~part.stock_narrow])
    if "industries_up" in data:
        overlap = data.dropna(subset=["industries_up"])
        industries_narrow = overlap.industries_up.le(15)
        for stock_narrow in (True, False):
            for industry_narrow in (True, False):
                part = overlap[overlap.stock_narrow.eq(stock_narrow) & industries_narrow.eq(industry_narrow)]
                label = "stocks %s, industries %s" % (
                    "<= 50%" if stock_narrow else "> 50%", "<= 15" if industry_narrow else "> 15",
                )
                add("industry overlap", "all", label, part)
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="Weekly CSV exported from JoinQuant")
    parser.add_argument("--min-coverage", type=float, default=0.90)
    parser.add_argument("--industry-data", type=Path, default=DEFAULT_DATA if DEFAULT_DATA.exists() else None)
    args = parser.parse_args()
    if not 0 < args.min_coverage <= 1:
        parser.error("--min-coverage must be greater than 0 and at most 1")

    data = prepare(args.csv, args.industry_data)
    covered = data.coverage.ge(args.min_coverage) & data.up_ratio.notna()
    eligible = covered & data.index_4w_pct.gt(0) & data.next_4w_pct.notna()
    print("Weeks: %s | coverage pass: %s | eligible for forward comparison: %s" %
          (len(data), int(covered.sum()), int(eligible.sum())))
    hits = data[eligible & data.stock_narrow]
    positions = [i for i, hit in enumerate(eligible & data.stock_narrow) if hit]
    episodes = sum(i == 0 or position - positions[i - 1] >= 4 for i, position in enumerate(positions))
    print("Narrow weeks: %s | episodes grouped by overlapping forward windows: %s" % (len(hits), episodes))
    summary = summarize(data[eligible])
    print(summary[summary.weeks.gt(0)].round(2).to_string(index=False))
    out = args.csv.with_name(args.csv.stem + "_analysis.csv")
    data.assign(coverage_pass=covered, eligible=eligible).to_csv(out)
    print(out)


if __name__ == "__main__":
    main()
