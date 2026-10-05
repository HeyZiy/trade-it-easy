"""日收益按已知市场状态归因；纯计算，不获取行情或写报告。"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd

from .data import normalize_index, normalize_returns

STATES = {
    "strong_strong": ("月强 / 周强", "#16a34a"),
    "strong_weak": ("月强 / 周弱", "#f59e0b"),
    "weak_strong": ("月弱 / 周强", "#3b82f6"),
    "weak_weak": ("月弱 / 周弱", "#e11d48"),
}


@dataclass(frozen=True)
class RegimeConfig:
    month_window: int = 10
    week_window: int = 20
    trading_days_per_year: int = 252
    exclude_years: tuple[int, ...] = ()

    def __post_init__(self):
        for name in ("month_window", "week_window", "trading_days_per_year"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < (2 if "window" in name else 1):
                raise ValueError(f"{name}必须为有效正整数（均线窗口至少2）")


@dataclass
class RegimeAnalysis:
    daily: pd.DataFrame
    episodes: pd.DataFrame
    summary: pd.DataFrame
    year_states: pd.DataFrame

def market_states(index: pd.DataFrame, dates: pd.Series,
                  month_window: int = 10, week_window: int = 20) -> pd.DataFrame:
    if month_window < 2 or week_window < 2:
        raise ValueError("均线窗口必须至少为2")
    close = index.set_index("date")["close"]
    out = pd.DataFrame({"date": pd.to_datetime(dates)}).sort_values("date")
    # 以周期结束日为可知时点；严格禁止把本周/本月最终收盘回填到周期内。
    for prefix, rule, window in (("month", "ME", month_window), ("week", "W-FRI", week_window)):
        bars = close.resample(rule).last()
        if prefix == "week":
            # 春节/国庆可能整周休市；该周没有K线，不复制收盘计入均线窗口。
            bars = bars.dropna()
        elif bars.isna().any():
            raise ValueError(f"指数存在整{prefix}缺失，不能前向填充伪造状态")
        signal = pd.DataFrame({f"{prefix}_close": bars,
                               f"{prefix}_ma": bars.rolling(window, min_periods=window).mean()})
        signal.index.name = f"{prefix}_end"
        signal = signal.reset_index()
        out = pd.merge_asof(out, signal, left_on="date", right_on=f"{prefix}_end",
                            direction="backward", allow_exact_matches=False)
    required = ["month_close", "month_ma", "week_close", "week_ma"]
    if out[required].isna().any().any():
        raise ValueError("指数预热期不足，需在回测开始前提供完整的月线/周线均线数据")
    m = out["month_close"] >= out["month_ma"]
    w = out["week_close"] >= out["week_ma"]
    out["state"] = np.select([m & w, m & ~w, ~m & w],
                              ["strong_strong", "strong_weak", "weak_strong"], default="weak_weak")
    out["state_label"] = out["state"].map(lambda s: STATES[s][0])
    return out


def attribute(returns: pd.DataFrame, index: pd.DataFrame, month_window: int,
              week_window: int) -> pd.DataFrame:
    start, end = returns["date"].iloc[[0, -1]]
    index = index[index["date"] <= end].copy()
    period_index = index.loc[index["date"].between(start, end), "date"]
    missing = pd.DatetimeIndex(returns["date"]).difference(period_index)
    skipped = pd.DatetimeIndex(period_index).difference(returns["date"])
    if len(missing) or len(skipped):
        raise ValueError(f"日历未对齐：指数缺少{len(missing)}个CSV日期，CSV缺少{len(skipped)}个指数日期；"
                         "请刷新指数或使用覆盖完整期间的CSV，不能静默缩短样本")
    index["index_return"] = index["close"].pct_change(fill_method=None)
    out = returns.merge(market_states(index, returns["date"], month_window, week_window), on="date")
    out = out.merge(index[["date", "close", "index_return"]], on="date", validate="one_to_one")
    if out["index_return"].isna().any():
        raise ValueError("需要回测首日之前的指数收盘，才能还原首日收益")
    out["index_nav"] = (1 + out["index_return"]).cumprod()
    out["strategy_drawdown"] = out["strategy_nav"] / out["strategy_nav"].cummax().clip(lower=1) - 1
    out["episode"] = out["state"].ne(out["state"].shift()).cumsum()
    return out


def compounded(values: pd.Series) -> float:
    return float(np.expm1(np.log1p(values).sum()))


def annualized(values: pd.Series, trading_days_per_year: int = 252) -> float:
    return float(np.expm1(np.log1p(values).mean() * trading_days_per_year)) if len(values) else float("nan")


def drawdown(values: pd.Series) -> float:
    nav = np.r_[1.0, (1 + values).cumprod().to_numpy()]
    return float(np.min(nav / np.maximum.accumulate(nav) - 1))


def episode_table(daily: pd.DataFrame) -> pd.DataFrame:
    records = []
    for number, frame in daily.groupby("episode", sort=True):
        records.append({
            "episode": number, "state": frame["state"].iloc[0], "state_label": frame["state_label"].iloc[0],
            "start": frame["date"].iloc[0], "end": frame["date"].iloc[-1], "days": len(frame),
            "strategy_return": compounded(frame["strategy_return"]),
            "benchmark_return": compounded(frame["benchmark_return"]) if "benchmark_return" in frame else float("nan"),
            "index_return": compounded(frame["index_return"]),
            "relative_index_return": compounded((1 + frame["strategy_return"]) / (1 + frame["index_return"]) - 1),
            "strategy_max_drawdown": drawdown(frame["strategy_return"]),
            "left_truncated": number == daily["episode"].iloc[0],
            "right_truncated": number == daily["episode"].iloc[-1],
        })
    return pd.DataFrame(records)


def summarize(daily: pd.DataFrame, episodes: pd.DataFrame,
              config: RegimeConfig | None = None) -> pd.DataFrame:
    config = config or RegimeConfig()
    ann = lambda values: annualized(values, config.trading_days_per_year)
    records = []
    for state, (label, _) in STATES.items():
        frame = daily[daily["state"] == state]
        if frame.empty:
            continue
        ep = episodes[episodes["state"] == state]
        records.append({
            "state": state, "state_label": label, "days": len(frame), "day_share": len(frame) / len(daily),
            "episodes": len(ep), "strategy_annualized": ann(frame["strategy_return"]),
            "benchmark_annualized": ann(frame["benchmark_return"]) if "benchmark_return" in frame else float("nan"),
            "index_annualized": ann(frame["index_return"]),
            "relative_index_annualized": ann((1 + frame["strategy_return"]) / (1 + frame["index_return"]) - 1),
            "strategy_daily_mean_bps": frame["strategy_return"].mean() * 10000,
            "strategy_annual_volatility": frame["strategy_return"].std(ddof=1) * np.sqrt(config.trading_days_per_year),
            "strategy_daily_win_rate": (frame["strategy_return"] > 0).mean(),
            "episode_median_return": ep["strategy_return"].median(),
            "episode_win_rate": (ep["strategy_return"] > 0).mean(),
            "episode_worst_drawdown": ep["strategy_max_drawdown"].min(),
            "log_return_contribution": np.log1p(frame["strategy_return"]).sum(),
        })
        if config.exclude_years:
            excluded = frame[~frame["date"].dt.year.isin(config.exclude_years)]
            records[-1].update({"excluded_years_days": len(excluded),
                                "excluded_years_annualized": ann(excluded["strategy_return"])})
    return pd.DataFrame(records)


def year_state_table(daily: pd.DataFrame, trading_days_per_year: int = 252) -> pd.DataFrame:
    records = []
    for (year, state), frame in daily.groupby([daily["date"].dt.year, "state"]):
        records.append({"year": year, "state": state, "state_label": STATES[state][0], "days": len(frame),
                        "strategy_annualized": annualized(frame["strategy_return"], trading_days_per_year),
                        "relative_index_annualized": annualized((1 + frame["strategy_return"]) /
                                                               (1 + frame["index_return"]) - 1, trading_days_per_year)})
    return pd.DataFrame(records)


def analyze_returns(returns: pd.DataFrame, index: pd.DataFrame,
                    config: RegimeConfig | None = None) -> RegimeAnalysis:
    """接受日收益/净值/聚宽曲线及date,close日线，返回全部归因表，不写文件。

    有效区间须逐交易日对应，指数须包含均线预热期和曲线首日之前的收盘。
    日收益为小数；不连续状态的年化仅作条件比较，不代表实际择时收益。
    """
    config = config or RegimeConfig()
    daily = attribute(normalize_returns(returns), normalize_index(index),
                      config.month_window, config.week_window)
    episodes = episode_table(daily)
    summary = summarize(daily, episodes, config)
    if not np.isclose(summary["log_return_contribution"].sum(), np.log(daily["strategy_nav"].iloc[-1]), atol=1e-10):
        raise AssertionError("状态收益贡献无法复原原曲线总收益")
    return RegimeAnalysis(daily, episodes, summary, year_state_table(daily, config.trading_days_per_year))


