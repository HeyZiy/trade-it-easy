"""六组聚宽账户的资金隔离、原规则一致性与真实账户统计。"""
import ast
import csv
import importlib.util
import io
import math
import sys
import types
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


FOLDER = Path(__file__).resolve().parents[1] / 'research/studies/etf_rotation'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, FOLDER / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def world(monkeypatch):
    api = types.ModuleType('jqdata')
    api.g = types.SimpleNamespace()
    api.sum, api.min, api.max, api.all = np.sum, np.min, np.max, np.all
    monkeypatch.setitem(sys.modules, 'jqdata', api)
    module = load('pool_controls_test', 'study_pool_controls_jq.py')
    logs, records, orders, files, history_calls, metadata_dates = [], [], [], {}, [], []
    context = types.SimpleNamespace(current_dt=datetime(2016, 1, 4, 10, 40),
                                    previous_date=date(2015, 12, 31),
                                    portfolio=types.SimpleNamespace(starting_cash=600000.))
    for name in ('set_benchmark', 'set_option', 'set_order_cost', 'set_slippage', 'run_daily'):
        setattr(module, name, lambda *args, **kwargs: None)
    module.OrderCost = lambda **kwargs: kwargs
    module.FixedSlippage = lambda value: value
    module.SubPortfolioConfig = lambda **kwargs: kwargs

    def portfolios(configs):
        assert sum(row['cash'] for row in configs) == context.portfolio.starting_cash
        context.subportfolios = [types.SimpleNamespace(total_value=row['cash'],
                                                       available_cash=row['cash'],
                                                       long_positions={}) for row in configs]

    module.set_subportfolios = portfolios
    module.log = types.SimpleNamespace(info=logs.append, error=logs.append)
    module.record = lambda **kwargs: records.append(kwargs)

    def write(filename, text, append=False):
        files[filename] = (files.get(filename, '') if append else '') + text

    module.write_file = write
    prices = {code: 1. for code in module.MIXED37}
    histories = {code: np.ones(25) for code in module.MIXED37}
    paused, failed_sells = set(), set()
    starts = {code: date(2010, 1, 1) for code in module.MIXED37}

    def history(code, days, frequency, fields, **kwargs):
        history_calls.append((context.current_dt, code, days, kwargs))
        return pd.DataFrame({'close': histories[code]})

    module.attribute_history = history
    module.get_current_data = lambda: {
        code: types.SimpleNamespace(last_price=prices[code], paused=code in paused)
        for code in module.MIXED37}

    def security(code, date=None):
        metadata_dates.append(date)
        return types.SimpleNamespace(start_date=starts[code], end_date=date_max,
                                     display_name=code)

    date_max = date(2200, 1, 1)
    module.get_security_info = security

    def order_target(code, target, pindex):
        portfolio = context.subportfolios[pindex]
        position = portfolio.long_positions.get(code)
        held = position.total_amount if position else 0
        delta = target - held
        orders.append((pindex, code, target))
        if delta == 0 or (delta < 0 and (pindex, code) in failed_sells):
            return types.SimpleNamespace(filled=0)
        if delta < 0:
            delta = -min(-delta, position.closeable_amount)
        notional = abs(delta) * prices[code]
        fee = max(5., notional * module.COMMISSION)
        if delta > 0:
            assert delta % 100 == 0
            assert notional + fee <= portfolio.available_cash + 1e-8
        portfolio.available_cash -= delta * prices[code] + fee
        portfolio.long_positions[code] = types.SimpleNamespace(
            total_amount=held + delta, closeable_amount=held + delta,
            value=(held + delta) * prices[code])
        portfolio.total_value -= fee
        return types.SimpleNamespace(filled=abs(delta))

    def order_target_value(code, value, pindex):
        if value <= 0:
            return order_target(code, 0, pindex)
        portfolio = context.subportfolios[pindex]
        affordable = max(0., portfolio.available_cash - 5.) / (1.0002 * prices[code])
        target = int(min(value / prices[code], affordable) / 100) * 100
        return order_target(code, target, pindex)

    module.order_target = order_target
    module.order_target_value = order_target_value
    module.initialize(context)
    return types.SimpleNamespace(module=module, ctx=context, prices=prices, histories=histories,
                                 paused=paused, starts=starts, failed_sells=failed_sells,
                                 logs=logs, records=records, orders=orders, files=files,
                                 history_calls=history_calls, metadata_dates=metadata_dates)


