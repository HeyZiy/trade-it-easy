# -*- coding: utf-8 -*-
"""场外基金定投：计划校验、到期推算、补记幂等与 fail-closed 行为。"""

from datetime import date, timedelta
import logging
from uuid import uuid4

import pytest

from src import etf_account as wallet
from src import fund_plans
from src.etf_account import account_data, collect_close, initialize, load, record_event, revision


@pytest.fixture(autouse=True)
def account_day(monkeypatch):
    monkeypatch.setattr('src.etf_account._today', lambda: date(2025, 1, 7))
    monkeypatch.setattr('src.fund_plans._today', lambda: date(2025, 1, 7))


@pytest.fixture
def account(tmp_path):
    path = tmp_path / 'data' / 'etf_account.json'
    initialize(path, asof='2025-01-04', cash=1000,
               positions=[dict(code='588000', name='ETF', count=1000, price=2, strategy='core')],
               targets={'588000': .4}, budgets={'core':1}, cash_allocations={'core':1000})
    collect_close(path, '2025-01-06', lambda *_: {'588000': 2})
    return path


@pytest.fixture
def weekend_calendar(monkeypatch):
    """以固定周末判定模拟交易日历（周末休市，其余全开市）；滞后计数同口径。"""
    def fake_days(start, end):
        return sorted(day for day in (start + timedelta(days=i)
                                      for i in range((end - start).days + 1))
                      if day.weekday() < 5)

    def fake_lag(earlier, later):
        days = fake_days(earlier, later)
        return len(days) - 1 if days else None

    monkeypatch.setattr('src.fund_plans.get_trading_dates', fake_days)
    monkeypatch.setattr('src.fund_plans.trading_days_lag', fake_lag)


def save(account, **fields):
    return fund_plans.save_plan(account, dict(code='000051', amount=100.0, frequency='monthly',
                                              anchor=5, enabled=True, note='') | fields)


def save_backdated(account, monkeypatch, created_on='2024-12-01', **fields):
    """以更早的 created_on 建计划：补记场景的到期日都落在建计划日期之前。"""
    monkeypatch.setattr('src.fund_plans._today', lambda: date.fromisoformat(created_on))
    plan = save(account, **fields)
    monkeypatch.setattr('src.fund_plans._today', lambda: date(2025, 1, 7))
    return plan


def fee_event(path, side='fund_buy', **fields):
    # fund_buy 会在多资产配置扣 10 元现金：后续断言的现金基数是 990。
    return record_event(path, dict(request_id=str(uuid4()), revision=revision(load(path)),
                                   date='2025-01-06', side=side, strategy='core',
                                   code='000051', qty=1, amount=10, nav=1,
                                   nav_date='2025-01-06',
                                   subscription_fee={"kind": "none", "value": 0}) | fields)


def void_event(path, pending_id, **fields):
    return record_event(path, dict(request_id=str(uuid4()), revision=revision(load(path)),
                                   date='2025-01-07', side='fund_void',
                                   voided_pending_id=pending_id) | fields)


def _any_plan_id(account):
    return fund_plans.load_plans(account)['plans'][0]['id']

# ---------- 计划 CRUD ----------

def test_save_and_update_plan_roundtrip(account):
    saved = save(account, note='沪深300')
    assert len(saved['id']) == 32
    assert saved['created_on'] == '2025-01-07'
    assert fund_plans.load_plans(account)['plans'] == [saved]

    fund_plans.save_plan(account, saved | {'note': '改备注'})
    updated = fund_plans.load_plans(account)['plans']
    assert updated == [{**saved, 'note': '改备注'}]


def test_plan_validation_rejects_bad_fields(account):
    bad = [dict(code='51'), dict(amount=0), dict(frequency='daily'),
           dict(frequency='monthly', anchor=32), dict(frequency='weekly', anchor=6)]
    for field in bad:
        with pytest.raises(ValueError):
            save(account, **field)
    assert fund_plans.load_plans(account)['plans'] == []


def test_delete_plan_is_idempotent_and_keeps_events(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch)
    fee_event(account)
    assert len(fund_plans.record_due_subscriptions(account)) == 1
    events_before = len(load(account)['events'])
    plan_id = _any_plan_id(account)
    assert fund_plans.delete_plan(account, {'id': plan_id}) is None
    assert fund_plans.delete_plan(account, {'id': plan_id}) is None  # 幂等
    assert fund_plans.load_plans(account)['plans'] == []
    assert len(load(account)['events']) == events_before  # 删除计划不动账本


