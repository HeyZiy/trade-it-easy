"""验证聚宽 v6 的日检、实际空位和新仓波动率门槛。"""
import sys
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from jq_fake import RotationStub


BASE = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum'


@pytest.fixture
def world(monkeypatch):
    # 安装桩会替换平台模块；使用monkeypatch恢复其他测试原有的模块。
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    factors = ModuleType('jqfactor')
    monkeypatch.setitem(sys.modules, 'jqfactor', factors)
    cal = pd.bdate_range('2024-01-02', periods=360)

    def create(script):
        codes = ['600%03d.XSHG' % i for i in range(8)]
        closes = {c: pd.Series(10.0, index=cal) for c in codes}
        stub = RotationStub(cal, closes).install()
        stub.set_date_table(pd.DataFrame({
            'code': codes, 'inc_net_profit_year_on_year': [20.0] * len(codes),
            'statDate': ['2024-03-31'] * len(codes), 'pubDate': ['2024-04-01'] * len(codes),
        }), table='indicator')
        stub.set_date_table(pd.DataFrame({
            'code': codes, 'pe_ratio': [20.0] * len(codes), 'pb_ratio': [3.0] * len(codes),
        }), table='valuation')

        def factor_values(securities, factors, end_date, count):
            assert pd.Timestamp(end_date) < stub.today
            assert factors == ['roe_ttm'] and count == 1
            return {'roe_ttm': pd.DataFrame({c: [.16] for c in securities},
                                             index=[pd.Timestamp(end_date)])}

        factors.get_factor_values = factor_values
        stub._module.get_industry = lambda securities, date: {
            c: {'sw_l1': {'industry_code': c, 'industry_name': c}} for c in securities}
        stub.quote_overrides = {}
        original_quotes = stub._module.get_current_data

        def quotes():
            cd = original_quotes()
            # 默认解除涨跌停，专门的受阻场景另行设置。
            for c in codes:
                cd[c].low_limit, cd[c].high_limit = 0.0, 1e9
                for field, value in stub.quote_overrides.get(c, {}).items():
                    setattr(cd[c], field, value)
            return cd

        stub._module.get_current_data = quotes

        def cancel(order):
            for oid, candidate in list(stub.live_orders.items()):
                if candidate is order:
                    del stub.live_orders[oid]
            return order

        stub._module.cancel_order = cancel
        stub.today = cal[270]
        stub.ctx.current_dt = cal[270] + pd.Timedelta(hours=9)
        mod = stub.load_script(BASE / script)
        stub.initialize()
        return stub, mod, codes

    return create, cal


def start(world, script='pool_rotation.py'):
    create, calendar = world
    stub, strategy, codes = create(script)
    for code in codes:
        stub.quote_overrides[code] = {'low_limit': .01}
    if not hasattr(strategy, 'after_trading_end'):
        strategy.after_trading_end = lambda context: None  # 交易桩每天调用这个可选回调。
    return stub, strategy, codes, calendar


def test_volatility_filters_new_entries_and_existing_positions_drift(world):
    stub, strategy, codes, calendar = start(world)
    # 第一名在建仓前已有高波动；第二名在建仓后才出现同样的波动。
    stub.closes[codes[0]].loc[calendar[269]:] *= .65
    stub.closes[codes[1]].loc[calendar[270]:] *= .65
    # 第二名的首日信号用第 269 日收盘，新的波动到下一轮才进入日线窗口。
    stub.run_days(calendar[270:292])

    assert strategy._held(stub.ctx) == set(codes[1:7])
    assert [row[0] for row in stub.orders] == ['B'] * 6
    assert not stub.exports and not stub.records


def test_financial_exit_refills_only_after_full_sale(world):
    stub, strategy, codes, calendar = start(world)
    code = codes[0]
    stub.run_days(calendar[270:271])
    assert strategy._held(stub.ctx) == set(codes[:6])
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.
    stub.sell_caps[code] = 100

    stub.run_days(calendar[271:272])
    assert strategy._held(stub.ctx) == set(codes[:6])
    assert stub.orders[-1][:3] == ('S', str(calendar[271].date()), code)
    assert code in stub.g.exit_pending

    stub.sell_caps.clear()
    stub.run_days(calendar[272:273])
    assert strategy._held(stub.ctx) == set(codes[1:7])
    assert [row[:3] for row in stub.orders[-2:]] == [
        ('S', str(calendar[272].date()), code),
        ('B', str(calendar[272].date()), codes[6]),
    ]


