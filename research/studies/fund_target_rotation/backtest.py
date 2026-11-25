"""Weekly mutual-fund subscriptions and target-profit rotation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from random import Random
from time import sleep

import numpy as np
import pandas as pd

from data_provider.fund_nav import _history


FUND_SHARES = {
    "166001": "005787",
    "090007": "018225",
    "006567": "012940",
    "163406": "023875",
    "163417": "005491",
    "014412": "014413",
    "720001": "021523",
    "020884": "020885",
    "110011": "110011",
    "015102": "015103",
}
QDII = "110011"
DATA = Path("research/data/fund_target_rotation")
INITIAL_CASH = 10_000
LOOKBACK = 60
TARGET = 0.10
# 110011 annual closure notices from the manager, 2024–2026.
QDII_CLOSED = {
    "2024-03-29", "2024-04-01", "2024-05-15", "2024-07-01", "2024-09-18",
    "2024-10-11", "2024-12-24", "2024-12-25", "2024-12-26", "2024-12-31",
    "2025-04-18", "2025-04-21", "2025-07-01", "2025-10-29",
    "2025-12-24", "2025-12-25", "2025-12-26", "2025-12-31",
    "2026-04-03", "2026-04-07", "2026-05-25", "2026-07-01", "2026-10-19",
    "2026-12-24", "2026-12-25", "2026-12-31",
}


def get_history(code, start, end):
    rows = []
    page = 1
    while True:
        for attempt in range(3):
            try:
                payload = _history(code, startDate=start, endDate=end, pageIndex=page, pageSize=100)
                break
            except (OSError, ValueError):
                if attempt == 2:
                    raise
                sleep(1 + attempt)
        batch = (payload["Data"] or {}).get("LSJZList") or []
        if not batch:
            break
        rows.extend(batch)
        if len(rows) >= payload["TotalCount"]:
            break
        page += 1
    if not rows:
        raise ValueError(f"{code}: no daily NAV records")

    history = pd.DataFrame(rows).rename(columns={
        "FSRQ": "date", "DWJZ": "nav", "JZZZL": "growth",
        "SGZT": "subscription", "SHZT": "redemption",
    })[["date", "nav", "growth", "subscription", "redemption"]]
    history = history.drop_duplicates("date").sort_values("date")
    history["nav"] = pd.to_numeric(history["nav"], errors="coerce")
    history["growth"] = pd.to_numeric(history["growth"], errors="coerce")
    # Some funds disclose weekly indicative marks before opening daily transactions.
    history = history.loc[history["nav"].gt(0)].copy()
    if history.empty:
        raise ValueError(f"{code}: no valid unit NAV")
    missing = history["growth"].isna()
    if missing.iloc[1:].any():
        raise ValueError(f"{code}: missing disclosed NAV growth after the first valid date")
    history["code"] = code
    return history


def load_history(path, start, end):
    codes = sorted(set(FUND_SHARES) | set(FUND_SHARES.values()))
    if path.exists():
        history = pd.read_csv(path, dtype={"code": str})
    else:
        def fetch(code):
            try:
                return get_history(code, start, end)
            except (OSError, ValueError) as error:
                raise RuntimeError(f"Could not download {code}: {error}") from error

        with ThreadPoolExecutor(max_workers=4) as pool:
            history = pd.concat(pool.map(fetch, codes), ignore_index=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        history.to_csv(path, index=False)
    if missing := set(codes) - set(history["code"]):
        raise ValueError(f"Missing codes in {path}: {', '.join(sorted(missing))}")
    history["date"] = pd.to_datetime(history["date"])
    return history.loc[history["date"].between(start, end)]


def load_nav_only(folder, start, end, actual_only=False):
    codes = set(FUND_SHARES.values())
    if not actual_only:
        codes.update(FUND_SHARES)
    history = pd.concat(
        [pd.read_csv(folder / f"{code}.csv", dtype={"code": str}) for code in sorted(codes)],
        ignore_index=True,
    )
    history = history.loc[history["date"].between(start, end) & history["nav"].gt(0)].copy()
    bad_growth = history["growth"].isna() | history["growth"].le(-100)
    if bad_growth.any():
        invalid = history.loc[bad_growth, ["code", "date"]].to_dict("records")
        raise ValueError(f"Invalid daily returns: {invalid[:10]}")
    closed = history["code"].eq(QDII) & history["date"].isin(QDII_CLOSED)
    history["subscription"] = np.where(closed, "暂停申购", "开放申购")
    history["redemption"] = np.where(closed, "暂停赎回", "开放赎回")
    history["date"] = pd.to_datetime(history["date"])
    return history


@dataclass
class Market:
    days: pd.DatetimeIndex
    codes: tuple
    nav: np.ndarray
    ranks: np.ndarray
    buy: np.ndarray
    sell: np.ndarray
    weekly: np.ndarray
    qdii: int


def prepare_market(history, codes):
    selected = history.loc[history["code"].isin(codes)].copy().sort_values(["code", "date"])
    selected["adjusted"] = (1 + selected["growth"].fillna(0) / 100).groupby(selected["code"]).cumprod()

    def panel(column):
        return selected.pivot(index="date", columns="code", values=column).reindex(columns=codes)

    raw = panel("adjusted")
    # Keep domestic NAV dates as the subscription and settlement calendar.
    dates = raw.drop(columns=QDII).dropna(how="all").index
    nav = raw.reindex(dates).ffill()
    rank = nav.shift(2).div(nav.shift(LOOKBACK + 2)).sub(1)
    # Each fund must have published a fresh NAV on the common ranking date.
    rank = rank.where(raw.reindex(dates).shift(2).notna())
    buy = panel("subscription").reindex(dates).apply(
        lambda col: col.str.contains("开放申购|限大额|限制大额|暂停大额", na=False)
    )
    sell = panel("redemption").reindex(dates).apply(
        lambda col: col.str.contains("开放赎回|限大额|限制大额", na=False)
    )
    if not buy.any().all() or not sell.any().all():
        raise ValueError("Historical subscription or redemption flags missing for at least one fund")
    week = dates.to_period("W-SUN")
    return Market(dates, codes, nav.to_numpy(), rank.to_numpy(),
                  buy.to_numpy(), sell.to_numpy(), ~week.duplicated(), codes.index(QDII))


def redemption_fee(value, days, fee_case):
    if days < 7:
        return value * 0.015
    if days < 30:
        return value * (0.01 if fee_case == "current_floor" else 0.005)
    if days < 180 and fee_case == "current_floor":
        return value * 0.005
    return 0.0


def simulate(market, start, rule, *, budget, fee_case, seed=0):
    rng = Random(seed)
    cash = INITIAL_CASH
    lots = [[] for _ in market.codes]
    pending = 0.0
    cash_release = 0
    active = None
    cycle_start = None
    exiting = False
    curve = []
    cycles = []

    for day in range(start, len(market.days)):
        date = market.days[day]
        if pending and day >= cash_release:
            cash += pending
            pending = 0.0

        if active is not None:
            domestic = active != market.qdii
            lag = 1 if domestic else 2
            principal = sum(lot[2] for lot in lots[active])
            if principal and lots[active][-1][0] <= day - lag:
                proceeds = sum(
                    units * market.nav[day - lag, active] -
                    redemption_fee(units * market.nav[day - lag, active],
                                   (date - market.days[bought]).days, fee_case)
                    for bought, units, _ in lots[active]
                )
                exiting |= proceeds >= principal * (1 + TARGET)

            if exiting and market.sell[day, active]:
                cost = sum(lot[2] for lot in lots[active])
                pending = sum(
                    units * market.nav[day, active] -
                    redemption_fee(units * market.nav[day, active],
                                   (date - market.days[bought]).days, fee_case)
                    for bought, units, _ in lots[active]
                )
                cash_release = day + (2 if domestic else 7)
                cycles.append((market.codes[active], market.days[cycle_start], date, cost, pending, pending / cost - 1))
                lots[active].clear()
                active, cycle_start, exiting = None, None, False

        if market.weekly[day] and cash > 1e-8 and not exiting and not pending:
            pool = np.flatnonzero(market.buy[day] & np.isfinite(market.ranks[day]))
            if rule == "equal":
                choices = pool
            else:
                if active is None and len(pool):
                    active = min(pool, key=lambda idx: market.ranks[day, idx]) if rule == "worst" else rng.choice(pool)
                    cycle_start = day
                choices = [active] if active is not None and market.buy[day, active] else []

            if len(choices):
                allocation = min(budget, cash) / len(choices)
                for fund in choices:
                    # A shares in the proxy panel, and QDII in both panels, pay 0.15%.
                    subscription_fee = 0.0015 if market.codes[fund] in FUND_SHARES else 0.0
                    units = allocation / (1 + subscription_fee) / market.nav[day, fund]
                    lots[fund].append((day, units, allocation))
                    cash -= allocation

        holdings = sum(
            sum(units for _, units, _ in entries) * market.nav[day, fund]
            for fund, entries in enumerate(lots) if entries
        )
        curve.append((date, cash + pending + holdings, holdings))

    account = pd.DataFrame(curve, columns=["date", "equity", "holdings"]).set_index("date")
    trades = pd.DataFrame(cycles, columns=["code", "buy_date", "sell_date", "cost", "proceeds", "return"])
    waiting = (market.days[-1] - market.days[cycle_start]).days if cycle_start is not None else 0
    longest = max(waiting, *[(sell - buy).days for _, buy, sell, *_ in cycles]) if cycles else waiting
    open_cost = sum(lot[2] for lot in lots[active]) if active is not None else 0
    metrics = {
        "return": account["equity"].iloc[-1] / INITIAL_CASH - 1,
        "max_drawdown": (account["equity"] / account["equity"].cummax().clip(lower=INITIAL_CASH) - 1).min(),
        "trades": len(cycles), "longest_cycle_days": longest, "current_cycle_days": waiting,
        "current_code": market.codes[active] if active is not None else "",
        "current_cost": open_cost,
        "current_return": account["holdings"].iloc[-1] / open_cost - 1 if open_cost else np.nan,
        "average_exposure": (account["holdings"] / account["equity"]).mean(),
    }
    return metrics, account, trades


def run_study(history, end, output, random_runs, actual_only=False):
    summary = []
    panels = [("actual", tuple(FUND_SHARES.values()))]
    if not actual_only:
        panels.append(("a_proxy", tuple(FUND_SHARES)))
    for panel, codes in panels:
        market = prepare_market(history, codes)
        complete = np.flatnonzero(np.isfinite(market.ranks).all(axis=1) & market.weekly)
        if not len(complete):
            raise ValueError(f"{panel}: no weekly date with all {len(codes)} funds ranked")
        print(f"{panel}: {market.days[complete[0]].date()} to {market.days[-1].date()}", flush=True)
        if (pd.Timestamp(end) - market.days[-1]).days > 7:
            raise ValueError(f"{panel}: NAV cache ends more than a week before requested end date")
        variants = [
            ("main", complete[0], 500, "current_floor"),
            ("half_budget", complete[0], 250, "current_floor"),
            ("lower_fee", complete[0], 500, "low_fee"),
        ]
        if panel == "actual":
            actual_start = market.days[complete[0]]
        elif len(overlap := complete[market.days[complete] >= actual_start]):
            variants.append(("actual_window", overlap[0], 500, "current_floor"))
        for offset in (4, 8, 12):
            if len(complete) > offset and complete[offset] < len(market.days) - 60:
                variants.append((f"start_{offset}", complete[offset], 500, "current_floor"))

        variants.append(("no_qdii", complete[0], 500, "current_floor"))
        buy_without_qdii = market.buy.copy()
        buy_without_qdii[:, market.qdii] = False
        domestic_market = replace(market, buy=buy_without_qdii)
        for variant, start, budget, fee_case in variants:
            random_curves = []
            for rule, runs in (("worst", 1), ("equal", 1), ("random", random_runs)):
                for seed in range(runs):
                    metrics, account, trades = simulate(
                        domestic_market if variant == "no_qdii" else market,
                        start, rule, budget=budget, fee_case=fee_case, seed=seed
                    )
                    summary.append({"panel": panel, "variant": variant, "rule": rule,
                                    "start": market.days[start].date(), "seed": seed, **metrics})
                    if variant == "main" and rule == "random":
                        random_curves.append(account["equity"])
                    elif variant == "main":
                        account.to_csv(output / f"{panel}_{rule}_curve.csv")
                        trades.to_csv(output / f"{panel}_{rule}_trades.csv", index=False)
            if random_curves:
                equity = pd.concat(random_curves, axis=1)
                equity.quantile([0.1, 0.5, 0.9], axis=1).T.to_csv(output / f"{panel}_random_curve.csv")

    pd.DataFrame(summary).to_csv(output / "summary.csv", index=False)
    main = pd.DataFrame(summary).query("variant == 'main'")
    stats = main.groupby(["panel", "rule"])[
        ["return", "max_drawdown", "trades", "longest_cycle_days", "average_exposure"]
    ].median()
    print(stats.to_string())
    for panel, block in main.groupby("panel"):
        target = block.loc[block["rule"].eq("worst"), "return"].iloc[0]
        random = block.loc[block["rule"].eq("random"), "return"]
        print(f"{panel}: worst beat {100 * random.lt(target).mean():.1f}% of random runs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-09-01", help="Earliest NAV date, includes ranking warm-up")
    parser.add_argument("--end", default="2026-11-23")
    parser.add_argument("--input", type=Path, help="Read a NAV CSV if present, otherwise download it")
    parser.add_argument("--nav-dir", type=Path, help="Use per-fund NAV CSVs and estimate trade availability")
    parser.add_argument("--actual-only", action="store_true", help="Skip the A-share proxy panel")
    parser.add_argument("--output", type=Path, default=DATA)
    parser.add_argument("--random-runs", type=int, default=200)
    args = parser.parse_args()
    if args.random_runs < 1:
        parser.error("--random-runs must be at least one")
    if args.nav_dir and args.input:
        parser.error("--input and --nav-dir are mutually exclusive")
    if args.nav_dir:
        history = load_nav_only(args.nav_dir, args.start, args.end, args.actual_only)
    else:
        path = args.input or args.output / f"nav_{args.start}_{args.end}.csv"
        history = load_history(path, args.start, args.end)
    args.output.mkdir(parents=True, exist_ok=True)
    run_study(history, args.end, args.output, args.random_runs, args.actual_only)


if __name__ == "__main__":
    main()
