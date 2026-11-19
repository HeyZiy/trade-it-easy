"""Execution accounting: official-hfq anchoring, unavailable quotes and net sale proceeds."""
from datetime import date

import pandas as pd
import pytest

from data_provider.etf import AMAZINGDATA, EASTMONEY
from data_provider.fetchers.amazingdata import client, market
from src.etf import rotation
from src.etf.rotation import ACCOUNT, RotationRow, fetch_bars, plan_rotation
from src.etf.rotation_advice import make_signal
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
    monkeypatch.setattr(market, "fetch_etf_hfq_bars", lambda *args, **kwargs: pytest.fail("东财合格，不应查备源"))
    monkeypatch.setattr(client, "is_configured", lambda: True)
    bars = fetch_bars(["510300"], decision_date=date(2026, 9, 1))
    assert bars["510300"].source == EASTMONEY
    close = bars["510300"].history["close"]
    assert close.tolist() == [4.5] * 30          # 折算/分红缺口消失
    assert close.iloc[-1] == raw[-1]             # 最近已完成收盘 = 原始单位


def test_fetch_bars_excludes_bars_after_decision_date(monkeypatch):
    calls = []
    def fake(code, end_date):
        calls.append(end_date)
        return _em_frame([10.0] * 30, [20.0] * 30)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", fake)
    bars = fetch_bars(["510300"], decision_date=date(2026, 8, 28))
    frame = bars["510300"].history
    assert calls == ["2026-08-28"]
    assert frame.index.max() == pd.Timestamp("2026-08-28")
    assert frame["close"].tolist() == [10.0] * 28


def test_fetch_bars_skips_code_when_anchor_close_invalid(monkeypatch):
    monkeypatch.setattr(client, "is_configured", lambda: False)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em",
                        lambda code, end_date: _em_frame([10.0] * 30,
                                                         [20.0] * 29 + [float("nan")]))
    assert fetch_bars(["510300"], decision_date=date(2026, 9, 1)) == {}


def test_fetch_bars_anchors_to_confirmed_previous_trading_day(monkeypatch):
    primary = _em_frame([10.] * 28 + [99.] * 2, [20.] * 30)
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", lambda code, end_date: primary)
    bars = fetch_bars(["510300"], date(2026, 8, 31), prev_trading_day=date(2026, 8, 28))
    assert bars["510300"].history.loc["2026-08-28", "close"] == 10.


@pytest.mark.parametrize("problem", ["outage", "short", "stale", "bad_close", "duplicate"])
def test_rotation_replaces_unusable_primary_with_whole_amazingdata_series(monkeypatch, problem):
    primary = _em_frame([10.] * 30, [20.] * 30)
    backup = _em_frame([8.] * 30, list(range(81, 111)))
    affected = "510300"
    healthy = "510500"

    def eastmoney(code, end_date):
        if code == healthy:
            return primary
        if problem == "outage":
            raise TimeoutError("东财不可用")
        if problem == "short":
            return primary.tail(24)
        if problem == "stale":
            return primary.iloc[:-1]
        if problem == "bad_close":
            return primary.assign(hfq_close=primary.hfq_close.mask(primary.index == 24))
        return pd.concat([primary, primary.tail(1)])

    calls = []

    def amazingdata(codes, end_date, *, begin_date, include_raw_close):
        calls.append((codes, end_date, begin_date, include_raw_close))
        return {affected: backup}

    monkeypatch.setattr("data_provider.bars.get_etf_daily_em", eastmoney)
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(market, "fetch_etf_hfq_bars", amazingdata)
    result = fetch_bars([affected, healthy], date(2026, 8, 31), prev_trading_day=date(2026, 8, 30))
    assert calls == [([affected], "2026-08-30", "2026-06-02", True)]
    assert result[affected].source == AMAZINGDATA
    assert result[affected].history.close.tolist() == pytest.approx((backup.hfq_close * (8 / 110)).tolist())
    assert result[healthy].source == EASTMONEY


@pytest.mark.parametrize("backup_ok, has_quote", [(False, True), (True, False), (True, True)])
def test_rotation_signal_requires_qualified_backup_and_live_quote(monkeypatch, backup_ok, has_quote):
    decision = date(2026, 8, 31)
    code, healthy = "510300", "510500"
    full = _em_frame([10.] * 30, [20.] * 30)
    monkeypatch.setattr(rotation, "latest_trading_day_on_or_before", lambda *args, **kw: date(2026, 8, 30))
    monkeypatch.setattr(rotation, "fetch_prices", lambda codes: {
        requested: 10. for requested in codes if requested == healthy or has_quote
    })
    monkeypatch.setattr("data_provider.bars.get_etf_daily_em",
                        lambda requested, end_date: full if requested == healthy else full.tail(24))
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(market, "fetch_etf_hfq_bars", lambda *args, **kw: {code: full if backup_ok else full.tail(23)})

    pool = [(requested, requested, "国内权益") for requested in (code, healthy)]
    rows, prices, diag = rotation.rotation_snapshot(decision, pool=pool)
    signal = make_signal(decision.isoformat(), rows, prices, diag)
    assert bool(signal["issues"]) == (not backup_ok or not has_quote)
    assert diag["bar_sources"].get(code) == (AMAZINGDATA if backup_ok else None)
    assert diag["missing_bars"] == ([] if backup_ok else [code])


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
