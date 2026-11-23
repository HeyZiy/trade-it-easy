"""账户基准按日复利，与账户流水及估值独立。"""

from datetime import date

import pytest

from src.etf_account import account_data, backfill_benchmarks, collect_close, initialize, load
from src.account_store import save

SESSIONS = ["2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"]
DAILY_RETURNS = {
    "H00300": {"2025-01-06": .10, "2025-01-07": -.10, "2025-01-08": .05},
    "H11001": {"2025-01-06": .01, "2025-01-07": .02, "2025-01-08": -.01},
    "513500": {"2025-01-06": .20, "2025-01-07": -.04},
    "159934": {"2025-01-06": -.02, "2025-01-07": .06},
    "001821": {"2025-01-06": .001, "2025-01-07": .0002},
}


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr("src.etf_account._today", lambda: date(2025, 1, 8))
    monkeypatch.setattr("src.account_benchmarks.get_trading_dates", lambda start, end: [
        date.fromisoformat(day) for day in SESSIONS if start.isoformat() <= day <= end.isoformat()])
    path = tmp_path / "data" / "etf_account.json"
    initialize(path, asof="2025-01-04", cash=1000,
               positions=[{"code": "588000", "count": 1000, "price": 2, "strategy": "core"}],
               targets={"588000": .4}, budgets={"core": 1}, cash_allocations={"core": 1000})
    return path


def close(path, day, prices=None):
    collect_close(path, day, lambda *_: {"588000": 2} if prices is None else prices)


def test_composites_chain_daily_and_continue_from_last_saved_level(account):
    close(account, "2025-01-06")
    close(account, "2025-01-07")
    calls = []

    def feed(codes, sessions):
        calls.append((codes, sessions))
        return DAILY_RETURNS

    state = backfill_benchmarks(account, feed)
    assert calls == [(["001821", "159934", "513500", "H00300", "H11001"], SESSIONS[:3])]
    assert state["benchmark_series"]["etf"]["navs"] == pytest.approx({
        "2025-01-04": 1, "2025-01-06": 1.064, "2025-01-07": 1.064 * .948})
    core = 1 + .30 * .10 + .35 * .20 + .05 * -.02 + .30 * .001
    assert state["benchmark_series"]["core"]["navs"]["2025-01-06"] == pytest.approx(core)
    assert account_data(account)["history"][-1]["benchmark_nav"] == pytest.approx(1.064 * .948)
    calls.clear()
    backfill_benchmarks(account, feed)
    assert not calls

    close(account, "2025-01-08")
    state = backfill_benchmarks(account, feed)
    assert calls[0][1] == SESSIONS[-2:]
    assert state["benchmark_series"]["etf"]["navs"]["2025-01-08"] == pytest.approx(1.064 * .948 * 1.026)


def test_legacy_price_benchmark_is_not_displayed_under_new_label(account):
    close(account, "2025-01-06")
    state = load(account)
    state["benchmark"] = {"name": "沪深300", "index": "000300", "baseline_price": 4000}
    state["records"][-1]["benchmark_nav"] = 1.06
    save(account, state)

    view = account_data(account)
    assert view["benchmark_name"] == "沪深300全收益60% + 中证全债40%"
    assert view["history"][-1]["benchmark_nav"] is None
    state = backfill_benchmarks(account, lambda *_: DAILY_RETURNS)
    assert state["records"][-1]["benchmark_nav"] == 1.06
    assert account_data(account)["history"][-1]["benchmark_nav"] == pytest.approx(1.064)


def test_missing_intermediate_session_blocks_compounding_and_retries(account):
    close(account, "2025-01-07")  # 6 日没有账户估值，基准仍要按交易日复利。
    records = load(account)["records"]
    incomplete = {**DAILY_RETURNS, "H11001": {"2025-01-07": .02}}
    state = backfill_benchmarks(account, lambda *_: incomplete)
    assert state["records"] == records
    assert state["benchmark_series"]["etf"]["navs"] == {"2025-01-04": 1}
    assert state["benchmark_series"]["core"]["navs"]["2025-01-07"] > 1
    assert account_data(account)["history"][-1]["benchmark_nav"] is None

    state = backfill_benchmarks(account, lambda *_: DAILY_RETURNS)
    assert state["records"] == records
    assert state["benchmark_series"]["etf"]["navs"]["2025-01-07"] == pytest.approx(1.064 * .948)


def test_invalid_account_close_is_excluded_from_comparison(account):
    close(account, "2025-01-06", prices={})
    close(account, "2025-01-07")
    state = backfill_benchmarks(account, lambda *_: DAILY_RETURNS)
    assert state["records"][1]["nav"] is None
    assert account_data(account)["history"][1]["benchmark_nav"] is None
    assert account_data(account)["history"][2]["benchmark_nav"] == pytest.approx(1.064 * .948)


def test_explicit_collection_cutoff_does_not_calculate_future_records(account):
    close(account, "2025-01-06")
    close(account, "2025-01-07")
    state = backfill_benchmarks(account, lambda *_: DAILY_RETURNS, through="2025-01-06")
    assert state["benchmark_series"]["etf"]["navs"] == pytest.approx({"2025-01-04": 1, "2025-01-06": 1.064})
