"""用三仓账户验证排名退出及其成交结果。"""
import csv
import importlib.util
import io
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/industry_momentum/study_rank_exit_jq.py'


def account_world(monkeypatch, mode, held_rank=10, sell_fill=1000):
    api = ModuleType('jqdata')
    api.g = SimpleNamespace()
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    spec = importlib.util.spec_from_file_location('rank_exit_under_test', SCRIPT)
    strategy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strategy)

    codes = ['510%03d.XSHG' % i for i in range(1, 11)]
    old = codes[held_rank - 1]
    prices = dict.fromkeys(codes, 10.0)
    positions = {c: SimpleNamespace(total_amount=1000, avg_cost=10.0, value=10000.0)
                 for c in [codes[0], codes[1], old]}
    portfolio = SimpleNamespace(positions=positions, available_cash=70000.0,
                                total_value=100000.0, starting_cash=100000.0)
    context = SimpleNamespace(current_dt=datetime(2026, 7, 2, 14, 55), portfolio=portfolio)
    orders, files = {}, {}

    def mark_account():
        for code, position in positions.items():
            position.value = position.total_amount * prices[code]
        portfolio.total_value = portfolio.available_cash + sum(p.value for p in positions.values())

    def execute(code, amount, is_buy):
        filled = amount if is_buy else min(amount, sell_fill)
        if filled == 0:
            return None
        price = prices[code]
        fee = filled * price * .0001
        position = positions.setdefault(code, SimpleNamespace(total_amount=0, avg_cost=0.0, value=0.0))
        if is_buy:
            cost = filled * price + fee
            assert cost <= portfolio.available_cash + 1e-8
            position.avg_cost = (position.avg_cost * position.total_amount + cost) / (position.total_amount + filled)
            position.total_amount += filled
            portfolio.available_cash -= cost
        else:
            position.total_amount -= filled
            portfolio.available_cash += filled * price - fee
            if position.total_amount == 0:
                position.avg_cost = 0.0
        mark_account()
        order_id = str(len(orders) + 1)
        result = SimpleNamespace(order_id=order_id, security=code, amount=amount,
                                 filled=filled, price=price, commission=fee, is_buy=is_buy)
        orders[order_id] = result
        return result

    def history(count, unit, field, security_list):
        return pd.DataFrame({c: [60000000.0] * count for c in security_list})

    def attribute_history(code, count, *args, **kwargs):
        # 十只均为正趋势，真实评分依次下降；旧仓不触发任何自身退出。
        slope = (11 - codes.index(code)) * .0002
        closes = 10.0 * np.exp(slope * (np.arange(count) - count))
        return pd.DataFrame({'close': closes})

    def write_file(name, text, append):
        files[name] = (files.get(name, '') if append else '') + text

    strategy.RANK_EXIT_PCT = mode
    strategy.set_option = strategy.set_benchmark = strategy.set_slippage = lambda *a, **k: None
    strategy.set_order_cost = strategy.run_daily = lambda *a, **k: None
    strategy.OrderCost = lambda **k: k
    strategy.FixedSlippage = lambda x: x
    strategy.log = SimpleNamespace(info=lambda *a: None, set_level=lambda *a: None)
    strategy.get_security_info = lambda c: SimpleNamespace(display_name=c)
    strategy.get_current_data = lambda: {c: SimpleNamespace(last_price=p, paused=False)
                                         for c, p in prices.items()}
    strategy.get_orders = lambda: orders
    strategy.history, strategy.attribute_history = history, attribute_history
    strategy.write_file = write_file
    strategy.order_target = lambda c, target: execute(c, positions[c].total_amount, False)
    strategy.order = lambda c, amount: execute(c, amount, True)
    strategy.initialize(context)
    strategy.g.pool = codes.copy()
    strategy.g.day = 1  # 已完成建池，检查一个普通交易日的轮动。
    return SimpleNamespace(strategy=strategy, context=context, codes=codes, old=old,
                           prices=prices, orders=orders, files=files, mark=mark_account)


def held_codes(world):
    return {c for c, p in world.context.portfolio.positions.items() if p.total_amount > 0}


@pytest.mark.parametrize('mode', [None, .40])
def test_same_three_holdings_with_and_without_rank_exit(monkeypatch, mode):
    world = account_world(monkeypatch, mode)
    world.strategy.run_rotation(world.context)
    portfolio = world.context.portfolio
    if mode is None:
        assert held_codes(world) == {world.codes[0], world.codes[1], world.old}
        assert world.orders == {}
        assert portfolio.available_cash == 70000.0
    else:
        assert held_codes(world) == set(world.codes[:3])
        assert portfolio.positions[world.codes[2]].total_amount == 3300
        assert portfolio.available_cash == pytest.approx(46995.7)
        assert portfolio.total_value == pytest.approx(99995.7)


@pytest.mark.parametrize('fill', [0, 400])
def test_incomplete_sell_keeps_third_slot_occupied(monkeypatch, fill):
    world = account_world(monkeypatch, .40, sell_fill=fill)
    world.strategy.run_rotation(world.context)
    assert held_codes(world) == {world.codes[0], world.codes[1], world.old}
    assert world.context.portfolio.positions[world.old].total_amount == 1000 - fill
    assert not any(o.is_buy for o in world.orders.values())
    assert world.context.portfolio.available_cash == pytest.approx(70000 + fill * 10 * .9999)
    assert world.strategy.g.trades == []


def test_holding_at_rank_four_stays_inside_forty_percent_band(monkeypatch):
    world = account_world(monkeypatch, .40, held_rank=4)
    world.strategy.run_rotation(world.context)
    assert world.orders == {}


def test_unranked_holding_is_evaluated_by_its_own_trend(monkeypatch):
    world = account_world(monkeypatch, .40)
    world.strategy.g.pool.remove(world.old)
    world.strategy.run_rotation(world.context)
    assert world.orders == {}
    assert len(held_codes(world)) == 3


@pytest.mark.parametrize('mode', [None, .40])
def test_empty_pool_still_allows_cost_stop(monkeypatch, mode):
    world = account_world(monkeypatch, mode)
    world.strategy.g.pool = []
    world.prices[world.old] = 9.0
    world.mark()
    world.strategy.run_rotation(world.context)
    assert held_codes(world) == set(world.codes[:2])
    assert world.context.portfolio.total_value == pytest.approx(98999.1)


def test_exports_match_partial_fill_and_account_values(monkeypatch):
    world = account_world(monkeypatch, .40, sell_fill=400)
    world.strategy.run_rotation(world.context)
    world.strategy.after_trading_end(world.context)
    prefix = 'im_rank_exit_rank40_'
    nav = list(csv.DictReader(io.StringIO(world.files[prefix + 'nav.csv'])))[0]
    orders = list(csv.DictReader(io.StringIO(world.files[prefix + 'orders.csv'])))
    assert len(orders) == 1
    assert orders[0]['code'] == world.old and orders[0]['side'] == 'sell'
    assert int(orders[0]['requested']) == 1000 and int(orders[0]['filled']) == 400
    assert float(orders[0]['commission']) == pytest.approx(.4)
    assert float(nav['cash']) == pytest.approx(73999.6)
    assert float(nav['invested']) == pytest.approx(26000.0)
    assert float(nav['equity']) == pytest.approx(float(nav['cash']) + float(nav['invested']))
    assert world.old + ':600' in nav['holdings']