def test_confirmed_exit_survives_missing_entry_history(world):
    stub, strategy, codes, calendar = start(world)
    stub.run_days(calendar[270:271])
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.
    history = strategy.history
    strategy.history = lambda *args, **kwargs: history(*args, **kwargs).iloc[:-1]

    stub.run_days(calendar[271:272])

    assert strategy._held(stub.ctx) == set(codes[1:6])
    assert stub.orders[-1][:3] == ('S', str(calendar[271].date()), codes[0])


def test_ma20_defers_new_entry_without_extending_top_six_or_selling_holdings(world):
    stub, strategy, codes, calendar = start(world, 'pool_rotation_ma20.py')
    stub.closes[codes[0]].loc[calendar[269]] = 9.0
    stub.closes[codes[1]].loc[calendar[270]] = 9.0

    stub.run_days(calendar[270:271])
    assert strategy._held(stub.ctx) == set(codes[1:6])

    stub.run_days(calendar[271:272])
    assert strategy._held(stub.ctx) == set(codes[:6])
    assert [row[0] for row in stub.orders] == ['B'] * 6


@pytest.mark.parametrize('block_sale, financial_exit', [(False, False), (True, False), (True, True)])
def test_ma20_exit_and_next_day_recovery(world, block_sale, financial_exit):
    stub, strategy, codes, calendar = start(world, 'pool_rotation_ma20_exit.py')
    code = codes[0]
    stub.closes[code].loc[calendar[270]] = 9.0
    stub.run_days(calendar[270:271])
    assert strategy._held(stub.ctx) == set(codes[:6])

    if block_sale:
        stub.quote_overrides[code]['low_limit'] = 10.0
    if financial_exit:
        stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.0
    stub.run_days(calendar[271:272])
    assert (code in strategy._held(stub.ctx)) == block_sale
    assert (code in stub.g.trend_exit_pending) == block_sale
    if not block_sale:
        assert stub.orders[-1][:3] == ('S', str(calendar[271].date()), code)
        assert strategy._held(stub.ctx) == set(codes[1:6])

    stub.quote_overrides[code]['low_limit'] = .01
    stub.run_days(calendar[272:273])
    assert code not in stub.g.trend_exit_pending
    assert (code not in strategy._held(stub.ctx)) == financial_exit


EPS_SCRIPT = 'pool_rotation_eps_retention.py'


def eps_portfolio(world, script=EPS_SCRIPT):
    stub, strategy, codes, cal = start(world, script)
    old, new = codes[0], codes[6]
    stub.closes[new] += pd.Series(range(len(cal)), index=cal) / 100
    stub.date_tables['valuation'].loc[6, 'pe_ratio'] = 40.
    stub.run_days(cal[270:271])
    assert strategy._held(stub.ctx) == set(codes[:6])
    stub.date_tables['valuation'].loc[6, 'pe_ratio'] = 20.
    report = stub.date_tables['indicator']
    report.loc[0, ['statDate', 'pubDate']] = ['2024-12-31', str(cal[271].date())]
    return stub, strategy, codes, cal, old, new


@pytest.mark.parametrize('case', ['weak', 'flat', 'growing', 'unknown', 'entry_day', 'limit_up',
                                  'partial_sale', 'buy_rejected', 'delayed_buy', 'tied_momentum',
                                  'industry', 'vacancy'])