def seed(world, index, code, amount=100000):
    portfolio = world.ctx.subportfolios[index]
    portfolio.available_cash = 0.
    portfolio.long_positions[code] = types.SimpleNamespace(
        total_amount=amount, closeable_amount=amount, value=amount * world.prices[code])


def observation(score, status='eligible'):
    return dict(score=score, status=status)


def test_six_isolated_accounts_and_pools_match_existing_versions(world):
    w = world
    assert [a['label'] for a in w.module.g.accounts] == [
        '15等权', '15合格持有', '15排名', '37等权', '37合格持有', '37排名']
    assert all(p.available_cash == 100000 for p in w.ctx.subportfolios)
    for filename, expected in (('unified_coverage.py', w.module.MIXED37),
                               ('study_etf_fit_jq.py', w.module.ORIGINAL15)):
        tree = ast.parse((FOLDER / filename).read_text(encoding='utf-8'))
        pool = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == 'etf_pool'
                            for t in node.targets))
        assert tuple(ast.literal_eval(pool)) == expected
    assert len(set(w.module.MIXED37)) == 37
    w.ctx.portfolio.starting_cash = 100000
    with pytest.raises(ValueError, match='600000'):
        w.module.initialize(w.ctx)


@pytest.mark.parametrize('daily_slope,last_ratio', [(.001, 1), (.002, 1), (.01, 1),
                                                   (-.002, 1), (.001, .94)])
def test_signal_numerically_matches_original_and_uses_only_past_history(world, daily_slope, last_ratio):
    w = world
    code = w.module.ORIGINAL15[0]
    w.histories[code] = np.exp(np.arange(25) * daily_slope)
    w.prices[code] = math.exp(25 * daily_slope) * last_ratio
    original = load('original_rotation_test', 'unified_coverage.py')
    original.attribute_history = w.module.attribute_history
    original.get_current_data = w.module.get_current_data
    strategy = original.EtfRotationStrategy(w.ctx)
    expected = strategy.calculate_momentum_score(code)
    _, signals = w.module.signal_snapshot(w.ctx)
    assert signals[code]['score'] == pytest.approx(expected, abs=1e-12)
    assert all(days == 25 and kwargs == {'skip_paused': False, 'fq': 'pre'}
               for _, _, days, kwargs in w.history_calls)


def test_rank_replaces_eligible_holding_but_hold_control_keeps_it(world):
    w = world
    old, new = w.module.ORIGINAL15[:2]
    seed(w, 1, old)
    seed(w, 2, old)
    signals = {old: observation(1.), new: observation(2.)}
    current = w.module.get_current_data()
    hold = w.module.rotation_trade(w.ctx, w.module.g.accounts[1], [new, old], signals, current)
    rank = w.module.rotation_trade(w.ctx, w.module.g.accounts[2], [new, old], signals, current)
    assert hold[3:5] == (old, 'keep')
    assert rank[3:5] == (new, 'rank_replaced')
    assert all(index == 2 for index, _, _ in w.orders)
    assert w.ctx.subportfolios[1].long_positions[old].total_amount == 100000
    assert w.ctx.subportfolios[2].long_positions[new].total_amount > 0
    assert w.ctx.subportfolios[0].available_cash == 100000


@pytest.mark.parametrize('status,score', [('score_upper_limit', 6.), ('drop_filter', 0.),
                                        ('nonpositive', -.1), ('missing_history', None)])
def test_hold_control_still_exits_when_ineligible_and_records_actual_reason(world, status, score):
    w = world
    old, new = w.module.ORIGINAL15[:2]
    seed(w, 4, old)
    signals = {old: observation(score, status), new: observation(1.)}
    decision = w.module.rotation_trade(w.ctx, w.module.g.accounts[4], [new], signals,
                                       w.module.get_current_data())
    assert decision[3:5] == (new, status)
    assert all(index == 4 for index, _, _ in w.orders)


def test_target_is_not_reported_as_actual_position_after_failed_sale(world):
    w = world
    old, new = w.module.ORIGINAL15[:2]
    seed(w, 2, old)
    w.failed_sells.add((2, old))
    decision = w.module.rotation_trade(w.ctx, w.module.g.accounts[2], [new],
                                       {old: observation(1), new: observation(2)},
                                       w.module.get_current_data())
    w.module.after_market_close(w.ctx)
    rows = list(csv.DictReader(io.StringIO(w.files['pool_control_nav.csv'])))
    assert decision[3] == new
    assert rows[2]['holdings'] == old
    assert rows[2]['cash'] == '0.0'


