"""验证聚宽 v6 的日检、实际空位和新仓波动率门槛。"""
from test_platform_quality_pool_v2 import world  # noqa: F401


def start(world, script='pool_rotation.py'):
    create, calendar = world
    stub, strategy, codes = create(script)
    for code in codes:
        stub.quote_overrides[code] = {'low_limit': .01}
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
