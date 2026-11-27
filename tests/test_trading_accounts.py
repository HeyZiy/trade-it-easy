from copy import deepcopy
from datetime import date
from uuid import uuid4

import pytest

from src import etf_account, fund_plans, portfolio_config, trading_accounts
from src.account_replay import snapshot


DAY = "2025-01-06"


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "etf_account.json"
    etf_account.initialize(path, asof=DAY, cash=2000, cash_allocations={"core": 2000},
                           positions=[{"code": "510300", "count": 100, "price": 1, "strategy": "core"}])
    config = etf_account.load(path)["_config"]
    config["trading_accounts"] = deepcopy(trading_accounts.TEMPLATE)
    cp = portfolio_config.path_for(path)
    portfolio_config.save_config(cp, config, portfolio_config.current_revision(cp))
    return path


def post(path, side, **kw):
    return etf_account.record_event(path, dict(date=DAY, side=side, strategy="core",
        request_id=str(uuid4()), revision=etf_account.revision(etf_account.load(path))) | kw)


def move(path, amount, target):
    return post(path, "custody_transfer", kind="cash", trading_account="unlocated", to_account=target, amount=amount)


def test_two_platforms_reconcile_through_confirmation_and_refund(ledger, monkeypatch):
    move(ledger, 300, "platform1")
    move(ledger, 500, "platform2")
    fee = {"kind": "none", "value": 0}
    pending = post(ledger, "fund_pending", code="000001", nav_date=DAY, amount=100, subscription_fee=fee)
    order = post(ledger, "fund_pending", code="000001", nav_date=DAY, amount=150, subscription_fee=fee,
                 trading_account="platform2")
    void = post(ledger, "fund_void", voided_pending_id=order["id"])
    assert void["trading_account"] == "platform2"
    cp = portfolio_config.path_for(ledger)
    config = portfolio_config.load(cp)
    config["trading_accounts"]["products"]["fund:000001"] = "platform2"
    portfolio_config.save_config(cp, config, portfolio_config.current_revision(cp))
    monkeypatch.setattr("data_provider.fund_nav.latest_fund_nav", lambda *_: (DAY, 1))
    assert etf_account.confirm_fund_subscriptions(ledger, DAY) == [pending["id"]]
    assert etf_account.load(ledger)["events"][-1]["trading_account"] == "platform1"
    assert post(ledger, "fund_buy", code="000001", qty=200, amount=200)["trading_account"] == "platform2"
    with pytest.raises(ValueError, match="实际交易账户"):
        post(ledger, "fund_redeem", code="000001", qty=1, amount=1)
    with pytest.raises(ValueError, match="份额超过所选"):
        post(ledger, "fund_redeem", code="000001", qty=101, amount=101, trading_account="platform1")
    post(ledger, "fund_redeem", code="000001", qty=10, amount=10, trading_account="platform2")
    post(ledger, "assign", code="000001", qty=10, kind="fund", from_strategy="core",
         trading_account="platform2", strategy="holding_area")
    view = snapshot(etf_account.load(ledger), DAY)
    assert view["fund_positions"]["000001"]["qty"] == 290
    assert view["custody_cash"] == {"unlocated": 1200, "platform1": 200, "platform2": 310}
    assert sum(view["custody_cash"].values()) == view["cash"]
    assert view["equity"] == 2100 and view["units"] == 2100
    assert {(r["account"], r["strategy"]): r["qty"] for r in view["custody_positions"] if r["kind"] == "fund"} == {
        ("platform1", "core"): 100, ("platform2", "core"): 180, ("platform2", "holding_area"): 10}


def test_redemption_reservation_and_receipt_stay_at_origin_platform(ledger):
    for platform in ('platform1', 'platform2'):
        move(ledger, 100, platform)
        post(ledger, 'fund_buy', code='000001', qty=100, amount=100, trading_account=platform,
             nav=1, nav_date=DAY)
    order = post(ledger, 'fund_redeem_pending', code='000001', qty=90, trading_account='platform1')
    post(ledger, 'fund_redeem_pending', code='000001', qty=100, trading_account='platform2')
    with pytest.raises(ValueError, match='可用份额'):
        post(ledger, 'custody_transfer', kind='fund', code='000001', qty=11,
             trading_account='platform1', to_account='platform2')
    with pytest.raises(ValueError, match='可用份额'):
        post(ledger, 'assign', kind='fund', code='000001', qty=11, trading_account='platform1',
             from_strategy='core', strategy='holding_area')
    with pytest.raises(ValueError, match='沿用原赎回'):
        post(ledger, 'fund_redeem', pending_id=order['id'], amount=89, trading_account='platform2')
    receipt = post(ledger, 'fund_redeem', pending_id=order['id'], amount=89)
    assert receipt['trading_account'] == 'platform1'
    view = snapshot(etf_account.load(ledger), DAY)
    assert view['custody_cash']['platform1'] == 89
    assert view['custody_cash']['platform2'] == 0


def test_claim_transfer_and_historical_correction(ledger):
    state = etf_account.load(ledger)
    rev = etf_account.revision(state, DAY, include_transfers=True)
    transfer = move(ledger, 300, "broker")
    post(ledger, "custody_transfer", kind="listed", code="510300", qty=80, trading_account="unlocated", to_account="broker")
    assert etf_account.revision(etf_account.load(ledger), DAY, include_transfers=True) == rev
    with pytest.raises(ValueError, match="交易账户现金不足"):
        post(ledger, "buy", code="510300", qty=100, price=4)
    post(ledger, "buy", code="510300", qty=100, price=2)
    before = snapshot(etf_account.load(ledger), DAY)
    with pytest.raises(ValueError, match="交易账户现金不足"):
        etf_account.edit_event(ledger, {**transfer, "amount": 100, "event_id": transfer["id"],
            "request_id": str(uuid4()), "revision": etf_account.revision(etf_account.load(ledger))})
    post(ledger, "custody_transfer", kind="cash", amount=50, trading_account="broker", to_account="platform1")
    post(ledger, "deposit", amount=30, trading_account="platform1")
    post(ledger, "withdraw", amount=10, trading_account="platform1")
    view = snapshot(etf_account.load(ledger), DAY)
    assert view["custody_cash"] == {"unlocated": 1700, "broker": 50, "platform1": 70}
    assert view["cash"] == 1820 and view["pnl"] == before["pnl"]
    assert view["equity"] - before["equity"] == 20