def test_equal_weight_paused_budget_historical_gate_and_monthly_entry(world):
    w = world
    account = w.module.g.accounts[0]
    account['pool'] = w.module.ORIGINAL15[:3]
    first, paused, late = account['pool']
    w.paused.add(paused)
    w.histories[late] = np.ones(24)
    w.module.equal_trade(w.ctx, account, w.module.get_current_data())
    portfolio = w.ctx.subportfolios[0]
    assert portfolio.long_positions[first].value <= 50000
    assert paused not in portfolio.long_positions and late not in portfolio.long_positions
    assert portfolio.available_cash == pytest.approx(50000 - 10.)
    assert all(day == w.ctx.previous_date for day in w.metadata_dates)
    before = list(w.orders)
    w.histories[late] = np.ones(25)
    w.ctx.current_dt = datetime(2016, 1, 5, 10, 40)
    assert w.module.equal_trade(w.ctx, account, w.module.get_current_data()) is None
    assert w.orders == before
    w.ctx.current_dt = datetime(2016, 2, 1, 10, 40)
    w.module.equal_trade(w.ctx, account, w.module.get_current_data())
    assert portfolio.long_positions[late].total_amount > 0
    assert portfolio.available_cash >= 0


def test_equal_weight_failed_sell_cannot_spend_other_accounts_cash(world):
    w = world
    code = w.module.ORIGINAL15[0]
    seed(w, 0, code)
    w.failed_sells.add((0, code))
    w.module.equal_trade(w.ctx, w.module.g.accounts[0], w.module.get_current_data())
    assert len(w.orders) == 1
    assert w.ctx.subportfolios[0].available_cash == 0
    assert w.ctx.subportfolios[1].available_cash == 100000


def test_two_pool_rankings_stable_ties_and_one_daily_export(world):
    w = world
    # 所有标的相同且有效趋势，同分沿用各池成员顺序。
    for code in w.module.MIXED37:
        w.histories[code] = np.exp(np.arange(25) * .001)
        w.prices[code] = math.exp(.025)
    w.module.trade_handler(w.ctx)
    before = list(w.orders)
    w.module.trade_handler(w.ctx)
    assert w.orders == before
    rows = list(csv.DictReader(io.StringIO(w.files['pool_control_signals.csv'])))
    assert len(rows) == 37
    assert rows[0]['rank15'] == rows[0]['rank37'] == '1'
    assert rows[15]['rank15'] == '' and rows[15]['rank37'] == '16'
    assert rows[0]['status'] == 'eligible'
    w.module.after_market_close(w.ctx)
    assert len(w.records[-1]) == 6
    assert all(value < 0 for value in w.records[-1].values())  # 成交佣金进入实际账户收益。


def test_year_boundary_drawdowns_cash_and_repeated_close(world):
    w = world
    for day, total in [('2016-12-29', 90000.), ('2016-12-30', 110000.),
                       ('2017-01-03', 99000.)]:
        w.ctx.current_dt = datetime.fromisoformat(day + 'T15:01:00')
        for portfolio in w.ctx.subportfolios:
            portfolio.total_value = portfolio.available_cash = total
        w.module.after_market_close(w.ctx)
    w.module.after_market_close(w.ctx)
    assert len(w.records) == 3
    assert len(list(csv.DictReader(io.StringIO(w.files['pool_control_nav.csv'])))) == 18
    for account in w.module.g.accounts:
        assert account['years'][2017]['start'] == 110000.
        assert account['years'][2017]['drawdown'] == pytest.approx(-.1)
        assert account['max_drawdown'] == pytest.approx(-.1)
    w.module.on_strategy_end(w.ctx)
    assert any('15等权 2017年' in row and '收益-10.00%' in row for row in w.logs)
    assert any('37排名全程' in row and '最大回撤10.00%' in row for row in w.logs)


@pytest.mark.parametrize('total', [0., float('nan'), float('inf')])
def test_invalid_nav_stops_statistics_instead_of_fabricating_returns(world, total):
    world.ctx.subportfolios[0].total_value = total
    with pytest.raises(ValueError, match='净值无效'):
        world.module.after_market_close(world.ctx)
    assert not world.records
