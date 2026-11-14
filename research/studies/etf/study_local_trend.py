"""ETF 轮动消融实验。仓库根目录运行 python -m research.studies.etf.study_local_trend。"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from research.studies.etf.v1_local import MomentumLocal, STUDY
from research.tools.backtest.engine import Account, metrics


@dataclass(frozen=True)
class Rules:
    name: str
    medium: bool = False
    trend: bool = False
    heat: bool = False
    correlation: bool = False
    exit_ma: bool = False
    allocation: float = 1.0
    skip_recent: bool = False
    rank_risk: bool = False
    medium_days: tuple[int, int] = (60, 120)
    vol_floor: float = 0.0


ROUND_ONE = (
    Rules("v1"),
    Rules("medium", medium=True),
    Rules("trend", trend=True),
    Rules("heat", heat=True),
    Rules("correlation", correlation=True),
    Rules("exit_ma", exit_ma=True),
    Rules("two_thirds", allocation=2 / 3),
)
ROUND_TWO = (
    Rules("medium_trend", medium=True, trend=True),
    Rules("medium_correlation", medium=True, correlation=True),
    Rules("medium_trend_correlation", medium=True, trend=True, correlation=True),
    Rules("medium_trend_exit", medium=True, trend=True, exit_ma=True),
)
ROUND_THREE = (
    Rules("v1"),
    Rules("heat", heat=True),
    Rules("heat_correlation", heat=True, correlation=True),
    Rules("medium_heat", medium=True, heat=True),
    Rules("medium_skip5", medium=True, skip_recent=True),
    Rules("medium_risk", medium=True, rank_risk=True),
)
ROUND_FOUR = (
    Rules("medium_80pct", medium=True, allocation=.80),
    Rules("medium_risk_40_80", medium=True, rank_risk=True, medium_days=(40, 80)),
    Rules("medium_risk_90_180", medium=True, rank_risk=True, medium_days=(90, 180)),
    Rules("medium_risk_floor", medium=True, rank_risk=True, vol_floor=.003),
    Rules("medium_risk", medium=True, rank_risk=True),
    Rules("medium_correlation", medium=True, correlation=True),
)
PERIODS = {
    "all": ("2016-01-01", None),
    "2016–2020": ("2016-01-01", "2020-12-31"),
    "2021–2023": ("2021-01-01", "2023-12-31"),
    "2016–2023": ("2016-01-01", "2023-12-31"),
    "2024–2026": ("2024-01-01", None),
    "2025-05–2026": ("2025-05-01", None),
}


def weighted_score(log_prices, days):
    """向量化 v1 的加权对数回归，使用与 polyfit 相同的平方权重。"""
    score = np.full(log_prices.shape, np.nan)
    x = np.arange(days + 1)
    weights = np.linspace(1, 2, days + 1) ** 2
    weights /= weights.sum()
    centered_x = x - x @ weights
    slope_weights = centered_x * weights / (centered_x ** 2 @ weights)
    windows = np.lib.stride_tricks.sliding_window_view(log_prices, days + 1, axis=0)
    for start in range(0, len(windows), 64):
        window = windows[start:start + 64]
        y = window - window[..., :1]
        mean = y @ weights
        slope = y @ slope_weights
        residual = y - mean[..., None] - slope[..., None] * centered_x
        ss_res = residual ** 2 @ weights
        ss_tot = (y - mean[..., None]) ** 2 @ weights
        r2 = np.zeros_like(slope)
        np.divide(ss_res, ss_tot, out=r2, where=ss_tot > 0)
        r2 = np.where(ss_tot > 0, np.clip(1 - r2, 0, 1), 0)
        result = np.expm1(slope * 250) * r2
        score[days + start:days + start + len(window)] = result
    return score


class TrendStudy(MomentumLocal):
    def __init__(self, cache_dir):
        super().__init__(cache_dir=cache_dir)
        close = pd.DataFrame(np.where(self.valid, self.adjusted_close, np.nan),
                             index=self.cal, columns=self.columns)
        log_price = np.log(close)
        self.short_score = weighted_score(log_price.to_numpy(), self.k.SCORE_DAYS)
        self.medium_scores = {
            (short, long): ((close.pct_change(short, fill_method=None)
                             + close.pct_change(long, fill_method=None)) / 2).to_numpy()
            for short, long in ((40, 80), (60, 120), (90, 180))
        }
        self.above_ma120 = (close > close.rolling(120).mean()).to_numpy()
        below_ma20 = close < close.rolling(20).mean()
        self.break_ma20 = (below_ma20 & below_ma20.shift(1, fill_value=False)).to_numpy()
        log_ret = log_price.diff()
        # 用急涨之前的 60 日波动，避免涨幅本身抬高入场阈值。
        prior_vol = log_ret.rolling(60).std().shift(5)
        self.hot = (log_price.diff(5) > 2.5 * np.sqrt(5) * prior_vol).to_numpy()
        self.medium_skip5 = ((close.shift(5).div(close.shift(60)) - 1
                              + close.shift(5).div(close.shift(120)) - 1) / 2).to_numpy()
        self.volatility = log_ret.rolling(60).std().to_numpy()
        self.log_ret = log_ret.to_numpy()
        self.open_price = self.data._frame("open").reindex(columns=self.columns)
        adjusted_open = self.data._frame("hfq_open").reindex(columns=self.columns)
        open_factor = adjusted_open.div(self.open_price.where(self.open_price.gt(0)))
        close_factor = close.div(self.raw_close)
        factors = close_factor.combine_first(open_factor.where(open_factor.gt(0))).ffill()
        prior_factor = factors.shift()
        opening_change = open_factor.div(prior_factor).to_numpy()
        self.open_adjustments = np.where(
            np.isfinite(opening_change) & (opening_change > 0)
            & ~np.isclose(opening_change, 1.0, rtol=0, atol=1e-9),
            opening_change, 1.0,
        )
        # 若开盘未观测到因子，持仓的剩余变化在收盘估值前入账。
        closing_change = factors.div(prior_factor).to_numpy() / self.open_adjustments
        self.close_adjustments = np.where(
            np.isfinite(closing_change) & (closing_change > 0)
            & ~np.isclose(closing_change, 1.0, rtol=0, atol=1e-9),
            closing_change, 1.0,
        )
        assert factors.index.equals(self.cal)
        self.mature = close.notna().rolling(self.k.MIN_BARS - 1).sum().shift(1).eq(
            self.k.MIN_BARS - 1).to_numpy()

    def rank(self, loc, held, scores):
        rows = []
        complete = True
        for code in self.pool:
            col = self.columns[code]
            if not self.valid[loc, col]:
                continue
            score = scores[loc, col]
            if (not np.isfinite(score)
                    or (code not in held and not self.mature[loc, col])):
                complete = False
                continue
            rows.append((code, score))
        rows.sort(key=lambda row: (-row[1], row[0]))
        return rows, complete

    def signals(self, loc, rules, held, scores, rows, rank_valid):
        ranks = {code: rank for rank, (code, _) in enumerate(rows, 1)}
        sells = []
        for code in sorted(held):
            col = self.columns[code]
            if not self.valid[loc, col]:
                continue
            rank = ranks.get(code)
            score = scores[loc, col]
            weak = score <= 0 or (
                rank_valid and rank is not None and rank > self.k.TOPN * self.k.EXIT_RANK_MULT
            )
            if weak:
                self.neg_run[code] = self.neg_run.get(code, 0) + 1
            elif rank_valid and rank is not None:
                self.neg_run[code] = 0
            if ((weak and self.neg_run[code] >= self.k.SCORE_EXIT_CONFIRM)
                    or (rules.exit_ma and self.break_ma20[loc, col])):
                sells.append(code)

        remaining = held.difference(sells)
        reserved = set(remaining)
        buys = []
        if rank_valid:
            for code, score in rows[:self.k.TOPN]:
                if len(reserved) >= self.k.TOPN:
                    break
                col = self.columns[code]
                if code in held or score <= 0:
                    continue
                if rules.trend and not self.above_ma120[loc, col]:
                    continue
                if rules.heat and self.hot[loc, col]:
                    continue
                if rules.correlation and self.correlated(loc, col, reserved):
                    continue
                reserved.add(code)
                buys.append(code)
        return sells, buys

    def correlated(self, loc, col, reserved):
        # 无法估计相关性时保留现金。候选名次仍限原始前 3。
        start = max(0, loc - 59)
        a = self.log_ret[start:loc + 1, col]
        for code in sorted(reserved):
            b = self.log_ret[start:loc + 1, self.columns[code]]
            valid = np.isfinite(a) & np.isfinite(b)
            if valid.sum() < 45:
                return True
            if np.std(a[valid]) == 0 or np.std(b[valid]) == 0:
                return True
            if np.corrcoef(a[valid], b[valid])[0, 1] >= 0.80:
                return True
        return False

    def fill(self, today, orders, prices, mark, allocation, slip):
        account = self.account
        sells, buys = orders
        for code in sells:
            px = prices[code]
            if np.isfinite(px) and px > 0:
                shares = account.closeable(code, today)
                if shares and account.sell(today, code, shares, px * (1 - slip)):
                    self.neg_run.pop(code, None)
        slots = self.k.TOPN - len(account.positions)
        if not buys or slots <= 0 or any(code in account.positions for code in sells):
            return
        value = account.value(mark)
        cash = account.cash
        free_budget = max(0.0, value * allocation - account.exposure(mark))
        per = min(cash / slots, value * allocation / self.k.TOPN)
        for code in buys:
            if slots <= 0:
                break
            px = prices[code] * (1 + slip)
            if not np.isfinite(px) or px <= 0:
                continue
            budget = min(per, free_budget, account.cash)
            shares = int(budget / (px * (1 + account.commission)) // 100) * 100
            if shares and account.buy(today, code, shares, px):
                slots -= 1
                free_budget = max(0.0, free_budget - shares * px)

    def simulate(self, rules, start, end, delay, slip):
        self.account = Account(1_000_000, commission=self.k.COMMISSION_RATE,
                               min_commission=0, slippage=0)
        self.neg_run = {}
        self.pool, pool_valid = [], False
        scores = self.medium_scores[rules.medium_days] if rules.medium else self.short_score
        if rules.skip_recent:
            scores = self.medium_skip5
        if rules.rank_risk:
            denominator = np.maximum(self.volatility, rules.vol_floor)
            scores = np.divide(scores, denominator, out=np.full_like(scores, np.nan),
                               where=denominator > 0)
        opens = self.open_price.to_numpy(dtype=float)
        closes = self.raw_close.to_numpy(dtype=float)
        first = self.cal.searchsorted(start)
        stop = self.cal.searchsorted(end, side="right")
        orders = ([], [])
        nav_rows = []
        adjustments = self.open_adjustments if delay else self.adjustments
        for loc in range(first, stop):
            today = self.cal[loc]
            for code in sorted(self.account.positions):
                multiplier = adjustments[loc, self.columns[code]]
                if multiplier != 1:
                    self.account.apply_adjustment(today, code, multiplier)
            if delay:
                opening = pd.Series(opens[loc], index=self.columns)
                prior_close = closes[max(0, loc - 1)] / self.open_adjustments[loc]
                mark = opening.where(opening.gt(0), prior_close)
                self.fill(today, orders, opening, mark, rules.allocation, slip)
                for code in sorted(self.account.positions):
                    multiplier = self.close_adjustments[loc, self.columns[code]]
                    if multiplier != 1:
                        self.account.apply_adjustment(today, code, multiplier)
            held = set(self.account.positions)
            if not pool_valid or (loc - first) % self.k.REBUILD_EVERY == 0:
                try:
                    self.pool = self.rebuild_pool(loc, held)
                    pool_valid = True
                except ValueError:
                    pool_valid = False
            rows, rank_valid = self.rank(loc, held, scores) if pool_valid else ([], False)
            orders = self.signals(loc, rules, held, scores, rows, rank_valid)
            closing = self.raw_close.iloc[loc]
            if not delay:
                self.fill(today, orders, closing, closing, rules.allocation, slip)
            value = self.account.value(closing)
            nav_rows.append((today, value, self.account.exposure(closing) / value))
        nav = pd.DataFrame(nav_rows, columns=["date", "nav", "exposure"]).set_index("date")
        nav.index = pd.DatetimeIndex(nav.index)
        ledger = pd.DataFrame(self.account.ledger)
        return nav, ledger


def summarize(nav, ledger, benchmark, name, execution, slip):
    results = []
    for period, (start, end) in PERIODS.items():
        sample = nav.loc[start:end]
        prior = nav.loc[nav.index < sample.index[0], "nav"]
        capital = prior.iloc[-1] if len(prior) else 1_000_000
        fills = ledger.loc[(ledger.date >= str(sample.index[0].date()))
                           & (ledger.date <= str(sample.index[-1].date()))
                           & ledger.side.isin(["buy", "sell"])]
        stats = metrics(sample.nav, sample.exposure, len(fills), capital)
        peak = sample.nav.cummax().clip(lower=capital)
        trough = (sample.nav / peak - 1).idxmin()
        benchmark_slice = benchmark.loc[sample.index]
        benchmark_prior = benchmark.loc[benchmark.index < sample.index[0]]
        benchmark_capital = benchmark_prior.iloc[-1] if len(benchmark_prior) else benchmark_slice.iloc[0]
        benchmark_stats = metrics(benchmark_slice, sample.exposure, 0, benchmark_capital)
        prior_nav = sample.nav.shift(1, fill_value=capital)
        turnover = ((fills.price * fills.quantity).to_numpy()
                    / prior_nav.reindex(pd.to_datetime(fills.date)).to_numpy()).sum()
        results.append(dict(
            strategy=name, execution=execution, slippage_bps=slip * 10_000,
            period=period, days=len(sample), annual_return=stats["年化收益%"],
            max_drawdown=stats["最大回撤%"], sharpe=stats["Sharpe"], calmar=stats["Calmar"],
            exposure=stats["平均仓位%"], fills=len(fills), annual_turnover=turnover * 244 / len(sample),
            trough=str(trough.date()), benchmark_return=benchmark_stats["年化收益%"],
            benchmark_drawdown=benchmark_stats["最大回撤%"],
        ))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir")
    parser.add_argument("--out", type=Path, default=STUDY / "results" / "local_trend_verified")
    parser.add_argument("--round", choices=["1", "2", "3", "4", "all"], default="all")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    study = TrendStudy(args.cache_dir)
    benchmark = study.data.index_close()
    benchmark.index = pd.DatetimeIndex(benchmark.index)
    benchmark = benchmark.loc["2016-01-01":]
    rounds = {"1": ROUND_ONE, "2": ROUND_TWO, "3": ROUND_THREE, "4": ROUND_FOUR}
    for stage in rounds if args.round == "all" else [args.round]:
        records = []
        for rules in rounds[stage]:
            executions = [("close", False, 0), ("next_open", True, .001)]
            if stage == "3" and rules.name in {"v1", "heat"}:
                executions = [("close_10bp", False, .001), ("next_open_0bp", True, 0)]
            if stage == "4":
                executions = ([("next_open_20bp", True, .002)]
                              if rules.name in {"medium_risk", "medium_correlation"}
                              else [("next_open", True, .001)])
            for execution, delay, slip in executions:
                nav, ledger = study.simulate(rules, "2016-01-01", study.cal[-1], delay, slip)
                if rules.name == "v1" and execution == "close" and args.cache_dir is None:
                    baseline = STUDY / "results" / "v1_local" / "nav.csv"
                    if baseline.exists():
                        reference = pd.read_csv(baseline, index_col=0, parse_dates=True)
                        pd.testing.assert_frame_equal(nav, reference, check_names=False)
                label = rules.name + "_" + execution
                nav.to_csv(args.out / (label + "_nav.csv"), index_label="date")
                ledger.to_csv(args.out / (label + "_ledger.csv"), index=False)
                records.extend(summarize(nav, ledger, benchmark, rules.name, execution, slip))
                row = next(row for row in records if row["strategy"] == rules.name
                           and row["execution"] == execution and row["period"] == "all")
                print(label, "CAGR %.2f | MDD %.2f | Sharpe %.3f" %
                      (row["annual_return"], row["max_drawdown"], row["sharpe"]), flush=True)
        pd.DataFrame(records).to_csv(args.out / ("round_" + stage + ".csv"), index=False,
                                     encoding="utf-8-sig")


if __name__ == "__main__":
    main()