def test_eps_replacement_from_signal_through_fills(world, case):
    stub, strategy, codes, cal, old, new = eps_portfolio(world)
    reads = []

    def eps(code, report, cutoff):
        reads.append((code, cutoff))
        assert code == old and report == pd.Timestamp('2024-12-31') and cutoff < stub.today
        return {'growing': (1.01, 1.), 'flat': (1., 1.), 'unknown': None}.get(case, (.9, 1.))

    strategy._read_eps = eps
    if case == 'entry_day':
        stub.date_tables['indicator'].loc[0, 'pubDate'] = str(cal[270].date())
    if case == 'limit_up':
        stub.quote_overrides[new]['high_limit'] = stub.closes[new][cal[272]]
    if case == 'partial_sale':
        stub.sell_caps[old] = 100
    if case == 'buy_rejected':
        stub.reject_buys.add(new)
    if case == 'delayed_buy':
        # 模拟委托返回时的 filled 尚未更新，收盘时账户已出现新仓。
        submit = strategy.order_target
        def delayed(code, shares):
            order = submit(code, shares)
            if code == new and order is not None:
                order.filled = 0
            return order
        strategy.order_target = delayed
    if case == 'tied_momentum':
        stub.closes[new][:] = 10.
    if case == 'industry':
        strategy.get_industry = lambda securities, date: {
            c: {'sw_l1': {'industry_code': 'X' if c in [codes[1], codes[2], new] else c}}
            for c in securities}
    if case == 'vacancy':
        stub.date_tables['indicator'].loc[5, 'inc_net_profit_year_on_year'] = 4.

    stub.run_days(cal[271:272])
    assert not reads and old in strategy._held(stub.ctx)
    stub.run_days(cal[272:273])
    holdings = strategy._held(stub.ctx)
    paired = case in ('weak', 'flat', 'delayed_buy')
    assert (new in holdings) == (paired or case == 'vacancy')
    assert (old in holdings) == (not paired and case != 'buy_rejected')
    assert len(reads) == (case != 'entry_day')
    assert len(strategy.g.fill_pairs) == paired
    if case in ('partial_sale', 'buy_rejected'):
        assert len(stub.orders) == 7 and stub.orders[-1][0] == 'S'
    if case == 'limit_up':
        stub.quote_overrides[new].pop('high_limit')
        stub.run_days(cal[273:274])
        assert new in strategy._held(stub.ctx) and len(reads) == 1
    if case == 'weak':
        def bars(codes, start_date, end_date, **kwargs):
            assert kwargs['fq'] == 'post' and start_date == cal[272].date() and end_date == cal[331].date()
            return pd.DataFrame({'code': codes + codes, 'time': [start_date] * 2 + [end_date] * 2,
                                 'open': [10, 20, 10, 20], 'close': [10, 20, 11, 25], 'paused': 0})
        strategy.get_price = bars
        stub.ctx.current_dt = cal[331]
        strategy.on_strategy_end(stub.ctx)
        assert '平均差=15.00' in stub.logs[-1]


def test_eps_strategy_tracks_control_until_disclosure(world):
    def trades(script):
        stub, strategy, codes, cal, old, new = eps_portfolio(world, script)
        if script == EPS_SCRIPT:
            strategy._read_eps = lambda *args: (1.1, 1.)
        stub.date_tables['indicator'].loc[1, 'inc_net_profit_year_on_year'] = 3.
        stub.run_days(cal[271:276])
        return stub.orders, stub.portfolio.cash
    assert trades(EPS_SCRIPT) == trades('pool_rotation_ma20.py')


def test_rolling_eps_uses_disclosed_comparatives_and_same_share_units(world):
    _, strategy, _, _ = start(world, EPS_SCRIPT)
    report, pub = pd.Timestamp('2024-06-30'), pd.Timestamp('2024-08-20')
    def row(year, month, published, value, kind=0):
        end = pd.Period('%d-%02d' % (year, month), freq='M').end_time.date()
        return (str(year) + '-01-01', str(end), published, value, kind)
    rows = pd.DataFrame([
        row(2024, 6, '2024-08-20', .32), row(2023, 6, '2024-08-20', .30, 1),
        row(2023, 6, '2023-08-15', .60), row(2022, 6, '2023-08-15', .48, 1),
        row(2023, 12, '2024-03-20', 1.40), row(2022, 12, '2024-03-20', 1.20, 1),
        row(2024, 6, '2025-08-20', .50, 1),
    ], columns=['start_date', 'end_date', 'pub_date', 'basic_eps', 'report_type'])
    # 十送十之后的报告已把上年同期从 .60 重列为 .30。此前报告仍须换算。
    actions = pd.DataFrame({'a_xr_date': pd.to_datetime(['2024-04-02', '2024-06-02']),
                            'dividend_ratio': [None, 10.], 'transfer_ratio': [None, None]})
    assert strategy._rolling_eps(rows, actions, report, pub) == (.72, .66)
    assert strategy._rolling_eps(rows.drop(index=3), actions, report, pub) is None
    conflict = pd.concat([rows, rows.iloc[[0]].assign(basic_eps=.41)])
    assert strategy._rolling_eps(conflict, actions, report, pub) is None