def test_cash_adjust_spreads_difference_by_cash_share_without_touching_nav(ledger):
    move(ledger, 300, "platform1")
    post(ledger, "cash_transfer", from_strategy="core", strategy="holding_area", amount=600)
    before = snapshot(etf_account.load(ledger), DAY)
    adjustment = post(ledger, "cash_adjust", trading_account="unlocated", amount=3.17)
    assert adjustment["amount"] == 3.17
    view = snapshot(etf_account.load(ledger), DAY)
    assert view["custody_cash"] == {"unlocated": 1703.17, "platform1": 300}
    assert view["cash"] == 2003.17
    # 1400:600 按比例分摊 3.17 → 2.22 / 0.95，尾差归后者
    assert view["cash_by_strategy"] == {"core": 1402.22, "rotation": 0, "quality": 0, "holding_area": 600.95}
    assert sum(view["cash_by_strategy"].values()) == view["cash"]
    assert view["units"] == before["units"] and view["equity"] == before["equity"] + 3.17
    assert view["pnl"] == before["pnl"] + 3.17
    # 策略份额不变，差额成为持有者的收益：各策略盈亏按分摊额上升。
    for key in view["strategy_pnl"]:
        assert view["strategy_pnl"][key] == pytest.approx(before["strategy_pnl"][key] + view["cash_by_strategy"][key] - before["cash_by_strategy"][key])


def test_cash_adjust_negative_difference_cannot_push_cash_below_zero(ledger):
    move(ledger, 300, "platform1")
    with pytest.raises(ValueError, match="账面现金"):
        post(ledger, "cash_adjust", trading_account="unlocated", amount=-2500)
    with pytest.raises(ValueError, match="所选交易账户现金"):
        post(ledger, "cash_adjust", trading_account="platform1", amount=-301)
    with pytest.raises(ValueError, match="非零"):
        post(ledger, "cash_adjust", trading_account="unlocated", amount=0)
    negative = post(ledger, "cash_adjust", trading_account="unlocated", amount=-40)
    assert negative["amount"] == -40
    view = snapshot(etf_account.load(ledger), DAY)
    assert view["cash"] == 1960 and view["custody_cash"] == {"unlocated": 1660, "platform1": 300}
    # 策略层只有 core 持有现金，差额全部归 core
    assert view["cash_by_strategy"] == {"core": 1960, "rotation": 0, "quality": 0, "holding_area": 0}


def test_cash_adjust_can_be_corrected_and_replayed(ledger):
    move(ledger, 300, "platform1")
    first = post(ledger, "cash_adjust", trading_account="unlocated", amount=3.17)
    edit = {**{k: v for k, v in first.items() if k != "request"}, "event_id": first["id"],
            "request_id": str(uuid4()), "revision": etf_account.revision(etf_account.load(ledger)), "amount": 5}
    corrected = etf_account.edit_event(ledger, edit)
    assert corrected["amount"] == 5
    view = snapshot(etf_account.load(ledger), DAY)
    assert view["cash"] == 2005 and view["custody_cash"] == {"unlocated": 1705, "platform1": 300}
    assert etf_account.edit_event(ledger, edit) == corrected
    # 更正后若某策略现金被推成负数，整笔拒绝且不落盘
    post(ledger, "cash_transfer", from_strategy="core", strategy="holding_area", amount=1000)
    before = etf_account.load(ledger)["events"]
    with pytest.raises(ValueError, match="账面现金"):
        etf_account.edit_event(ledger, {**edit, "request_id": str(uuid4()),
            "revision": etf_account.revision(etf_account.load(ledger)), "amount": -2500})
    assert etf_account.load(ledger)["events"] == before


def test_two_recurring_plans_charge_their_own_platforms_once(ledger, monkeypatch):
    cp = portfolio_config.path_for(ledger)
    config = portfolio_config.load(cp)
    config["fund_subscription_fee"] = {"kind": "none", "value": 0}
    portfolio_config.save_config(cp, config, portfolio_config.current_revision(cp))
    monkeypatch.setattr(fund_plans, "_today", lambda: date.fromisoformat(DAY))
    monkeypatch.setattr(fund_plans, "_due_dates", lambda *_: [date.fromisoformat(DAY)])
    monkeypatch.setattr(fund_plans, "trading_days_lag", lambda *_: 1)
    for account in ("platform1", "platform2"):
        move(ledger, 200, account)
        fund_plans.save_plan(ledger, {"code": "000001", "amount": 50, "frequency": "daily",
                                      "trading_account": account})
    events = fund_plans.record_due_subscriptions(ledger)
    assert {e["trading_account"] for e in events} == {"platform1", "platform2"}
    assert fund_plans.record_due_subscriptions(ledger) == []
    view = snapshot(etf_account.load(ledger), "2025-01-07")
    assert view["custody_cash"] == {"unlocated": 1600, "platform1": 150, "platform2": 150}
    assert view["pending_fund_value"] == 100 and view["equity"] == 2100