# ---------- 到期日推算 ----------

def test_monthly_anchor_collapses_to_month_end(weekend_calendar):
    plan = dict(frequency='monthly', anchor=31, created_on='2025-01-01')
    assert fund_plans._due_dates(plan, date(2025, 2, 1), date(2025, 4, 30)) == \
        [date(2025, 2, 28), date(2025, 3, 31), date(2025, 4, 30)]


def test_monthly_anchor_postpones_after_created_on(weekend_calendar):
    # 2025-01-04 周六 → 顺延 1-06 周一；1-05 周日 → 同样落在 1-06。
    plan = dict(frequency='monthly', anchor=4, created_on='2024-12-01')
    assert fund_plans._due_dates(plan, date(2025, 1, 1), date(2025, 2, 28)) == \
        [date(2025, 1, 6), date(2025, 2, 4)]


def test_weekly_anchor_lands_on_friday(weekend_calendar):
    plan = dict(frequency='weekly', anchor=5, created_on='2025-01-01')
    assert fund_plans._due_dates(plan, date(2025, 1, 6), date(2025, 1, 20)) == \
        [date(2025, 1, 10), date(2025, 1, 17)]


def test_due_dates_respect_created_on(weekend_calendar):
    plan = dict(frequency='monthly', anchor=10, created_on='2025-03-15')
    assert fund_plans._due_dates(plan, date(2025, 1, 1), date(2025, 4, 30)) == [date(2025, 4, 10)]


# ---------- 补记 ----------

def test_record_is_silent_noop_without_plans_file(account, monkeypatch):
    fetched = []

    def fetch(code, day):
        fetched.append((code, day))
        return day, 1.0

    monkeypatch.setattr('data_provider.fund_nav.latest_fund_nav', fetch)
    assert fund_plans.record_due_subscriptions(account) == []
    assert fetched == []
    assert fund_plans.load_plans(account)['plans'] == []


def test_record_due_subscription_with_fee_and_postponement(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, anchor=5)  # 2025-01-05 周日 → 顺延 1-06 周一
    fee_event(account, subscription_fee={"kind": "rate", "value": 0.5})
    recorded = fund_plans.record_due_subscriptions(account)
    assert len(recorded) == 1
    event = recorded[0]
    assert event['side'] == 'fund_pending'
    assert event['code'] == '000051' and event['amount'] == 100.0
    assert event['nav_date'] == '2025-01-06'
    assert event['date'] == '2025-01-07'
    assert event['subscription_fee'] == {"kind": "rate", "value": 0.5}
    view = account_data(account)
    assert view['cash_by_strategy']['core'] == 890.0  # 1000 - 10（登记费率的买入）- 100
    assert view['pending_fund_value'] == 100.0


def test_record_is_idempotent_across_reruns(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, anchor=5)
    fee_event(account)
    assert len(fund_plans.record_due_subscriptions(account)) == 1
    assert fund_plans.record_due_subscriptions(account) == []
    pendings = [e for e in load(account)['events'] if e['side'] == 'fund_pending']
    assert len(pendings) == 1
    assert pendings[0]['id'].startswith('dca-')


def test_catchup_records_missed_day_with_correct_nav_date(account, weekend_calendar, monkeypatch):
    # 回看窗口 14 天覆盖 2025-01-01；元旦（周三）按周末日历是交易日，正常补记。
    save_backdated(account, monkeypatch, anchor=1)
    fee_event(account)
    recorded = fund_plans.record_due_subscriptions(account)
    assert len(recorded) == 1
    assert recorded[0]['nav_date'] == '2025-01-01'
    assert recorded[0]['date'] == '2025-01-07'


def test_two_plans_same_code_both_recorded(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, amount=100.0, note='A')
    save_backdated(account, monkeypatch, amount=200.0, note='B')
    fee_event(account)
    recorded = fund_plans.record_due_subscriptions(account)
    assert [row['amount'] for row in recorded] == [100.0, 200.0]


