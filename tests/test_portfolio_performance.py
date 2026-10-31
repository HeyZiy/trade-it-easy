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
    def fetch(code, *, adjust):
        seen.append((code, adjust))
        return frames[code]
    monkeypatch.setattr("data_provider.bars.get_etf_daily", fetch)
    feed = ClosingPrices()
    assert feed.close_prices(list(frames), "2026-09-23") == {"512880": 12}
    feed.close_prices(list(frames), "2026-09-23")
    assert len(seen) == 4 and all(adjust is None for _, adjust in seen)


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


def test_quality_combined_run_and_manual_stages_archive_separately(tmp_path, monkeypatch):
    import quality_pool as entry
    from src.task_io import save_report
    monkeypatch.setattr("src.quality_pool.config.POOL_LEDGER_PATH", str(tmp_path / "empty.jsonl"))
    monkeypatch.setattr("src.quality_pool.config.STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(entry.pool_signal, "run_signal", lambda *a, **kw: "# 信号\n无指令")
    monkeypatch.setattr(entry.assembly, "run_execution", lambda *a, **kw: "# 执行\n无成交")
    monkeypatch.setattr(entry, "notify", lambda *a, **kw: True)
    monkeypatch.setattr(entry, "save_report", lambda text, prefix, **kw: save_report(text, prefix, str(tmp_path), **kw))
    day = date(2026, 9, 23)
    entry.run_signal(day)
    entry.run_execute(day)
    entry.run_execute(day, dry_run=True)
    entry.run(day)
    entry.run(day, dry_run=True)
    assert {p.name for p in tmp_path.glob("*.md")} == {
        "quality_pool_signal_20260923.md", "quality_pool_execute_20260923.md",
        "quality_pool_execute_dry_run_20260923.md", "quality_pool_20260923.md",
        "quality_pool_dry_run_20260923.md"}


def test_two_account_collector_keeps_ledgers_untouched(tmp_path, monkeypatch):
    import portfolio_report as entry
    from src.trade_ledger.closing_prices import ClosingPrices
    main, pool = tmp_path / "main.jsonl", tmp_path / "pool.jsonl"
    for path in (main, pool):
        append_trade(TradeRecord("2026-09-22", "510300", "测试ETF", "buy", 100, 10), path)
    before = [p.read_bytes() for p in (main, pool)]
    monkeypatch.setattr("src.etf.config.ACCOUNT_SOURCE.ledger_path", str(main))
    monkeypatch.setattr("src.quality_pool.config.ACCOUNT_SOURCE.ledger_path", str(pool))
    monkeypatch.setattr(entry, "get_trading_dates", calendar)
    monkeypatch.setattr(ClosingPrices, "close_prices", lambda self, codes, asof: {c: 10 if asof == "2026-09-21" else 11 for c in codes})
    monkeypatch.setattr(ClosingPrices, "index_prices", lambda self, codes, asof: {c: 100 if asof == "2026-09-21" else 102 for c in codes})
    now = datetime(2026, 9, 23, 15, 30, tzinfo=timezone(timedelta(hours=8)))
    report, errors = entry.run(now=now)
    assert errors == 0 and "ETF账户" in report and "质量池轮动" in report
    assert all(performance_path(p).exists() for p in (main, pool))
    assert [p.read_bytes() for p in (main, pool)] == before
    # 补采更早日期失败时，也要报告该日期的失败，不被已存在的最新记录掩盖。
    monkeypatch.setattr(ClosingPrices, "close_prices", lambda self, codes, asof: {})
    _, errors = entry.run(asof="2026-09-22", now=now)
    assert errors == 2
