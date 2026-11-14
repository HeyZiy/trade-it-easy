"""验证正趋势分同时约束新仓和旧仓。"""
import numpy as np
import pytest

from test_platform_pool_rotation_v6 import start, world  # noqa: F401


def rising_world(world):
    stub, strategy, codes, calendar = start(world, 'pool_rotation_v1_1.py')
    for index, code in enumerate(codes):
        stub.closes[code].iloc[:] = 10 * np.exp((8 - index) * .00004 * np.arange(len(calendar)))
    return stub, strategy, codes, calendar


def test_trend_gate_filters_pool_before_taking_top_six(world):
    stub, strategy, codes, calendar = rising_world(world)
    stub.closes[codes[0]].loc[calendar[269]] *= .96
    flat = stub.closes[codes[1]]
    flat.loc[calendar[244]:calendar[269]] = flat.loc[calendar[244]]

    stub.run_days(calendar[270:271])

    assert strategy._held(stub.ctx) == set(codes[2:8])
    assert not stub.exports and not stub.records


@pytest.mark.parametrize('price_factor,zero', [(.94, True), (.96, False)])
def test_known_nonpositive_score_sells_at_next_session(world, price_factor, zero):
    stub, strategy, codes, calendar = rising_world(world)
    close = stub.closes[codes[0]]
    close.loc[calendar[270]] *= price_factor
    stub.run_days(calendar[270:271])
    assert codes[0] in strategy._held(stub.ctx)
    score = strategy.calculate_momentum_score(close.loc[:calendar[270]].tail(26).values)
    assert score == 0 if zero else score < 0

    stub.run_days(calendar[271:272])

    assert strategy._held(stub.ctx) == set(codes[1:7])
    assert [order[:3] for order in stub.orders[-2:]] == [
        ('S', str(calendar[271].date()), codes[0]),
        ('B', str(calendar[271].date()), codes[6]),
    ]
    assert any('momentum_nonpositive' in line for line in stub.logs)


def test_missing_score_keeps_holding_but_confirmed_financial_failure_still_sells(world):
    stub, strategy, codes, calendar = rising_world(world)
    stub.run_days(calendar[270:271])
    history = strategy.history

    def incomplete_history(count, *args, **kwargs):
        frame = history(count, *args, **kwargs)
        if count == 26 and codes[0] in frame:
            frame.loc[frame.index[-1], codes[0]] = np.nan
        return frame

    strategy.history = incomplete_history
    stub.run_days(calendar[271:272])
    assert strategy._held(stub.ctx) == set(codes[:6])
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.
    stub.run_days(calendar[272:273])
    assert strategy._held(stub.ctx) == set(codes[1:7])