@pytest.mark.parametrize('confirm, first_growth, second_growth, other_failure, sell_offset', [
    (False, 9.9, None, False, 271),
    (True, 9.9, 8., False, 274),
    (True, 10., 12., False, None),
    (True, 0., None, False, 271),
    (True, -3., None, False, 271),
    (True, 9.9, None, True, 271),
])
def test_growth_confirmation_replays_two_distinct_reports(
        world, confirm, first_growth, second_growth, other_failure, sell_offset):
    stub, strategy, codes, cal = start(world, 'pool_rotation_ma20.py')
    strategy.CONFIRM_GROWTH_EXIT = confirm
    old, candidate = codes[0], codes[6]
    indicators = stub.date_tables['indicator']
    indicators.loc[0, 'inc_net_profit_year_on_year'] = 10.1
    rows = pd.DataFrame([
        [old, '2024q1', cal[269], 10.1],
        [old, '2024q2', cal[270], first_growth],
        [old, '2024q3', cal[273], second_growth],
    ], columns=indicators.columns[[0, 2, 3, 1]])

    def history(securities, fields, watch_date, count, interval, stat_by_year):
        assert securities == [old]
        assert pd.Timestamp(watch_date) < stub.today
        assert count == 4 and interval == '1q' and stat_by_year is False
        # 同时返回当时尚不可见的行，检查策略会自行按披露日过滤。
        return rows

    strategy.get_history_fundamentals = history
    stub.run_days(cal[270:271])
    indicators.loc[0, ['statDate', 'pubDate', 'inc_net_profit_year_on_year']] = [
        '2024q2', str(cal[270].date()), first_growth]
    indicators.loc[6, 'inc_net_profit_year_on_year'] = 9.9
    if other_failure:
        stub.date_tables['valuation'].loc[0, 'pe_ratio'] = 30.
    stub.run_days(cal[271:274])
    if second_growth is not None:
        indicators.loc[0, ['statDate', 'pubDate', 'inc_net_profit_year_on_year']] = [
            '2024q3', str(cal[273].date()), second_growth]
    stub.run_days(cal[274:276])

    sales = [row[:3] for row in stub.orders if row[0] == 'S']
    assert sales == ([('S', str(cal[sell_offset].date()), old)] if sell_offset else [])
    assert candidate not in strategy._held(stub.ctx)
    assert len(strategy.g.growth_waits) == (second_growth is not None)


def test_growth_confirmation_needs_visible_adjacent_quarter(world):
    stub, strategy, codes, cal = start(world, 'pool_rotation_ma20.py')
    strategy.CONFIRM_GROWTH_EXIT = True
    old = codes[0]
    strategy.get_history_fundamentals = lambda *args, **kwargs: pd.DataFrame({
        'code': [old, old], 'statDate': ['2024q1', '2024q2'],
        'pubDate': [cal[268], cal[272]], 'inc_net_profit_year_on_year': [4., 9.9],
    })
    stub.run_days(cal[270:271])
    stub.date_tables['indicator'].loc[0, ['statDate', 'pubDate', 'inc_net_profit_year_on_year']] = [
        '2024q3', str(cal[270].date()), 8.]
    stub.run_days(cal[271:273])
    assert old in strategy._held(stub.ctx)
    assert (old, pd.Timestamp('2024-09-30')) in strategy.g.growth_missing
    stub.run_days(cal[273:274])
    assert stub.orders[-1][0] == 'B'
    assert ('S', str(cal[273].date()), old) in [row[:3] for row in stub.orders]
