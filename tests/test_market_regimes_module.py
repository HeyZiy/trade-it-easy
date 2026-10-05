"""覆盖跨研究输入、可选基准、共享行情和离线完整工作流。"""

import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.tools.market_regimes import CurveSpec, RegimeConfig, analyze_returns, normalize_returns, read_returns
from research.tools.market_regimes import data
from research.tools.market_regimes.cli import main


@pytest.fixture
def index():
    dates = pd.bdate_range("2019-01-01", "2021-02-10")
    return pd.DataFrame({"date": dates, "close": 100 + 5 * np.sin(np.arange(len(dates)) / 10)})


@pytest.mark.parametrize("kind,values,columns,initial", [
    ("cumulative-percent", [10, 21, 8.9], ["时间", "策略收益"], 1),
    ("cumulative-return", [.1, .21, .089], ["date", "strategy_cumulative"], 1),
    ("nav", [110000, 121000, 108900], ["date", "strategy_nav"], 100000),
    ("daily-return", [.1, .1, -.1], ["date", "strategy_return"], 1),
])
def test_four_input_formats_produce_identical_attribution(index, kind, values, columns, initial):
    raw = pd.DataFrame({columns[0]: pd.bdate_range("2020-02-03", periods=3), columns[1]: values})
    curve = normalize_returns(raw, CurveSpec(kind=kind, initial_nav=initial))
    result = analyze_returns(curve, index)
    np.testing.assert_allclose(result.daily["strategy_return"], [.1, .1, -.1])
    assert result.daily["strategy_nav"].iloc[-1] == pytest.approx(1.089)
    assert result.summary["benchmark_annualized"].isna().all()
    assert "benchmark_nav" not in result.daily
    assert "excluded_years_annualized" not in result.summary


def test_auto_never_guesses_numeric_units_and_positions_handle_broken_headers(tmp_path):
    raw = pd.DataFrame({"broken date": ["2020-02-03", "2020-02-04"],
                        "broken benchmark": [-10, -19], "broken strategy": [10, 21]})
    path = tmp_path / "curve.csv"
    raw.to_csv(path, index=False, encoding="utf-8-sig")
    with pytest.raises(ValueError, match="无法识别"):
        read_returns(path)
    curve = read_returns(path, CurveSpec("cumulative-percent", ("#0", "#2", "#1")))
    np.testing.assert_allclose(curve["strategy_return"], [.1, .1])
    np.testing.assert_allclose(curve["benchmark_return"], [-.1, -.1])
    with pytest.raises(ValueError, match="指定format"):
        CurveSpec(columns=("#0", "#2"))


def test_year_exclusion_and_annualization_are_explicit_configuration(index):
    curve = pd.DataFrame({"date": pd.bdate_range("2020-12-28", "2021-01-06"), "strategy_return": .001})
    plain = analyze_returns(curve, index)
    configured = analyze_returns(curve, index, RegimeConfig(trading_days_per_year=200, exclude_years=(2021,)))
    assert "excluded_years_days" not in plain.summary
    assert configured.summary["excluded_years_days"].sum() == 4
    np.testing.assert_allclose(configured.summary["strategy_annualized"], 1.001 ** 200 - 1)
    assert configured.daily["state"].tolist() == plain.daily["state"].tolist()


def test_offline_cli_writes_generic_report_without_inventing_benchmark(tmp_path, index):
    curve_path, index_path, out = tmp_path / "test.csv", tmp_path / "index.csv", tmp_path / "report"
    pd.DataFrame({"date": pd.bdate_range("2020-02-03", periods=5), "strategy_nav": [1.1] * 5}).to_csv(curve_path, index=False)
    index.to_csv(index_path, index=False)
    main(["--results", str(curve_path), "--index-csv", str(index_path), "--index-name", "测试指数",
          "--month-window", "6", "--week-window", "12", "--out", str(out)])
    text = (out / "report.html").read_text(encoding="utf-8")
    assert "测试指数" in text and "6月均线 + 12周均线" in text
    assert "ROE" not in text and "沪深300" not in text and "中证800" not in text
    assert '"benchmark_nav":' not in text and "__DATA__" not in text
    assert (out / "test/year_states.csv").exists()
    summary = pd.read_csv(out / "test/summary.csv")
    assert "excluded_years_annualized" not in summary


def test_index_cache_is_shared_per_symbol_and_rejects_wrong_identity(tmp_path, monkeypatch):
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        return pd.DataFrame({"date": ["2020-01-02", "2020-01-03"], "close": [100, 101]})

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(stock_zh_index_daily=fetch))
    monkeypatch.setattr(data, "HERE", tmp_path)
    data.fetch_index("sh000906")
    _, source = data.fetch_index("sh000906")
    data.fetch_index("sh000300")
    assert source["cache_hit"]
    assert calls == ["sh000906", "sh000300"]
    assert (tmp_path / "_cache/sh000906.csv").exists()
    assert (tmp_path / "_cache/sh000300.csv").exists()
    with pytest.raises(ValueError, match="代码与请求不符"):
        data.fetch_index("sh000300", cache=tmp_path / "_cache/sh000906.csv")


def test_duplicate_dates_and_invalid_daily_return_are_not_silently_fixed():
    with pytest.raises(ValueError, match="重复"):
        normalize_returns(pd.DataFrame({"date": ["2020-01-02"] * 2, "strategy_return": [0, .1]}))
    with pytest.raises(ValueError, match="日收益"):
        normalize_returns(pd.DataFrame({"date": ["2020-01-02"], "strategy_return": [-1]}))


def test_network_python_dates_and_csv_timestamps_align_despite_datetime_precision(index):
    index = index.copy()
    index["date"] = index["date"].dt.date
    curve = pd.DataFrame({"date": pd.bdate_range("2020-02-03", periods=3).astype("datetime64[us]"),
                          "strategy_return": [.01, -.01, .02]})
    result = analyze_returns(curve, index)
    assert len(result.daily) == 3
    assert result.daily["date"].dtype == np.dtype("datetime64[ns]")
