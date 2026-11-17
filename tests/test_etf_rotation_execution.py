"""Execution accounting: official-hfq anchoring, unavailable quotes and net sale proceeds."""
from datetime import date

import pandas as pd

from src.etf.rotation import ACCOUNT, RotationRow, fetch_bars, plan_rotation
from src.trade_ledger import BatchOrder, TradeRecord, append_trade, derive, execute_batch, load_trades


def _em_frame(raw, hfq):
    n = len(raw)
    return pd.DataFrame({
        "date": pd.date_range("2026-08-01", periods=n).strftime("%Y-%m-%d"),
        "close": raw, "hfq_close": hfq,
    })


def test_fetch_bars_rescales_official_hfq_to_latest_raw_units(monkeypatch):
    raw = [10.0] * 20 + [5.0] * 5 + [4.5] * 5   # 1:2 折算 + 每份 0.5 分红
    hfq = [10.0] * 30                            # 官方后复权连续，无跳变
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em",
                        lambda code, end_date: _em_frame(raw, hfq))
    bars = fetch_bars(["510300"], decision_date=date(2026, 9, 1))
    close = bars["510300"]["close"]
    assert close.tolist() == [4.5] * 30          # 折算/分红缺口消失
    assert close.iloc[-1] == raw[-1]             # 最近已完成收盘 = 原始单位


def test_fetch_bars_excludes_bars_after_decision_date(monkeypatch):
    calls = []
    def fake(code, end_date):
        calls.append(end_date)
        return _em_frame([10.0] * 30, [20.0] * 30)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", fake)
    bars = fetch_bars(["510300"], decision_date=date(2026, 8, 20))
    frame = bars["510300"]
    assert calls == ["2026-08-20"]
    assert frame.index.max() == pd.Timestamp("2026-08-20")
    assert frame["close"].tolist() == [10.0] * 20


def test_fetch_bars_skips_code_when_anchor_close_invalid(monkeypatch):
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em",
                        lambda code, end_date: _em_frame([10.0] * 30,
                                                         [20.0] * 29 + [float("nan")]))
    assert fetch_bars(["510300"], decision_date=date(2026, 9, 1)) == {}


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
