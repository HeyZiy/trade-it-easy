"""收盘采集的次日回补：补齐旧洞、不重写有效记录、单账户失败隔离。"""

from datetime import datetime, timedelta, timezone

import pytest

from src.etf.config import CORE_BASELINE
from src.trade_ledger.closing_prices import ClosingPrices
from src.trade_ledger.ledger import TradeRecord, append_trade
from src.trade_ledger.performance import collect_close, load_performance, performance_path


def calendar(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


NOW = datetime(2026, 9, 24, 8, 30, tzinfo=timezone(timedelta(hours=8)))  # 目标日 = 09-23
BENCHMARK = {"name": "ETF期初权重买入持有（现金零收益，不再平衡）",
             "weights": {a.code: a.neutral_weight for a in CORE_BASELINE}}


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    import portfolio_report as entry
    main, pool, rotation = (tmp_path / name for name in
                            ("main.jsonl", "pool.jsonl", "rotation.jsonl"))
    append_trade(TradeRecord("2026-09-22", "510300", "测试ETF", "buy", 100, 10), main)
    # 账户事实的缝＝模拟账户描述符（src/sim_accounts 各家单点），不再逐家戳源模块常量。
    monkeypatch.setattr("src.etf.config.SIM_ACCOUNT.ledger_path", str(main))
    monkeypatch.setattr("src.quality_pool.config.SIM_ACCOUNT.ledger_path", str(pool))
    monkeypatch.setattr("src.etf.rotation.SIM_ACCOUNT.ledger_path", str(rotation))
    monkeypatch.setattr(entry, "get_trading_dates", calendar)
    return main, pool, rotation


def patch_feed(monkeypatch, missing=(), calls=None):
    def close_prices(self, codes, asof):
        if calls is not None:
            calls.append(asof)
        if asof in missing:
            return {}
        return {c: 10.0 for c in codes}

    monkeypatch.setattr(ClosingPrices, "close_prices", close_prices)
    monkeypatch.setattr(ClosingPrices, "index_prices",
                        lambda self, codes, asof: {c: 100.0 for c in codes})


def seed_gap(ledger):
    """先写一条缺价的 09-22 记录，模拟当晚源没给数。"""
    collect_close(ledger, "2026-09-22", BENCHMARK,
                  close_prices=lambda codes, asof: {},
                  index_prices=lambda codes, asof: {},
                  trading_dates=calendar)
    row = load_performance(ledger)["records"][-1]
    assert row["date"] == "2026-09-22" and row["nav"] is None and row["issues"]


def test_next_morning_heals_previous_gap(accounts, monkeypatch):
    import portfolio_report as entry
    main, _, _ = accounts
    seed_gap(main)
    patch_feed(monkeypatch)
    report, errors = entry.run(now=NOW, backfill=True)
    state = load_performance(main)
    row = next(r for r in state["records"] if r["date"] == "2026-09-22")
    assert row["nav"] == pytest.approx(1.0) and not row["issues"]
    assert "本次回补" in report and "2026-09-22" in report
    assert errors == 0


def test_valid_records_are_not_refetched(accounts, monkeypatch):
    import portfolio_report as entry
    main, _, _ = accounts
    patch_feed(monkeypatch)
    entry.run(now=NOW, backfill=True)
    saved = performance_path(main).read_bytes()
    calls = []

    def no_fetch(self, codes, asof):
        calls.append(asof)
        raise AssertionError("有效记录不应重新取价")

    monkeypatch.setattr(ClosingPrices, "close_prices", no_fetch)
    _, errors = entry.run(now=NOW, backfill=True)
    assert calls == [] and errors == 0
    assert performance_path(main).read_bytes() == saved


def test_missing_rows_are_created(accounts, monkeypatch):
    import portfolio_report as entry
    main, _, _ = accounts
    patch_feed(monkeypatch)
    report, errors = entry.run(now=NOW, backfill=True)
    assert [r["date"] for r in load_performance(main)["records"]] == ["2026-09-22", "2026-09-23"]
    assert "已补齐：2026-09-22" in report
    assert errors == 0


def test_one_account_failure_does_not_block_others(accounts, monkeypatch):
    import portfolio_report as entry
    main, pool, _ = accounts
    append_trade(TradeRecord("2026-09-22", "600001", "测试股", "buy", 100, 10), pool)
    patch_feed(monkeypatch)
    real_collect = entry.collect_close

    def flaky(path, *args, **kwargs):
        if str(path) == str(pool):
            raise RuntimeError("boom")
        return real_collect(path, *args, **kwargs)

    monkeypatch.setattr(entry, "collect_close", flaky)
    report, errors = entry.run(now=NOW, backfill=True)
    assert errors == 1 and "本次收盘采集失败" in report
    assert performance_path(main).exists()
