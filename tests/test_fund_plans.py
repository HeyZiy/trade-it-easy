# -*- coding: utf-8 -*-
"""场外基金定投计划和记账。"""

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
    monkeypatch.setattr('src.fund_plans._today', lambda: date.fromisoformat(created_on))
    plan = save(account, **({'anchor': 7} | fields))
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
    bad = [dict(code='51'), dict(amount=0), dict(frequency='yearly'),
           dict(frequency='monthly', anchor=32), dict(frequency='weekly', anchor=6)]
    for field in bad:
        with pytest.raises(ValueError):
            save(account, **field)
    assert fund_plans.load_plans(account)['plans'] == []


def test_switching_to_daily_starts_the_new_schedule_on_save(account, weekend_calendar, monkeypatch):
    weekly = save_backdated(account, monkeypatch, frequency='weekly', anchor=5)
    daily = fund_plans.save_plan(account, weekly | {'frequency': 'daily'})
    assert daily['anchor'] is None
    assert daily['created_on'] == weekly['created_on']

    monkeypatch.setattr(fund_plans, '_today', lambda: date(2025, 1, 8))
    updated = fund_plans.save_plan(account, daily | {'note': '改备注'})
    assert fund_plans._due_dates(updated, date(2025, 1, 1), date(2025, 1, 8)) == [
        date(2025, 1, 7), date(2025, 1, 8),
    ]

    switched_back = fund_plans.save_plan(account, updated | {'frequency': 'weekly', 'anchor': 5})
    assert fund_plans._due_dates(switched_back, date(2025, 1, 1), date(2025, 1, 10)) == [date(2025, 1, 10)]


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

def test_next_due_skips_a_recorded_installment(account, weekend_calendar):
    save(account, frequency='daily')
    fee_event(account)
    assert fund_plans.dca_status(account)['plans'][0]['next_due'] == '2025-01-07'
    fund_plans.record_due_subscriptions(account)
    assert fund_plans.dca_status(account)['plans'][0]['next_due'] == '2025-01-08'


def test_next_due_uses_month_end_and_holiday_postponement(account, weekend_calendar, monkeypatch):
    save(account, anchor=31)
    monkeypatch.setattr(fund_plans, '_today', lambda: date(2025, 8, 31))
    assert fund_plans.dca_status(account)['plans'][0]['next_due'] == '2025-09-01'


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


# ---------- 记账 ----------

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
    recorded = fund_plans.record_due_subscriptions(account, run_day=date(2025, 1, 6))
    assert len(recorded) == 1
    event = recorded[0]
    assert event['side'] == 'fund_pending'
    assert event['code'] == '000051' and event['amount'] == 100.0
    assert event['nav_date'] == '2025-01-06'
    assert event['date'] == '2025-01-06'
    assert event['subscription_fee'] == {"kind": "rate", "value": 0.5}
    view = account_data(account)
    assert view['cash_by_strategy']['core'] == 890.0  # 1000 - 10（登记费率的买入）- 100
    assert view['pending_fund_value'] == 100.0


def test_same_fund_dca_charges_each_plan_strategy_and_void_refunds_its_cash(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, strategy='core')
    plan = save_backdated(account, monkeypatch, strategy='holding_area', amount=150)
    plan.pop('strategy')
    assert fund_plans.save_plan(account, plan | {'note': '待定持仓'})['strategy'] == 'holding_area'
    fee_event(account)
    record_event(account, dict(request_id=str(uuid4()), revision=revision(load(account)),
                               date='2025-01-06', side='cash_transfer', from_strategy='core',
                               strategy='holding_area', amount=200))
    recorded = {row['strategy']: row for row in fund_plans.record_due_subscriptions(account)}
    assert set(recorded) == {'core', 'holding_area'}
    assert fund_plans.record_due_subscriptions(account) == []
    view = account_data(account)
    assert view['cash_by_strategy']['core'] == 690
    assert view['cash_by_strategy']['holding_area'] == 50
    status = fund_plans.dca_status(account)
    assert status['recorded_today_by_strategy'] == {'core': ['2025-01-07'], 'holding_area': ['2025-01-07']}
    with pytest.raises(ValueError, match='属于其他策略'):
        void_event(account, recorded['holding_area']['id'], strategy='core')
    assert void_event(account, recorded['holding_area']['id'])['strategy'] == 'holding_area'
    view = account_data(account)
    assert view['cash_by_strategy']['core'] == 690
    assert view['cash_by_strategy']['holding_area'] == 200
    assert list(view['fund_pending']) == [recorded['core']['id']]


