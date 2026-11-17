"""Execution accounting: distributions, unavailable quotes and net sale proceeds."""
from datetime import date

import pandas as pd

from data_provider.fund_actions import adjusted_close
from src.etf.rotation import ACCOUNT, RotationRow, plan_rotation
from src.trade_ledger import BatchOrder, TradeRecord, append_trade, derive, execute_batch, load_trades
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
