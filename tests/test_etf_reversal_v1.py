"""天级版离线行为验证；不代表收益有效。"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/etf/reversal_v1.py'


def load():
    sys.modules.setdefault('jqdata', ModuleType('jqdata'))
    spec = importlib.util.spec_from_file_location('reversal_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bars(closes=None):
    closes = np.array([100.] * 20 + [90., 96.] if closes is None else closes)
    return pd.DataFrame({'open': closes, 'high': closes + 1, 'low': closes - 1,
                         'close': closes, 'money': 50_000_000., 'paused': 0},
                        index=pd.bdate_range(end='2026-11-06', periods=len(closes)))


def test_two_day_reclaim_uses_ma_atr_and_fixed_two_day_low():
    m = load()
    signal = m.entry_signal(bars())
    assert signal is not None
    assert signal['previous_d'] < -2 <= signal['d']
    assert signal['ma'] == pytest.approx(99.3)
    assert signal['low_ratio'] == pytest.approx(89 / 96)
    assert signal['signal_date'] == '2026-11-06'


@pytest.mark.parametrize('kind', ['nan', 'inf', 'negative', 'ohlc', 'paused', 'missing', 'duplicate', 'zero_atr'])
def test_invalid_bars_cannot_trigger(kind):
    frame = bars()
    if kind in ('nan', 'inf', 'negative'):
        frame.iloc[3, frame.columns.get_loc('close')] = {'nan': np.nan, 'inf': np.inf, 'negative': -1}[kind]
    elif kind == 'ohlc':
        frame.iloc[-1, frame.columns.get_loc('high')] = 1
    elif kind == 'paused':
        frame.iloc[-2, frame.columns.get_loc('paused')] = 1
    elif kind == 'missing':
        frame = frame.iloc[1:]
    elif kind == 'duplicate':
        frame.index = list(frame.index[:-1]) + [frame.index[-2]]
    else:
        frame[['open', 'high', 'low', 'close']] = 100.
    assert load().entry_signal(frame) is None


@pytest.mark.parametrize('tail', [[90., 89.], [96., 96.], [100., 100.]])
def test_no_falling_knife_or_old_recovery(tail):
    assert load().entry_signal(bars([100.] * 20 + tail)) is None


@pytest.mark.parametrize('price,allowed', [(96., True), (99.3, False), (101., False), (88., False), (np.nan, False), (0., False)])
def test_live_entry_does_not_chase_or_buy_below_structure(price, allowed):
    m = load()
    assert m.entry_allowed(m.entry_signal(bars()), price) is allowed


@pytest.mark.parametrize('close,expected', [(88., 'structure'), (101., 'mean'), (96., None)])
def test_close_exits_structure_and_mean(close, expected):
    m = load()
    frame = bars()
    meta = m.entry_signal(frame)
    frame.loc[pd.Timestamp('2026-11-09')] = [close, close + 1, close - 1, close, 5e7, 0]
    assert m.exit_reason(frame, meta) == expected


def test_stop_reference_rebuilds_after_corporate_action():
    m = load()
    frame = bars()
    meta = m.entry_signal(frame)
    frame.loc[pd.Timestamp('2026-11-09')] = [95, 96, 94, 95, 5e7, 0]
    frame[['open', 'high', 'low', 'close']] *= .5
    assert m.exit_reason(frame, meta) is None
    frame.iloc[-1, frame.columns.get_loc('close')] = 44
    frame.iloc[-1, frame.columns.get_loc('low')] = 43
    assert m.exit_reason(frame, meta) == 'structure'


def pool_inputs():
    names = ['沪深300ETF', '沪深300增强ETF', '中证500ETF', '未映射ETF']
    secs = pd.DataFrame({'type': 'etf', 'display_name': names,
                         'start_date': pd.Timestamp('2020-01-01')}, index=['A', 'B', 'C', 'U'])
    maps = pd.DataFrame([
        dict(code=c, pub_date='2026-01-01', start_date='2026-01-01', end_date=None,
             traced_index_code=k, traced_index_name=n)
        for c, k, n in [('A', '000300.XSHG', '沪深300'), ('B', None, '沪深300（000300）'),
                        ('C', '000905', '中证500'), ('U', None, None)]])
    sessions = bars().index[-20:]
    money = {c: pd.Series(5e7, index=sessions) for c in secs.index}
    money['B'] *= 2
    return secs, maps, money, sessions


def pool_world(m, secs, maps, money):
    """桩替平台接口；代码与公告日过滤下推到 finance.run_query，由桩模拟。"""
    calls = []

    class Field:
        def in_(self, values):
            calls.append(('codes', list(values)))
            self.codes = list(values)
            return self

        def __le__(self, value):
            calls.append(('pub_date', value))
            self.cutoff = value
            return self

    table = NS(code=Field(), pub_date=Field())

    def run_query(q):
        out = maps.copy()
        for field in q:
            if hasattr(field, 'codes'):
                out = out[out['code'].isin(field.codes)]
            if hasattr(field, 'cutoff'):
                out = out[pd.to_datetime(out['pub_date']) <= pd.Timestamp(field.cutoff)]
        return out

    m.finance = NS(FUND_INVEST_TARGET=table, run_query=run_query)
    m.query = lambda t: NS(filter=lambda *filters: filters)
    m.get_all_securities = lambda types, date: (calls.append(('securities', types, date)) or secs)

    def prices(codes, **kwargs):
        calls.append(('price', kwargs))
        return pd.concat([pd.DataFrame({'time': money[c].index, 'code': c, 'money': money[c].values})
                          for c in codes], ignore_index=True)

    m.get_price = prices
    m.log = NS(info=lambda text: calls.append(('log', text)))
    return calls


def test_historical_pool_dedupes_by_explicit_index_and_keeps_provenance():
    m = load()
    secs, maps, money, sessions = pool_inputs()
    calls = pool_world(m, secs, maps, money)
    selected = m.build_pool('2026-11-06')
    assert [r['code'] for r in selected] == ['B', 'C']
    assert selected[0]['index_key'] == selected[0]['pool_key'] == '000300'
    assert selected[0]['mapping_source'] == 'parsed_code'
    assert 'U' not in [r['code'] for r in selected]
    assert ('securities', ['fund'], '2026-11-06') in calls
    assert ('pub_date', '2026-11-06') in calls
    price_call = next(v[1] for v in calls if v[0] == 'price')
    assert price_call['count'] == 20 and price_call['end_date'] == '2026-11-06'


@pytest.mark.parametrize('change', ['future_pub', 'future_effective', 'expired', 'young', 'low_money'])
def test_pool_rejects_unknown_or_ineligible_without_merging(change):
    m = load()
    secs, maps, money, sessions = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    if change == 'future_pub':
        maps['pub_date'] = '2026-11-07'
    elif change == 'future_effective':
        maps['start_date'] = '2026-11-07'
    elif change == 'expired':
        maps['end_date'] = '2026-11-06'
    elif change == 'young':
        secs.loc['A', 'start_date'] = pd.Timestamp('2026-01-01')
    else:
        money['A'] *= .1
    pool_world(m, secs, maps, money)
    assert m.build_pool('2026-11-06') == []


def test_latest_publication_must_also_be_effective():
    m = load()
    secs, maps, money, sessions = pool_inputs()
    later = maps[maps.code == 'A'].copy()
    later['pub_date'], later['start_date'], later['traced_index_code'] = '2026-10-01', '2026-12-01', '000905'
    maps = pd.concat([maps, later])
    money['B'] = money['A'] * 0.9
    money['C'] = money['A'] * 0.8
    pool_world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06')
    assert [r['code'] for r in rows] == ['A', 'C']
    assert rows[0]['index_key'] == '000300'


def test_pool_money_uses_mean_of_available_rows():
    """天级口径：成交额按返回行取均值，不做逐日完整性校验。"""
    m = load()
    secs, maps, money, sessions = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    money['A'] = money['A'].iloc[1:]
    pool_world(m, secs, maps, money)
    assert [r['code'] for r in m.build_pool('2026-11-06')] == ['A']


def test_non_six_digit_official_code_forms_own_group():
    m = load()
    secs, maps, money, sessions = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    maps['traced_index_code'] = 'HSI'
    pool_world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06')
    assert [r['code'] for r in rows] == ['A']
    assert rows[0]['index_key'] == 'HSI' and rows[0]['mapping_source'] == 'unmapped_code'


def test_conflicting_mappings_resolve_to_single_index():
    m = load()
    secs, maps, money, sessions = pool_inputs()
    secs = secs.loc[['A']]
    maps = maps[maps.code == 'A'].copy()
    other = maps.copy()
    other['traced_index_code'] = '000905'
    maps = pd.concat([maps, other])
    pool_world(m, secs, maps, money)
    rows = m.build_pool('2026-11-06')
    assert len(rows) == 1 and rows[0]['index_key'] in ('000300', '000905')


class World:
    def __init__(self):
        self.m = load()
        self.logs, self.buys, self.sells = [], [], []
        self.live, self.orders = {}, {}
        self.spec, self.sell_spec = 'fill', 'fill'
        self.frames = {c: bars() for c in ['A', 'B', 'C', 'D']}
        self.rows = [dict(code=c, pool_key=k, index_key=k, avg_money=5e7)
                     for c, k in zip(self.frames, ['000300', '000905', '000852', '000016'])]
        self.context = NS(current_dt=datetime(2026, 11, 9, 9, 30),
                          previous_date=datetime(2026, 11, 6).date(),
                          portfolio=NS(positions={}, total_value=1e6, available_cash=1e6))
        self.calls = []
        self.m.g = NS()
        for name in ['set_option', 'set_benchmark', 'set_slippage', 'set_order_cost', 'run_daily']:
            setattr(self.m, name, lambda *a, **kw: None)
        self.m.FixedSlippage = lambda x: x
        self.m.OrderCost = lambda **kw: kw
        self.m.log = NS(info=self.logs.append)
        self.m.get_open_orders = lambda: self.live
        self.m.get_trade_days = lambda start_date=None, end_date=None, count=None: pd.bdate_range(start=start_date, end=end_date) if count is None else pd.bdate_range(end=end_date, periods=count)
        self.m.get_price = self.price
        self.m.order = self.buy
        self.m.order_target = self.sell
        self.m.initialize(self.context)
        self.m.build_pool = lambda asof: self.rows

    def window(self, code, kwargs):
        frame = self.frames[code].copy()
        frame = frame[frame.index <= pd.Timestamp(kwargs['end_date'])]
        count = kwargs.get('count')
        return frame if count is None else frame.iloc[-count:]

    def price(self, code, **kwargs):
        self.calls.append(kwargs)
        if isinstance(code, list):
            parts = []
            for c in code:
                f = self.window(c, kwargs).reset_index().rename(columns={'index': 'time'})
                f['code'] = c
                parts.append(f)
            return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return self.window(code, kwargs)

    def hold(self, code='A', amount=1000, entry='2026-11-05'):
        self.context.portfolio.positions[code] = NS(total_amount=amount, closeable_amount=amount, value=amount * 96.)
        signal = self.m.entry_signal(bars())
        self.m.g.book[code] = dict(signal, pool_key='000300', index_key='000300',
                                   entry_date=entry, order_date=entry, exit=None)

    def buy(self, code, amount):
        self.buys.append((code, amount))
        if self.spec == 'reject':
            return None
        filled = amount if self.spec == 'fill' else (100 if self.spec == 'partial' else 0)
        o = NS(order_id=str(len(self.orders)), security=code, amount=amount, filled=filled, is_buy=True, price=96.)
        self.orders[o.order_id] = o
        if filled:
            self.context.portfolio.positions[code] = NS(total_amount=filled, closeable_amount=0, value=filled*96.)
        if self.spec in ('partial', 'pending'):
            self.live[o.order_id] = o
        self.context.portfolio.available_cash -= amount*96.*1.0001
        return o

    def sell(self, code, target):
        self.sells.append(code)
        if self.sell_spec == 'reject':
            return None
        p = self.context.portfolio.positions[code]
        amount = p.total_amount if self.sell_spec == 'fill' else 100
        p.total_amount -= amount
        p.value = p.total_amount*96.
        return NS(order_id='sell', security=code, is_buy=False, filled=amount)

    def run(self):
        self.m.run_reversal(self.context)


def test_real_signal_to_platform_buy_is_small_and_stably_sorted():
    w = World()
    w.run()
    assert [c for c, _ in w.buys] == ['A', 'B', 'C']
    assert all(a * 96 * 1.0001 <= 100_000 for _, a in w.buys)
    assert sum(a * 96 for _, a in w.buys) <= 300_000
    assert all(call['end_date'] == str(w.context.previous_date) and call['fq'] == 'pre'
               and not call['skip_paused'] for call in w.calls)
    w.run()
    assert len(w.buys) == 3


@pytest.mark.parametrize('spec', ['pending', 'partial'])
def test_accepted_buys_not_duplicated_same_day(spec):
    w = World()
    w.spec = spec
    w.rows = w.rows[:3]
    w.run()
    w.run()
    assert len(w.buys) == 3
    assert len(w.m.g.book) == 3


@pytest.mark.parametrize('spec', ['reject', 'partial'])
def test_exit_latches_until_real_flat_even_when_pool_fails(spec):
    w = World()
    w.hold()
    w.context.current_dt = datetime(2026, 11, 10, 9, 30)
    w.context.previous_date = datetime(2026, 11, 9).date()
    w.frames['A'].loc[pd.Timestamp('2026-11-09')] = [88., 89., 87., 88., 5e7, 0]
    w.sell_spec = spec
    w.m.build_pool = lambda asof: (_ for _ in ()).throw(ValueError('pool offline'))
    w.run()
    assert w.sells == ['A']
    assert w.m.g.book['A']['exit'] == 'structure'
    w.frames['A'] = bars()
    w.sell_spec = 'fill'
    w.run()
    assert w.sells == ['A', 'A']
    assert 'A' not in w.m.g.book
    assert w.buys == []


def test_t_plus_one_and_same_day_global_sell_block():
    w = World()
    w.hold(entry='2026-11-09')
    w.m.g.book['A']['exit'] = 'structure'
    w.run()
    assert w.sells == []
    buys_before = len(w.buys)
    w.m.g.book['A']['entry_date'] = '2026-11-05'
    w.run()
    assert w.sells == ['A'] and len(w.buys) == buys_before


def test_representative_switch_does_not_duplicate_existing_index():
    w = World()
    w.hold()
    w.rows = [dict(code='B', pool_key='000300', index_key='000300', avg_money=9e7)]
    w.run()
    assert w.buys == [] and w.sells == []


@pytest.mark.parametrize('bad', ['missing', 'paused_bar', 'nan_close', 'ohlc'])
def test_invalid_candidate_frame_never_bought(bad):
    w = World()
    w.rows = w.rows[:1]
    if bad == 'missing':
        w.frames['A'] = w.frames['A'].iloc[:-1]
    elif bad == 'paused_bar':
        w.frames['A'].iloc[-1, w.frames['A'].columns.get_loc('paused')] = 1
    elif bad == 'nan_close':
        w.frames['A'].iloc[-1, w.frames['A'].columns.get_loc('close')] = np.nan
    else:
        w.frames['A'].iloc[-1, w.frames['A'].columns.get_loc('high')] = 1
    w.run()
    assert w.buys == []


def test_cash_and_existing_exposure_limit_new_orders():
    w = World()
    w.context.portfolio.available_cash = 5000.
    w.run()
    assert w.buys == []
    w.context.portfolio.available_cash = 1e6
    w.hold(amount=3100)
    w.run()
    assert w.buys == []


@pytest.mark.parametrize('reason', ['mean', 'time'])
def test_platform_exit_paths_and_closed_day_count(reason):
    w = World()
    w.hold(entry='2026-10-26' if reason == 'time' else '2026-11-05')
    if reason == 'mean':
        w.context.current_dt = datetime(2026, 11, 10, 9, 30)
        w.context.previous_date = datetime(2026, 11, 9).date()
        w.frames['A'].loc[pd.Timestamp('2026-11-09')] = [101., 102., 100., 101., 5e7, 0]
    w.run()
    assert w.sells == ['A'] and w.buys == []


def test_async_flat_keeps_day_block_and_clears_book():
    w = World()
    w.hold()
    w.m.g.book['A']['exit'] = 'structure'
    w.context.portfolio.positions['A'].total_amount = 0
    w.run()
    assert w.buys == [] and 'A' not in w.m.g.book


def test_late_buy_fill_reconciles_entry_date_at_close():
    w = World()
    w.spec = 'pending'
    w.rows = w.rows[:1]
    w.run()
    o = next(iter(w.live.values()))
    o.filled = 100
    w.context.portfolio.positions['A'] = NS(total_amount=100, closeable_amount=0, value=9600.)
    w.m.after_trading_end(w.context)
    assert w.m.g.book['A']['entry_date'] == '2026-11-09'


@pytest.mark.parametrize('field', ['available_cash', 'total_value'])
def test_unknown_account_numbers_fail_closed(field):
    w = World()
    setattr(w.context.portfolio, field, np.nan)
    w.run()
    assert w.buys == []


def test_same_index_gate_uses_entry_time_index_key():
    w = World()
    w.hold()
    w.m.g.book['A']['index_key'] = '000905'
    w.rows = [dict(code='B', pool_key='000905', index_key='000905', avg_money=9e7)]
    w.run()
    assert w.buys == []