def test_account_fee_covers_first_subscription_for_dca(account, weekend_calendar, monkeypatch):
    from src import portfolio_config

    config = load(account)['_config']
    fee = {'kind': 'none', 'value': 0}
    portfolio_config.update(account, {'config': {**config, 'fund_subscription_fee': fee},
                                      'revision': portfolio_config.revision(config)})
    save_backdated(account, monkeypatch, code='999999')
    recorded, = fund_plans.record_due_subscriptions(account)
    assert recorded['subscription_fee'] == fee
    assert recorded['code'] == '999999'


def test_record_is_idempotent_across_reruns(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch)
    fee_event(account)
    assert len(fund_plans.record_due_subscriptions(account)) == 1
    assert fund_plans.record_due_subscriptions(account) == []
    pendings = [e for e in load(account)['events'] if e['side'] == 'fund_pending']
    assert len(pendings) == 1
    assert pendings[0]['id'].startswith('dca-')


def test_missed_day_is_listed_and_can_be_recorded_manually(account, weekend_calendar, monkeypatch):
    plan = save_backdated(account, monkeypatch, anchor=6)
    fee_event(account)
    before = load(account)
    assert fund_plans.record_due_subscriptions(account) == []
    assert load(account)['events'] == before['events']
    installment = next(row for row in fund_plans.dca_status(account)['plans'][0]['installments']
                       if row['due_date'] == '2025-01-06')
    assert installment['status'] == 'missing'

    payload = dict(id=plan['id'], due_date='2025-01-06', request_id=str(uuid4()),
                   code=plan['code'], strategy='core', plan_amount=plan['amount'], amount=95,
                   revision=revision(before), subscription_fee={'kind': 'none', 'value': 0})
    with pytest.raises(ValueError, match='不在当前计划内'):
        fund_plans.record_installment(account, payload | {'due_date': '2025-01-05'})
    recorded = fund_plans.record_installment(account, payload)
    assert recorded['date'] == '2025-01-07' and recorded['nav_date'] == '2025-01-06'
    assert fund_plans.record_installment(account, payload) == recorded
    with pytest.raises(ValueError, match='已有流水'):
        fund_plans.record_installment(account, payload | {'request_id': str(uuid4())})
    view = wallet.snapshot(load(account), '2025-01-07')
    assert view['cash'] == 895 and view['pending_fund_value'] == 95
    status = fund_plans.dca_status(account)
    assert status['sources'][recorded['id']] == 'manual' and status['recorded_today'] == []
    assert status['plans'][0]['installments'][0]['status'] == 'pending'
    assert status['plans'][0]['installments'][0]['amount'] == 95
    void_event(account, recorded['id'])
    assert fund_plans.dca_status(account)['plans'][0]['installments'][0]['status'] == 'voided'
    assert wallet.snapshot(load(account), '2025-01-07')['cash'] == 990


def test_automatic_run_records_only_today(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, anchor=6)
    save_backdated(account, monkeypatch, anchor=7)
    fee_event(account)
    today, = fund_plans.record_due_subscriptions(account)
    assert today['side'] == 'fund_pending'
    assert today['nav_date'] == today['date'] == '2025-01-07'
    assert fund_plans.record_due_subscriptions(account) == []


def test_deleted_dca_installment_stays_deleted_after_scheduler_retry(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch)
    fee_event(account)
    entry, = fund_plans.record_due_subscriptions(account)
    wallet.delete_fund_subscription(account, {'request_id': str(uuid4()), 'event_id': entry['id'],
                                               'revision': revision(load(account))})
    assert fund_plans.record_due_subscriptions(account) == []
    assert wallet.snapshot(load(account), '2025-01-07')['cash'] == 990
    status = fund_plans.dca_status(account)
    assert status['recorded_today'] == []
    assert status['plans'][0]['installments'][0]['status'] == 'deleted'


def test_same_day_manual_records_require_reconciliation_and_do_not_double_charge(account, weekend_calendar, monkeypatch):
    plan = save_backdated(account, monkeypatch)
    fee_event(account, date='2025-01-07', nav_date='2025-01-07', amount=100)
    status = fund_plans.dca_status(account)
    assert status['plans'][0]['installments'][0]['possible_manual']
    assert fund_plans.record_due_subscriptions(account) == []
    assert wallet.snapshot(load(account), '2025-01-07')['cash'] == 900

    payload = dict(id=plan['id'], due_date='2025-01-07', request_id=str(uuid4()),
                   code=plan['code'], strategy='core', plan_amount=plan['amount'], amount=100,
                   revision=revision(load(account)), subscription_fee={'kind': 'none', 'value': 0})
    # The app shows a distinct second debit, so the user explicitly records it against the plan.
    entry = fund_plans.record_installment(account, payload)
    assert fund_plans.record_due_subscriptions(account) == []
    assert wallet.snapshot(load(account), '2025-01-07')['cash'] == 800
    assert fund_plans.dca_status(account)['sources'][entry['id']] == 'manual'


