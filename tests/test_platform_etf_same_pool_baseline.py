"""同池基线的历史可用性、实际下单约束与年度收益/回撤口径。"""
import importlib.util
import sys
import types
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


SCRIPT = (Path(__file__).resolve().parents[1] / 'research/studies/'
          'etf_rotation/same_pool_baseline.py')

# 原8只排名策略已归档；冻结历史名单，不随当前15/37只研究池扩充。
ORIGINAL8 = (
    '510300.XSHG', '159915.XSHE', '515180.XSHG', '513500.XSHG',
    '513100.XSHG', '518880.XSHG', '511090.XSHG', '511010.XSHG',
)


@pytest.fixture
def world(monkeypatch):
    api = types.ModuleType('jqdata')
    api.g = types.SimpleNamespace()
    # 模拟聚宽通配导入遮蔽内置函数；统计和资金约束仍须成立。
    api.max, api.min, api.sum, api.all = np.max, np.min, np.sum, np.all
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    spec = importlib.util.spec_from_file_location('same_pool_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    logs, orders, metadata_dates = [], [], []
    module.log = types.SimpleNamespace(info=logs.append, error=logs.append)
    for name in ('set_benchmark', 'set_option', 'set_order_cost', 'set_slippage',
                 'run_daily', 'record'):
        setattr(module, name, lambda *args, **kwargs: None)
    module.OrderCost = lambda **kwargs: kwargs
    module.PriceRelatedSlippage = lambda spread: spread
    portfolio = types.SimpleNamespace(starting_cash=100000.0, total_value=100000.0,
                                      available_cash=100000.0, positions={})
    context = types.SimpleNamespace(current_dt=datetime(2016, 1, 4, 10, 40),
                                    previous_date=date(2015, 12, 31), portfolio=portfolio)
    prices = {code: 1.0 for code in module.ETF_POOL}
    paused, failed_sells = set(), set()
    starts = {code: date(2010, 1, 1) for code in module.ETF_POOL}
    history = {code: [1.0] * 25 for code in module.ETF_POOL}

    def security(code, date=None):
        metadata_dates.append(date)
        return types.SimpleNamespace(start_date=starts[code], end_date=date_max,
                                     display_name=code)

    date_max = date(2200, 1, 1)
    module.get_security_info = security
    module.attribute_history = lambda code, *args, **kwargs: pd.DataFrame({'close': history[code]})
    module.get_current_data = lambda: {
        code: types.SimpleNamespace(last_price=prices[code], paused=code in paused)
        for code in module.ETF_POOL
    }

    def order_target(code, target):
        pos = portfolio.positions.get(code)
        held = pos.total_amount if pos else 0
        delta = target - held
        orders.append((code, delta))
        if delta < 0 and code in failed_sells:
            return types.SimpleNamespace(filled=0)
        assert delta != 0
        value = abs(delta) * prices[code]
        fee = max(5.0, value * module.COMMISSION)
        if delta > 0:
            assert delta % 100 == 0
            assert value + fee <= portfolio.available_cash + 1e-9
            portfolio.available_cash -= value + fee
        else:
            assert -delta <= pos.closeable_amount
            portfolio.available_cash += value - fee
        portfolio.positions[code] = types.SimpleNamespace(
            total_amount=target, closeable_amount=target, value=target * prices[code])
        portfolio.total_value -= fee
        return types.SimpleNamespace(filled=abs(delta))

    module.order_target = order_target
    module.initialize(context)
    return types.SimpleNamespace(module=module, ctx=context, prices=prices, paused=paused,
                                 starts=starts, history=history, orders=orders, logs=logs,
                                 metadata_dates=metadata_dates, failed_sells=failed_sells)


def test_same_pool_and_historical_entry_gate(world):
    w = world
    codes = w.module.ETF_POOL
    assert tuple(codes) == ORIGINAL8
    w.starts[codes[-1]] = date(2023, 1, 1)
    w.history[codes[-2]] = [1.0] * 24
    w.history[codes[-3]] = [1.0] * 24 + [np.nan]
    assert w.module.eligible_members(w.ctx) == list(codes[:-3])
    assert all(value == w.ctx.previous_date for value in w.metadata_dates)
    assert len(codes) == 8


def test_monthly_equal_weight_and_new_member_waits_until_next_month(world):
    w = world
    late = w.module.ETF_POOL[-1]
    w.history[late] = [1.0] * 24
    w.module.rebalance_task(w.ctx)
    assert late not in w.ctx.portfolio.positions
    quantities = [p.total_amount for p in w.ctx.portfolio.positions.values()]
    assert len(quantities) == 7 and len(set(quantities)) == 1
    before = list(w.orders)
    w.history[late] = [1.0] * 25
    w.ctx.current_dt = datetime(2016, 1, 5, 10, 40)
    w.module.rebalance_task(w.ctx)
    assert w.orders == before
    w.ctx.current_dt = datetime(2016, 2, 1, 10, 40)
    w.ctx.previous_date = date(2016, 1, 29)
    w.module.rebalance_task(w.ctx)
    assert w.ctx.portfolio.positions[late].total_amount > 0
    assert w.ctx.portfolio.available_cash >= 0


def test_paused_member_budget_is_not_redistributed(world):
    w = world
    code = w.module.ETF_POOL[-1]
    w.paused.add(code)
    w.module.rebalance_task(w.ctx)
    assert code not in w.ctx.portfolio.positions
    assert all(p.total_amount <= 12500 for p in w.ctx.portfolio.positions.values())
    # 其他七只的最低佣金也从现金扣除；不把费用误认为重新分配预算。
    assert w.ctx.portfolio.available_cash == pytest.approx(12500 - 7 * 5)


def test_failed_sell_cannot_finance_new_buys(world):
    w = world
    code = w.module.ETF_POOL[0]
    w.ctx.portfolio.positions[code] = types.SimpleNamespace(
        total_amount=100000, closeable_amount=100000, value=100000.0)
    w.ctx.portfolio.available_cash = 0.0
    w.failed_sells.add(code)
    w.module.rebalance_task(w.ctx)
    assert w.orders == [(code, -87500)]
    assert w.ctx.portfolio.positions[code].total_amount == 100000
    assert w.ctx.portfolio.available_cash == 0


def test_t1_locked_position_is_not_sold(world):
    w = world
    code = w.module.ETF_POOL[0]
    w.ctx.portfolio.positions[code] = types.SimpleNamespace(
        total_amount=100000, closeable_amount=0, value=100000.0)
    w.ctx.portfolio.available_cash = 0.0
    w.module.rebalance_task(w.ctx)
    assert w.orders == []


def test_year_return_drawdown_and_actual_positions_include_cash_days(world):
    w = world
    code = w.module.ETF_POOL[0]

    def close(day, total, invested=0.0):
        w.ctx.current_dt = datetime.fromisoformat(day + 'T15:01:00')
        w.ctx.portfolio.total_value = total
        w.ctx.portfolio.positions = ({code: types.SimpleNamespace(
            total_amount=100, value=invested)} if invested else {})
        w.module.after_market_close(w.ctx)

    close('2016-12-29', 90000, 45000)
    close('2016-12-30', 110000)
    close('2017-01-03', 99000)
    w.module.after_market_close(w.ctx)  # 同日重复调用不能重复计数。
    first, second = w.module.g.year_stats[2016], w.module.g.year_stats[2017]
    assert first['start'] == 100000 and first['end'] == 110000
    assert first['drawdown'] == pytest.approx(-.1)
    assert first['holding_days'] == {code: 1}
    assert first['weight_sum'][code] / first['days'] == pytest.approx(.25)
    assert first['cash_weight_sum'] / first['days'] == pytest.approx(.75)
    assert second['start'] == 110000 and second['days'] == 1
    assert second['drawdown'] == pytest.approx(-.1)
    w.module.on_strategy_end(w.ctx)
    assert any('年度收益率: +10.00%' in line for line in w.logs)
    assert any('年度收益率: -10.00%' in line for line in w.logs)
    assert any('全程最大回撤: 10.00%' in line for line in w.logs)


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), 0.0])
def test_invalid_nav_is_not_reported_as_a_loss(world, invalid):
    world.ctx.portfolio.total_value = invalid
    with pytest.raises(ValueError, match='账户净值无效'):
        world.module.after_market_close(world.ctx)
    assert world.module.g.year_stats == {}
