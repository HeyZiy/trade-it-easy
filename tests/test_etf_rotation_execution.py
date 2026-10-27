"""Execution accounting: distributions, unavailable quotes and net sale proceeds."""
from datetime import date

import pandas as pd
import pytest

import etf_rotation as entry
from data_provider.fund_actions import adjusted_close
from src.etf.rotation import ACCOUNT, RotationRow, plan_rotation
from src.trade_ledger import BatchOrder, TradeRecord, append_trade, derive, execute_batch, load_trades
from src.etf.rotation import dividend_records
from data_provider.fund_actions import get_fund_events


def test_cash_dividend_does_not_create_false_price_drop_or_use_future_event():
    raw = pd.Series([10., 10., 9., 9.], index=pd.date_range('2026-09-01', periods=4))
    events = [dict(date='2026-09-03', cash_per_share=1., split_ratio=1.),
              dict(date='2026-09-05', cash_per_share=2., split_ratio=1.)]
    result = adjusted_close(raw, events, date(2026, 9, 4))
    assert result.tolist() == [9., 9., 9., 9.]
    # Ex-date can be today before a daily bar exists: today's quote uses ex-dividend units.
    assert adjusted_close(raw.iloc[:2], events, date(2026, 9, 3)).tolist() == [9., 9.]


def test_fee_is_deducted_from_cash_and_legacy_records_remain_fee_free(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    append_trade(TradeRecord('2026-09-01', '510300', 'ETF', 'buy', 100, 10., account=ACCOUNT), path)
    result = execute_batch([BatchOrder('510300', 'ETF', 'sell', 100, 11.)],
                           account=ACCOUNT, trade_date='2026-09-02', path=path,
                           commission_rate=.0002, min_commission=5.)
    assert result.abort is None
    assert load_trades(path)[-1].fee == 5.
    assert derive(load_trades(path), '2026-09-02').cash == 1_000_095.


def test_fee_shortfall_rejects_entire_batch_before_any_write(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    orders = [BatchOrder('510300', 'ETF', 'buy', 100000, 10.)]
    result = execute_batch(orders, account=ACCOUNT, trade_date='2026-09-02', path=path,
                           commission_rate=.0002, min_commission=5.)
    assert result.abort and not path.exists()


def test_switch_uses_sale_proceeds_after_selling_fee():
    row = RotationRow('510300', 'ETF', '股票', 1., 1., 1., True, 1)
    held = [dict(code='513100', name='旧仓', count=1000, current_price=10., account=ACCOUNT)]
    plan = plan_rotation([row], held, {'513100': 10.}, 7.)
    buy = plan['buys'][0]
    assert buy.amount + max(5., buy.amount * .0002) <= 10007. - 5.


def test_dividend_entitlement_survives_sale_and_is_not_available_before_payment():
    trades = [TradeRecord('2026-09-01', '510300', 'ETF', 'buy', 100, 10., account=ACCOUNT),
              TradeRecord('2026-09-03', '510300', 'ETF', 'sell', 100, 9., account=ACCOUNT)]
    event = dict(date='2026-09-03', record_date='2026-09-02', pay_date='2026-09-07',
                 cash_per_share=1., split_ratio=1.)
    records = dividend_records(trades, {'510300': [event]}, '2026-09-04')
    assert len(records) == 1
    before = derive(trades + records, '2026-09-04')
    assert before.cash == 999900. and before.receivables == 100.
    assert before.total_assets == 1000000.
    assert derive(trades + records, '2026-09-07').cash == 1000000.
    assert dividend_records(trades + records, {'510300': [event]}, '2026-09-07') == []
    # Buying on the ex-date does not confer entitlement.
    late = [TradeRecord('2026-09-03', '510300', 'ETF', 'buy', 100, 9., account=ACCOUNT)]
    assert dividend_records(late, {'510300': [event]}, '2026-09-04') == []


def test_missing_realtime_price_never_executes_at_fallback_close(monkeypatch, tmp_path):
    path = tmp_path / 'ledger.jsonl'
    row = RotationRow('510300', 'ETF', '股票', 10., 10., 1., True, 1)
    diag = dict(n=1, pool=15, eligible=1, realtime_missing=1, prev_trading_day='2026-09-02')
    monkeypatch.setattr(entry, 'LEDGER_PATH', path)
    monkeypatch.setattr(entry, 'rotation_snapshot', lambda **kwargs: ([row], {}, diag))
    report = entry.run(trade_date='2026-09-03')
    assert not path.exists()
    assert '本次不执行' in report and '实时价' in report


def test_public_dividend_dates_are_preserved_and_success_is_cached(monkeypatch, tmp_path):
    import requests
    from types import SimpleNamespace
    html = '''<table><tr><th>权益登记日</th><th>除息日</th><th>每10份分红</th><th>分红发放日</th></tr>
    <tr><td>2026-09-02</td><td>2026-09-03</td><td>每10份派现金1.0元</td><td>2026-09-07</td></tr></table>
    暂无拆分信息'''
    response = SimpleNamespace(text=html, raise_for_status=lambda: None)
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        return response
    monkeypatch.setattr(requests, 'get', fetch)
    events = get_fund_events('510300', '2026-09-03', cache_dir=tmp_path)
    assert events == [dict(date='2026-09-03',cash_per_share=.1,split_ratio=1.,
                           record_date='2026-09-02',pay_date='2026-09-07')]
    assert get_fund_events('510300', '2026-09-03', cache_dir=tmp_path) == events
    assert len(calls) == 1


def test_entry_books_dividend_once_and_dry_run_never_changes_ledger(monkeypatch, tmp_path):
    path = tmp_path / 'ledger.jsonl'
    append_trade(TradeRecord('2026-09-01','510300','ETF','buy',100,10.,account=ACCOUNT),path)
    event = dict(date='2026-09-03',record_date='2026-09-02',pay_date='2026-09-07',
                 cash_per_share=1.,split_ratio=1.)
    row = RotationRow('510300','ETF','股票',9.,9.,1.,True,1)
    diag = dict(n=1,pool=15,eligible=1,realtime_missing=0,prev_trading_day='2026-09-02',
                dividends={'510300':[event]})
    monkeypatch.setattr(entry,'LEDGER_PATH',path)
    monkeypatch.setattr(entry,'rotation_snapshot',lambda **kwargs:([row],{'510300':9.},diag))
    before = path.read_bytes()
    entry.run(dry_run=True,trade_date='2026-09-03')
    assert path.read_bytes() == before
    entry.run(trade_date='2026-09-03')
    entry.run(trade_date='2026-09-03')
    trades=load_trades(path)
    assert [t.side for t in trades] == ['buy','dividend']
    assert derive(trades,'2026-09-03',{'510300':9.}).total_assets == 1_000_000.
