"""检查收益口径与周期边界，避免状态诊断引入前视和静默丢失样本。"""

import numpy as np
import pandas as pd
import pytest

from research.tools.market_regimes import analysis as regimes
from research.tools.market_regimes import read_returns


def prices():
    dates = pd.bdate_range("2019-10-01", "2020-03-06")
    frame = pd.DataFrame({"date": dates, "close": 100.0})
    frame.loc[frame["date"] == "2020-01-31", "close"] = 200
    frame.loc[frame["date"] == "2020-02-03", "close"] = 50
    return frame


def test_week_and_month_end_only_affect_later_returns():
    dates = pd.Series(pd.to_datetime(["2020-01-31", "2020-02-03", "2020-02-28", "2020-03-02"]))
    result = regimes.market_states(prices(), dates, 2, 2).set_index("date")
    assert result.loc["2020-01-31", "month_end"] == pd.Timestamp("2019-12-31")
    assert result.loc["2020-01-31", "week_end"] == pd.Timestamp("2020-01-24")
    assert result.loc["2020-02-03", "month_end"] == pd.Timestamp("2020-01-31")
    assert result.loc["2020-02-03", "week_end"] == pd.Timestamp("2020-01-31")
    # 当日指数已经跌到50，仍由已完成月/周的200收盘决定状态。
    assert result.loc["2020-02-03", "state"] == "strong_strong"
    assert result.loc["2020-02-28", "month_end"] == pd.Timestamp("2020-01-31")
    assert result.loc["2020-03-02", "month_end"] == pd.Timestamp("2020-02-29")


def test_future_closes_cannot_change_earlier_states_or_moving_averages():
    index = prices()
    dates = pd.Series(pd.bdate_range("2020-02-03", "2020-02-12"))
    expected = regimes.market_states(index, dates, 2, 2)
    index.loc[index["date"] >= "2020-02-13", "close"] = 99999
    actual = regimes.market_states(index, dates, 2, 2)
    pd.testing.assert_frame_equal(actual, expected)


def test_closed_week_does_not_create_a_synthetic_weekly_bar():
    index = prices()
    index = index[~index["date"].between("2020-02-03", "2020-02-07")]
    result = regimes.market_states(index, pd.Series(pd.to_datetime(["2020-02-10"])), 2, 2)
    assert result["week_end"].iloc[0] == pd.Timestamp("2020-01-31")
    assert result["week_ma"].iloc[0] == pytest.approx(150)


def test_percentage_cumulative_returns_use_nav_ratio_and_initial_capital(tmp_path):
    path = tmp_path / "curve.csv"
    pd.DataFrame({"时间": ["2020-02-03 16:00", "2020-02-04 16:00"],
                  "策略收益": [10, 21], "基准收益": [-10, -19]}).to_csv(path, index=False, encoding="gb18030")
    curve = read_returns(path)
    np.testing.assert_allclose(curve["strategy_return"], [.1, .1])
    np.testing.assert_allclose(curve["benchmark_return"], [-.1, -.1])
    assert regimes.compounded(curve["strategy_return"]) == pytest.approx(.21)


def test_missing_csv_trading_day_or_insufficient_warmup_is_rejected():
    curve = pd.DataFrame({"date": pd.to_datetime(["2020-02-03", "2020-02-05"]),
                          "strategy_nav": [1., 1.], "benchmark_nav": [1., 1.],
                          "strategy_return": [0., 0.], "benchmark_return": [0., 0.]})
    with pytest.raises(ValueError, match="CSV缺少1"):
        regimes.attribute(curve, prices(), 2, 2)
    with pytest.raises(ValueError, match="预热期不足"):
        regimes.market_states(prices().tail(10), curve["date"], 10, 20)


def test_group_contributions_recover_total_and_drawdowns_reset_at_each_episode():
    daily = pd.DataFrame({"date": pd.bdate_range("2020-01-01", periods=4),
                          "state": ["strong_strong", "strong_strong", "weak_weak", "weak_weak"],
                          "state_label": ["月强 / 周强"] * 2 + ["月弱 / 周弱"] * 2,
                          "strategy_return": [.1, -.1, .1, .1],
                          "benchmark_return": [0.] * 4, "index_return": [0.] * 4,
                          "episode": [1, 1, 2, 2]})
    episodes = regimes.episode_table(daily)
    summary = regimes.summarize(daily, episodes)
    assert np.expm1(summary["log_return_contribution"].sum()) == pytest.approx(regimes.compounded(daily["strategy_return"]))
    assert episodes["strategy_max_drawdown"].tolist() == pytest.approx([-.1, 0])
    assert episodes["left_truncated"].tolist() == [True, False]
    assert episodes["right_truncated"].tolist() == [False, True]
