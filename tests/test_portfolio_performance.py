"""模拟成绩的真实流水闭环：日期隔离、估值缺口、冻结基准、只读与执行归因。"""

from datetime import date, datetime, timedelta, timezone
import json

import pandas as pd
import pytest

from src.trade_ledger.ledger import TradeRecord, append_trade, derive, load_trades
from src.trade_ledger.performance import (
    _fingerprint, collect_close, execution_summary, performance_path,
    render_summary, valid_records,
)


def calendar(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    append_trade(TradeRecord("2026-09-22", "600001", "测试股", "buy", 100, 10), path)
    return path


def collect(path, day="2026-09-23", prices=None, **kwargs):
    return collect_close(path, day, {"name": "中证800", "index": "000906"},
                         close_prices=lambda codes, asof: prices if prices is not None else {c: 11.0 for c in codes},
                         index_prices=lambda codes, asof: {c: 100 if asof == "2026-09-21" else 102 for c in codes},
                         trading_dates=calendar, **kwargs)


def test_historical_date_excludes_future_fills(ledger):
    append_trade(TradeRecord("2026-09-24", "600001", "测试股", "sell", 100, 12), ledger)
    row = collect(ledger)["records"][-1]
    assert row["cash"] == 999000 and row["equity"] == 1000100
    assert row["trading_days"] == 2 and row["holding_count"] == 1
    assert derive(load_trades(ledger), "2026-09-21").positions == []
    assert "超额收益 -1.99 个百分点" in render_summary(ledger, today=date(2026, 9, 23))


def test_realized_and_floating_profit_are_both_in_nav(ledger):
    append_trade(TradeRecord("2026-09-23", "600001", "测试股", "sell", 100, 12), ledger)
    row = collect(ledger, prices={})["records"][-1]
    assert row["nav"] == pytest.approx(1.0002)
    assert row["holding_count"] == 0 and row["invested"] == 0
    assert "现金 100.0%" in render_summary(ledger, today=date(2026, 9, 23))


def test_valid_records_filters_by_date_fingerprint_and_positive_nav(ledger):
    trades = load_trades(ledger)
    good = {"date": "2026-09-22", "nav": 1.0,
            "fingerprint": _fingerprint(trades, "2026-09-22"), "issues": []}
    stale = dict(good, date="2026-09-23", fingerprint="bad")   # 流水已变更
    empty = dict(good, date="2026-09-23", nav=None)            # 估值不完整
    future = dict(good, date="2026-09-30")                     # 未到日期
    rows, valid = valid_records(trades, [good, stale, empty, future], "2026-09-23")
    assert [r["date"] for r in rows] == ["2026-09-22", "2026-09-23", "2026-09-23"]
    assert valid == [good]


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_missing_or_invalid_quote_never_becomes_loss(ledger, price):
    collect(ledger)
    state = collect(ledger, "2026-09-24", prices={"600001": price})
    assert state["records"][-1]["nav"] is None
    assert "positions" not in state["records"][-1]
    text = render_summary(ledger, today=date(2026, 9, 24))
    assert "收盘截至 2026-09-23" in text
    assert "估值不完整" in text and "600001" in text
    assert "-100.00%" not in text


def test_valid_close_snapshot_keeps_each_position(ledger):
    (position,) = collect(ledger, prices={"600001": 11.0})["records"][-1]["positions"]
    assert position == {"code": "600001", "name": "测试股", "count": 100,
                        "cost_price": 10.0, "close": 11.0,
                        "market_value": 1100.0, "pos_pct": 0.11}


def test_missing_benchmark_keeps_valid_account_return(ledger):
    state = collect_close(ledger, "2026-09-23", {"name": "基准", "index": "000906"},
                          close_prices=lambda codes, asof: {c: 11 for c in codes},
                          index_prices=lambda codes, asof: {}, trading_dates=calendar)
    row = state["records"][-1]
    assert row["nav"] == pytest.approx(1.0001) and row["benchmark_nav"] is None
    text = render_summary(ledger, today=date(2026, 9, 23))
    assert "累计收益 +0.01%" in text and "同期基准 —｜超额收益 —" in text


def test_etf_baseline_is_frozen_and_cash_does_not_earn(ledger):
    def prices(codes, asof):
        return {c: {"2026-09-21": 10, "2026-09-23": 11, "2026-09-24": 12}[asof] for c in codes}
    def run(day, weight):
        return collect_close(ledger, day, {"name": "ETF", "weights": {"510300": weight, "CASH": 1-weight}},
                             close_prices=prices, index_prices=None, trading_dates=calendar)
    assert run("2026-09-23", 0.5)["records"][-1]["benchmark_nav"] == pytest.approx(1.05)
    state = run("2026-09-24", 0.1)
    assert state["records"][-1]["benchmark_nav"] == pytest.approx(1.1)
    assert state["benchmark"]["weights"]["510300"] == 0.5


def test_retries_replace_same_date_and_dry_run_writes_nothing(ledger):
    before = ledger.read_bytes()
    collect(ledger, dry_run=True)
    assert not performance_path(ledger).exists() and ledger.read_bytes() == before
    collect(ledger, prices={})
    collect(ledger)
    saved = performance_path(ledger).read_bytes()
    collect(ledger, "2026-09-24", dry_run=True)
    assert performance_path(ledger).read_bytes() == saved
    assert len(json.loads(saved)["records"]) == 1
    assert ledger.read_bytes() == before


def test_backdated_change_invalidates_saved_valuation(ledger):
    collect(ledger)
    append_trade(TradeRecord("2026-09-23", "600001", "测试股", "buy", 100, 10), ledger)
    text = render_summary(ledger, today=date(2026, 9, 23))
    assert "历史估值已失效" in text and "累计收益 +0.01%" not in text


def test_new_start_is_not_silently_spliced_into_old_curve(ledger):
    collect(ledger)
    append_trade(TradeRecord("2026-09-21", "600002", "测试股2", "buy", 100, 10), ledger)
    with pytest.raises(ValueError, match="模拟起点已变化"):
        collect(ledger)
    assert "模拟起点已变化" in render_summary(ledger, today=date(2026, 9, 23))


def test_empty_account_is_not_an_invented_zero_return(tmp_path):
    path = tmp_path / "empty.jsonl"
    assert collect(path) == {} and not performance_path(path).exists()
    assert "尚未开始模拟记账" in render_summary(path)


def test_execution_counts_only_recorded_fills(ledger):
    before = load_trades(ledger)
    append_trade(TradeRecord("2026-09-23", "600001", "测试股", "sell", 100, 11), ledger)
    text = execution_summary(before, load_trades(ledger), planned=2)
    assert "已记账 1 笔（买入 0，卖出 1）；未完成 1 笔" in text
    assert "未记账" in execution_summary(before, before, dry_run=True, planned=2)


def test_raw_close_feed_rejects_stale_future_and_duplicate_rows(monkeypatch):
    from src.trade_ledger.closing_prices import ClosingPrices
    frames = {
        "510300": pd.DataFrame({"date": ["2026-09-22"], "close": [10]}),
        "510500": pd.DataFrame({"date": ["2026-09-24"], "close": [10]}),
        "510050": pd.DataFrame({"date": ["2026-09-23"] * 2, "close": [10, 11]}),
        "512880": pd.DataFrame({"date": ["2026-09-23"], "close": [12]}),
    }
    seen = []
    def fetch(code):
        seen.append(code)
        return frames[code]
    def em_unavailable(code, end_date):
        raise ValueError("东财行情为空")
    def ad_unavailable(codes, begin_date, end_date):
        raise TimeoutError("AD 行情不可用")
    monkeypatch.setattr("data_provider.fetchers.amazingdata.market.fetch_kline", ad_unavailable)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", em_unavailable)
    monkeypatch.setattr("data_provider.bars.get_etf_daily", fetch)
    feed = ClosingPrices()
    assert feed.close_prices(list(frames), "2026-09-23") == {"512880": 12}
    feed.close_prices(list(frames), "2026-09-23")
    assert seen == list(frames)


def test_raw_close_feed_batches_amazingdata_and_uses_etf_fallbacks_for_gaps(monkeypatch):
    import akshare as ak
    from src.trade_ledger.closing_prices import ClosingPrices
    asof = "2026-11-18"
    ad_frames = {
        "600519": pd.DataFrame({"kline_time": [asof], "close": [1500]}),
        "513500": pd.DataFrame({"kline_time": [asof], "close": [2.3]}),
        "159632": pd.DataFrame({"kline_time": ["2026-11-17"], "close": [2.366]}),
        "510050": pd.DataFrame({"kline_time": [asof], "close": pd.Series([pd.NA], dtype="Float64")}),
    }
    em_frames = {
        "510300": pd.DataFrame({"date": [asof], "close": [10], "hfq_close": [20]}),
        "510050": pd.DataFrame({"date": ["2026-11-17"], "close": [9]}),
    }
    ad_calls, em_calls, sina_calls = [], [], []

    def ad_fetch(codes, begin_date, end_date):
        ad_calls.append((codes, begin_date, end_date))
        return ad_frames

    def em_fetch(code, end_date):
        assert end_date == asof
        em_calls.append(code)
        if code == "159632":
            raise ValueError("东财行情为空")
        return em_frames[code]

    def sina_fetch(symbol):
        sina_calls.append(symbol)
        return pd.DataFrame({"date": [asof], "close": [{"sz159632": 2.39, "sh510050": 12}[symbol]],
                             "volume": [10000]})

    monkeypatch.setattr("data_provider.fetchers.amazingdata.market.fetch_kline", ad_fetch)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", em_fetch)
    monkeypatch.setattr(ak, "fund_etf_hist_sina", sina_fetch)
    feed = ClosingPrices()
    codes = ["600519", "513500", "510300", "159632", "510050"]
    closes = {"600519": 1500, "513500": 2.3, "510300": 10, "159632": 2.39, "510050": 12}
    assert feed.close_prices(codes, asof) == closes
    assert feed.close_prices(codes, asof) == closes
    assert ad_calls == [(codes, asof, asof)]
    assert em_calls == ["510300", "159632", "510050"]
    assert sina_calls == ["sz159632", "sh510050"]


def test_close_collector_rejects_intraday_and_nontrading_dates(monkeypatch):
    import portfolio_report as entry
    monkeypatch.setattr(entry, "get_trading_dates", calendar)
    now = datetime(2026, 9, 23, 9, 31, tzinfo=timezone(timedelta(hours=8)))
    assert entry.closed_date(now) == date(2026, 9, 22)
    with pytest.raises(ValueError, match="尚未收盘"):
        entry.run(asof="2026-09-23", now=now)
    with pytest.raises(ValueError, match="不是已确认交易日"):
        entry.run(asof="2026-09-20", now=now)
    monkeypatch.setattr(entry, "get_trading_dates", lambda start, end: [])
    with pytest.raises(ValueError, match="交易日历不可用"):
        entry.closed_date(now)