def test_unknown_fee_fail_closed_with_warning(account, weekend_calendar, monkeypatch, caplog):
    save_backdated(account, monkeypatch, code='999999')  # 该代码没有已登记申购费率
    with caplog.at_level(logging.WARNING, logger='src.fund_plans'):
        assert fund_plans.record_due_subscriptions(account) == []
    assert any('申购费率' in row.message for row in caplog.records)
    assert not [e for e in load(account)['events'] if e['side'] == 'fund_pending']


def test_cash_shortage_fails_one_plan_others_continue(account, weekend_calendar, monkeypatch, caplog):
    save_backdated(account, monkeypatch, code='000051', amount=600.0, note='A')
    save_backdated(account, monkeypatch, code='000124', amount=600.0, note='B')
    fee_event(account)
    fee_event(account, code='000124', qty=2, amount=20)
    with caplog.at_level(logging.WARNING, logger='src.fund_plans'):
        recorded = fund_plans.record_due_subscriptions(account)
    assert len(recorded) == 1
    assert recorded[0]['amount'] == 600.0
    assert any('现金不足' in row.message or '补记失败' in row.message for row in caplog.records)
    assert sum(e['amount'] for e in load(account)['events'] if e['side'] == 'fund_pending') == 600.0


def test_disabled_plan_is_skipped(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, enabled=False)
    fee_event(account)
    assert fund_plans.record_due_subscriptions(account) == []


# ---------- 作废 ----------

def test_void_pending_restores_cash(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, amount=100.0)
    fee_event(account, subscription_fee={"kind": "rate", "value": 0.5})
    pending_id = fund_plans.record_due_subscriptions(account)[0]['id']
    assert account_data(account)['cash'] == 890.0  # 990 - 100 在途
    void = void_event(account, pending_id)
    assert void['code'] == '000051' and void['amount'] == 100.0
    view = account_data(account)
    assert view['cash'] == view['cash_by_strategy']['core'] == 990.0
    assert view['fund_pending'] == {} and view['pending_fund_value'] == 0
    with pytest.raises(ValueError, match='不存在或已确认'):
        void_event(account, pending_id)


def test_voided_pending_blocks_edit_of_original(account, weekend_calendar, monkeypatch):
    from src.etf_account import event_context

    save_backdated(account, monkeypatch, amount=100.0)
    fee_event(account)
    pending_id = fund_plans.record_due_subscriptions(account)[0]['id']
    void_event(account, pending_id)
    with pytest.raises(ValueError, match='已作废'):
        event_context(account, {'event_id': pending_id, 'revision': revision(load(account))})


def test_void_then_snapshot_replay_is_consistent(account, weekend_calendar, monkeypatch):
    # 申购与作废同为记账日：重放整本流水含补偿事件，账目必须对称。
    save_backdated(account, monkeypatch, amount=100.0)
    fee_event(account)
    pending_id = fund_plans.record_due_subscriptions(account)[0]['id']
    before = wallet.snapshot(load(account), '2025-01-07')
    assert before['pending_fund_value'] == 100.0 and before['cash'] == 890.0
    void_event(account, pending_id)
    after = wallet.snapshot(load(account), '2025-01-07')
    assert after['pending_fund_value'] == 0 and after['cash'] == 990.0
    assert after['cash_by_strategy']['core'] == 990.0 and after['fund_pending'] == {}


# ---------- 告警与日期边界 ----------

def test_aging_pending_warns_after_threshold(account, weekend_calendar, monkeypatch, caplog):
    # nav_date 远早于今日：超过 5 个交易日未确认份额即在补记流程末尾告警。
    save_backdated(account, monkeypatch, anchor=5)
    record_event(account, dict(request_id=str(uuid4()), revision=revision(load(account)),
                               date='2025-01-07', side='fund_pending', code='000051',
                               amount=50, nav_date='2024-12-20',
                               subscription_fee={"kind": "none", "value": 0}))
    with caplog.at_level(logging.WARNING, logger='src.fund_plans'):
        fund_plans.record_due_subscriptions(account)
    assert any('未确认份额' in row.message for row in caplog.records)


def test_calendar_unavailable_skips_recording(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, anchor=5)
    fee_event(account)
    monkeypatch.setattr('src.fund_plans.get_trading_dates', lambda start, end: [])
    assert fund_plans.record_due_subscriptions(account) == []
    assert not [e for e in load(account)['events'] if e['side'] == 'fund_pending']
