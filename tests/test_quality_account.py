"""质量动量实际账户闭环：真实选股核、独立周期、人工成交及共享账户边界。"""

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from src import etf_account as wallet, portfolio_config
from src.quality_pool import config, feeds
from src.trade_ledger.ledger import TradeRecord, append_trade
from test_quality_pool_state import env, _patch_signal_data, _patch_execute_data


DAY = date(2026, 10, 2)


@pytest.fixture
def account(env, tmp_path):
    env.setattr(wallet, "_today", lambda: DAY)
    path = tmp_path / "data" / "etf_account.json"
    wallet.initialize(path, asof="2026-09-30", cash=119000,
                      positions=[dict(code="510300", count=1000, price=1, strategy="core")],
                      budgets={"core": .83, "rotation": .07, "quality": .1},
                      cash_allocations={"core": 100000, "rotation": 7000, "quality": 12000})
    wallet.collect_close(path, "2026-10-01", lambda *_: {"510300": 1})
    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 10, "600002": 20, "600003": 10})
    return path


def quality(path):
    return next(s for s in wallet.account_data(path)["strategies"] if s["id"] == "quality")


def event(path, side, **fields):
    return wallet.record_event(path, dict(request_id=str(uuid4()), revision=wallet.revision(wallet.load(path)),
                                          date=DAY.isoformat(), strategy="quality", side=side) | fields)


def test_plan_uses_ten_percent_budget_and_never_imports_simulated_positions(account, env):
    from dashboard import dashboard_data

    env.setattr(config, "POOL_LEDGER_PATH", str(account.with_name("trade_ledger_quality_pool_v2.jsonl")))
    append_trade(TradeRecord("2026-10-01", "600003", "模拟旧仓", "buy", 100, 10, account=config.ACCOUNT),
                 config.POOL_LEDGER_PATH)
    simulated = Path(config.POOL_LEDGER_PATH).read_bytes()
    initial = wallet.account_data(account)
    wallet.publish_quality(DAY.isoformat(), path=account)
    row = quality(account)
    assert row["budget"] == 12000 and row["cash"] == 12000
    assert {o["code"]: o["qty"] for o in row["suggestions"]} == {"600001": 200, "600002": 100}
    assert not row["positions"] and not row["trades"]
    after = wallet.account_data(account)
    assert (after["equity"], after["cash"], after["history"]) == (initial["equity"], initial["cash"], initial["history"])
    assert Path(config.POOL_LEDGER_PATH).read_bytes() == simulated
    assert not Path(config.STATE_PATH).exists()
    displayed = next(s for s in dashboard_data(account.parent.parent)["strategies"] if s["id"] == "quality")
    assert displayed["account_id"] == "etf" and displayed["observation"]["positions"][0]["code"] == "600003"


def test_partial_fill_consumes_only_remaining_plan_and_keeps_realized_pnl(account):
    wallet.publish_quality(DAY.isoformat(), path=account)
    event(account, "buy", code="600001", qty=100, price=10, fee=5)
    assert next(o for o in quality(account)["suggestions"] if o["code"] == "600001")["qty"] == 100
    event(account, "buy", code="600001", qty=100, price=10, fee=5)
    assert all(o["code"] != "600001" for o in quality(account)["suggestions"])
    event(account, "sell", code="600001", qty=200, price=11, fee=5)
    event(account, "deposit", amount=1000)
    assert all(o["code"] != "600001" for o in quality(account)["suggestions"])
    assert quality(account)["pnl"] == 185
    assert quality(account)["cash"] == 13185
    assert wallet.account_data(account)["cash_by_strategy"]["core"] == 100000


def test_actual_old_position_drives_exit_and_next_day_retries_only_exits(account, env):
    event(account, "buy", code="600003", qty=100, price=10, fee=0)
    # 前一日持仓，允许本轮退出；模拟账户没有这只股票。
    env.setattr(wallet, "_today", lambda: date(2026, 10, 5))
    wallet.publish_quality("2026-10-05", path=account)
    orders = quality(account)["suggestions"]
    assert orders[0]["side"] == "sell" and orders[0]["code"] == "600003"
    assert any(o["side"] == "buy" and o["cash_note"] for o in orders)
    env.setattr(wallet, "_today", lambda: date(2026, 10, 6))
    assert not quality(account)["suggestions"]  # 昨日买入建议过期。
    wallet.publish_quality("2026-10-06", path=account)
    assert [(o["side"], o["code"]) for o in quality(account)["suggestions"]] == [("sell", "600003")]
    assert wallet.load(account)["quality_state"]["next_signal_date"] == "2026-11-02"


def test_dry_run_does_not_write_account_or_simulation(account):
    before = account.read_bytes()
    report = wallet.publish_quality(DAY.isoformat(), path=account, dry_run=True)
    assert "买入" in report
    assert account.read_bytes() == before
    assert not Path(config.STATE_PATH).exists() and not Path(config.POOL_LEDGER_PATH).exists()


def test_ignore_group_remains_ignored_after_cash_changes_and_same_day_rerun(account):
    wallet.publish_quality(DAY.isoformat(), path=account)
    row = quality(account)["suggestions"][0]
    wallet.dismiss(account, {"revision": wallet.revision(wallet.load(account)), "id": row["id"]})
    event(account, "deposit", amount=100)
    wallet.publish_quality(DAY.isoformat(), path=account)
    assert not quality(account)["suggestions"]
    assert quality(account)["advice_status"] == "本轮建议已忽略"


