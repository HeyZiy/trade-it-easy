"""盘前收盘补采限于投资账户。"""

from datetime import datetime, timedelta, timezone

import pytest

import portfolio_report as entry
from src import allocation_report, etf_account as wallet, fund_plans, portfolio_config
from data_provider.closing_prices import ClosingPrices

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
    monkeypatch.setattr("src.account_benchmarks.get_trading_dates", calendar)
    monkeypatch.setattr(entry, "benchmark_returns", lambda codes, sessions: {
        code: {day: .01 for day in sessions[1:]} for code in codes})
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
    assert saved["benchmark_series"]["etf"]["navs"]["2026-09-23"] == pytest.approx(1.01 ** 2)
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


def test_close_reports_allocation_actions_and_reprices_pending_suggestions(account, tmp_path, monkeypatch):
    path = tmp_path / "allocation_account.json"
    wallet.initialize(path, asof="2026-09-21", cash=5000,
                      positions=[{"code": "510300", "count": 500, "price": 10, "strategy": "core"}],
                      budgets={"core": .5, "bond": .5}, targets={"510300": .2},
                      cash_allocations={"core": 1000, "bond": 4000})
    config_path = portfolio_config.path_for(path)
    config = portfolio_config.load(config_path)
    expected = portfolio_config.revision(config)
    config["bond"] = [{"code": "FUND:110020", "fund_codes": ["110020"], "label": "场外固收",
                       "weight": .5, "asset_type": "bond", "tracked_index": None,
                       "valuation_kind": "none", "dividend_style": False, "volatility_rank": 0}]
    portfolio_config.save_config(config_path, config, expected)
    monkeypatch.setattr(wallet, "ACCOUNT_PATH", path)
    monkeypatch.setattr(ClosingPrices, "close_prices", lambda _, codes, day: {
        code: 10 if day == "2026-09-24" else 12 for code in codes})
    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(allocation_report, "notify", lambda report: sent.append(report) or True)

    close = NOW.replace(hour=18)
    preview, errors = entry.run(now=close, dry_run=True)
    assert not errors and "收盘再平衡建议" in preview
    assert not sent and not (tmp_path / "reports").exists()
    assert entry.run(now=close)[1] == entry.run(now=close)[1] == 0
    assert len(sent) == 1
    assert "权益组合" in sent[0] and "400 份，约 4,000.00 元" in sent[0]
    assert "固收组合" in sent[0] and "买入 场外固收（110020）约 2,500.00 元" in sent[0]
    assert "仍待处理" not in sent[0]

    assert entry.run(now=close + timedelta(days=1))[1] == 0
    assert len(sent) == 2 and sent[1].count("仍待处理") == 2
    assert "约 4,800.00 元" in sent[1] and "约 2,750.00 元" in sent[1]
    assert (tmp_path / "reports/allocation_rebalance_20260925.md").read_text(encoding="utf-8") == sent[1]


def test_close_report_stays_quiet_without_actions_and_retries_failed_delivery(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(allocation_report, "notify", lambda report: sent.append(report) or len(sent) > 1)
    actions = {"core:510300:买入": ("权益组合", "买入约 800 元")}

    assert allocation_report.publish("2026-09-22", {}, reports_dir=tmp_path)
    assert not sent
    assert not allocation_report.publish("2026-09-23", actions, reports_dir=tmp_path)
    assert allocation_report.publish("2026-09-23", actions, reports_dir=tmp_path)
    assert sent[0] == sent[1]
    assert allocation_report.publish("2026-09-24", {}, reports_dir=tmp_path)
    assert len(sent) == 2
    assert allocation_report.publish("2026-09-25", actions, reports_dir=tmp_path)
    assert "仍待处理" not in sent[2]
