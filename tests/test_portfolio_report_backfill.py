"""盘前收盘补采限于投资账户。"""

from datetime import datetime, timedelta, timezone

import pytest

import portfolio_report as entry
from src import etf_account as wallet, fund_plans
from src.trade_ledger.closing_prices import ClosingPrices

NOW = datetime(2026, 9, 24, 8, 30, tzinfo=timezone(timedelta(hours=8)))


def calendar(start, end):
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]


@pytest.fixture
def account(tmp_path, monkeypatch):
    path = tmp_path / "etf_account.json"
    wallet.initialize(path, asof="2026-09-21", cash=1000,
                      positions=[{"code": "510300", "count": 100, "price": 10, "strategy": "core"}])
    monkeypatch.setattr(wallet, "ACCOUNT_PATH", path)
    monkeypatch.setattr(entry, "get_trading_dates", calendar)
    monkeypatch.setattr(wallet, "refresh_fund_navs", lambda *args: {"updated": 0, "failed": 0})
    monkeypatch.setattr(fund_plans, "record_due_subscriptions", lambda *args: 0)
    monkeypatch.setattr(wallet, "refresh_close_prices", lambda *args: [])
    monkeypatch.setattr(ClosingPrices, "close_prices", lambda _, codes, day: {c: 10. for c in codes})
    return path


def test_next_morning_repairs_previous_gap_and_keeps_old_simulation_results(account):
    wallet.collect_close(account, "2026-09-22", lambda codes, day: {})
    old_ledger = account.with_name("trade_ledger_etf_rotation.jsonl")
    old_ledger.write_text('retired simulation', encoding="utf-8")

    report, errors = entry.run(now=NOW, backfill=True)

    saved = wallet.load(account)
    recent = [r for r in saved["records"] if r["date"] > "2026-09-21"]
    assert [(r["date"], r["nav"]) for r in recent] == [
        ("2026-09-22", pytest.approx(1.0)), ("2026-09-23", pytest.approx(1.0))]
    assert errors == 0 and "已补齐" in report
    assert old_ledger.read_text(encoding="utf-8") == 'retired simulation'
    assert not old_ledger.with_suffix(".performance.json").exists()


def test_only_target_day_is_refetched_when_backfill_is_complete(account, monkeypatch):
    entry.run(now=NOW, backfill=True)
    calls = []

    def fetch(_, codes, asof):
        calls.append(asof)
        return {c: 10. for c in codes}

    monkeypatch.setattr(ClosingPrices, "close_prices", fetch)
    _, errors = entry.run(now=NOW, backfill=True)
    assert calls == ["2026-09-23"] and errors == 0