def test_daily_plan_lists_missed_trading_days_without_recording_them(account, weekend_calendar, monkeypatch):
    monkeypatch.setattr(fund_plans, '_today', lambda: date(2024, 12, 31))
    plan = fund_plans.save_plan(account, {'code': '000051', 'amount': 100, 'frequency': 'daily'})
    assert plan['anchor'] is None
    monkeypatch.setattr(fund_plans, '_today', lambda: date(2025, 1, 7))
    weekdays = fund_plans.get_trading_dates
    monkeypatch.setattr(fund_plans, 'get_trading_dates', lambda start, end: [
        day for day in weekdays(start, end) if day != date(2025, 1, 1)
    ])
    fee_event(account)

    events = fund_plans.record_due_subscriptions(account)
    assert [event['nav_date'] for event in events] == ['2025-01-07']
    installments = fund_plans.dca_status(account)['plans'][0]['installments']
    assert [(row['due_date'], row['status']) for row in installments] == [
        ('2025-01-07', 'pending'), ('2025-01-06', 'missing'),
    ]
    assert fund_plans.record_due_subscriptions(account) == []


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


def test_latest_result_tracks_failure_and_retry_per_plan(account, weekend_calendar, monkeypatch):
    first = save_backdated(account, monkeypatch)
    second = save_backdated(account, monkeypatch, amount=950)

    def results():
        return {plan['id']: plan['latest_result'] for plan in fund_plans.dca_status(account)['plans']}

    assert results() == {first['id']: None, second['id']: None}
    assert fund_plans.record_due_subscriptions(account) == []
    assert '申购费率' in results()[first['id']]['reason']
    fee_event(account)
    assert len(fund_plans.record_due_subscriptions(account)) == 1
    latest = results()
    assert latest[first['id']] == {'status': 'recorded', 'due_date': '2025-01-07', 'date': '2025-01-07'}
    assert latest[second['id']]['status'] == 'failed'
    assert '现金不足' in latest[second['id']]['reason']
    save(account, id=second['id'], amount=950, note='保留失败原因')
    assert results()[second['id']] == latest[second['id']]


def test_cash_shortage_fails_one_plan_others_continue(account, weekend_calendar, monkeypatch, caplog):
    save_backdated(account, monkeypatch, code='000051', amount=600.0, note='A')
    save_backdated(account, monkeypatch, code='000124', amount=600.0, note='B')
    fee_event(account)
    fee_event(account, code='000124', qty=2, amount=20)
    with caplog.at_level(logging.WARNING, logger='src.fund_plans'):
        recorded = fund_plans.record_due_subscriptions(account)
    assert len(recorded) == 1
    assert recorded[0]['amount'] == 600.0
    assert any('现金不足' in row.message for row in caplog.records)
    assert sum(e['amount'] for e in load(account)['events'] if e['side'] == 'fund_pending') == 600.0


def test_disabled_plan_is_skipped(account, weekend_calendar, monkeypatch):
    save_backdated(account, monkeypatch, enabled=False)
    fee_event(account)
    assert fund_plans.record_due_subscriptions(account) == []
    assert fund_plans.dca_status(account)['plans'][0]['next_due'] is None


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
    save_backdated(account, monkeypatch, anchor=5)
    record_event(account, dict(request_id=str(uuid4()), revision=revision(load(account)),
                               date='2025-01-07', side='fund_pending', code='000051',
                               amount=50, nav_date='2024-12-20',
                               subscription_fee={"kind": "none", "value": 0}))
    with caplog.at_level(logging.WARNING, logger='src.fund_plans'):
        fund_plans.record_due_subscriptions(account)
    assert any('未确认份额' in row.message for row in caplog.records)


@pytest.mark.parametrize('frequency', ['monthly', 'daily'])
def test_calendar_unavailable_skips_recording(account, weekend_calendar, monkeypatch, frequency):
    save_backdated(account, monkeypatch, anchor=5, frequency=frequency)
    fee_event(account)
    monkeypatch.setattr('src.fund_plans.get_trading_dates', lambda start, end: [])
    assert fund_plans.record_due_subscriptions(account) == []
    assert not [e for e in load(account)['events'] if e['side'] == 'fund_pending']
    assert fund_plans.dca_status(account)['plans'][0]['next_due'] is None