def test_failure_pauses_advice_and_recovery_preserves_cycle(account, env):
    _patch_signal_data(env, fail=True)
    assert "暂停" in wallet.publish_quality(DAY.isoformat(), path=account)
    assert not quality(account)["suggestions"]
    assert wallet.load(account)["quality_state"]["signal_retry"]
    next_day = wallet.load(account)["quality_state"]["next_signal_date"]
    _patch_signal_data(env)
    _patch_execute_data(env, {"600001": 10, "600002": 20})
    assert "买入" in wallet.publish_quality(DAY.isoformat(), path=account)
    assert wallet.load(account)["quality_state"]["next_signal_date"] == next_day


def test_concurrent_account_change_rejects_plan_without_overwriting_user_event(account, env):
    original = feeds.fetch_universe

    def changing_account(day):
        event(account, "deposit", amount=100)
        return original(day)

    env.setattr(feeds, "fetch_universe", changing_account)
    with pytest.raises(ValueError, match="账户已更新"):
        wallet.publish_quality(DAY.isoformat(), path=account)
    assert quality(account)["cash"] == 12100 and len(wallet.load(account)["events"]) == 1
    assert not quality(account)["suggestions"]


def test_quality_cannot_spend_other_strategies_cash_and_budget_changes_preserve_ownership(account):
    before = wallet.account_data(account)
    with pytest.raises(ValueError, match="该策略现金不足"):
        event(account, "buy", code="600001", qty=1300, price=10, fee=0)
    wallet.set_budgets(account, {"revision": before["revision"], "budgets": {"core": .83, "rotation": .07, "quality": .1}})
    assert wallet.account_data(account)["cash_by_strategy"] == before["cash_by_strategy"]
    invalid = portfolio_config.example()
    invalid["budgets"]["quality"] = .2
    with pytest.raises(ValueError, match="100%"):
        portfolio_config.validate(invalid)


def test_missing_execution_status_blocks_buys(account, env):
    original = feeds.fetch_execution_status
    env.setattr(feeds, "fetch_execution_status", lambda codes, day: original(codes, day).iloc[:0])
    wallet.publish_quality(DAY.isoformat(), path=account)
    assert not quality(account)["suggestions"]


def test_same_day_cash_assignment_activates_budget_plan_without_new_round(account):
    event(account, "cash_assign", cash_allocations={"core": 112000, "rotation": 7000})
    wallet.publish_quality(DAY.isoformat(), path=account)
    assert not quality(account)["suggestions"]
    assert "分配现金" in quality(account)["advice_status"]
    event(account, "cash_assign", cash_allocations={"core": 100000, "rotation": 7000, "quality": 12000})
    assert {o["code"]: o["qty"] for o in quality(account)["suggestions"]} == {"600001": 200, "600002": 100}


def test_entry_runs_simulation_and_actual_plan_but_only_manual_trades_enter_account(account, env):
    import quality_pool as entry

    env.setattr(wallet, "ACCOUNT_PATH", account)
    report = entry.run(DAY)
    assert "投资账户 · 质量动量计划" in report and "买入" in report
    assert not quality(account)["positions"] and not wallet.load(account)["events"]
    assert Path(config.POOL_LEDGER_PATH).exists() and Path(config.STATE_PATH).exists()
    assert wallet.load(account)["quality_state"]["pending_plan"] is None


def test_quality_shares_transfer_keeps_account_nav_and_cash(account):
    event(account, "buy", code="600001", qty=100, price=10, fee=5)
    before = wallet.account_data(account)
    event(account, "assign", code="600001", qty=100, from_strategy="quality", **{"strategy": "rotation"})
    after = wallet.account_data(account)
    assert after["equity"] == before["equity"] and after["cash_by_strategy"] == before["cash_by_strategy"]
    assert quality(account)["pnl"] == -5 and not quality(account)["positions"]


def test_six_stock_plan_reserves_fees_and_respects_lower_budget(account, env):
    codes = {f"60000{i}" for i in range(1, 7)}
    _patch_signal_data(env, universe=codes)
    _patch_execute_data(env, dict.fromkeys(codes, 10))
    wallet.publish_quality(DAY.isoformat(), path=account)
    orders = quality(account)["suggestions"]
    assert len(orders) == 6
    assert sum(o["amount"] + max(config.MIN_COMMISSION, o["amount"] * config.FEE_COMMISSION)
               for o in orders) <= 12000
    wallet.set_budgets(account, {"revision": wallet.revision(wallet.load(account)),
                                 "budgets": {"core": .83, "rotation": .07, "quality": .01}})
    assert sum(o["amount"] for o in quality(account)["suggestions"]) <= 1200


def test_position_change_after_signal_commit_forces_reselection_on_retry(account, env):
    from src.quality_pool import assembly

    original = assembly.build_snapshot

    def changing_holdings(*args, **kwargs):
        event(account, "buy", code="600003", qty=100, price=10, fee=0)
        return original(*args, **kwargs)

    env.setattr(assembly, "build_snapshot", changing_holdings)
    with pytest.raises(ValueError, match="账户已更新"):
        wallet.publish_quality(DAY.isoformat(), path=account)
    state = wallet.load(account)["quality_state"]
    assert state["signal_retry"] and state["pending_plan"] is None
    env.setattr(assembly, "build_snapshot", original)
    wallet.publish_quality(DAY.isoformat(), path=account)
    assert wallet.load(account)["quality_state"]["exit_queue"] == {"600003": config.EXIT_OUT_OF_POOL}
