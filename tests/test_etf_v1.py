"""全 ETF 独立脚本的合成交易世界；覆盖行为，不验证历史收益。"""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/etf/v1.py'


class World:
    def __init__(self):
        tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom)]
        self.ns = {}
        exec(compile(tree, str(SCRIPT), 'exec'), self.ns)
        self.codes = ['E%d' % i for i in range(1, 9)]
        dates = pd.bdate_range('2025-01-01', periods=250)
        self.closes = {c: pd.Series(10. + i, index=dates)
                       for i, c in enumerate(self.codes)}
        self.money = {c: np.full(60, 100_000_000.) for c in self.codes}
        self.scores = {c: 1. - i / 10. for i, c in enumerate(self.codes)}
        self.quotes = {c: NS(last_price=float(self.closes[c].iloc[-1]), paused=False)
                       for c in self.codes}
        self.context = NS(current_dt=datetime(2026, 11, 3, 14, 55),
                          portfolio=NS(positions={}, available_cash=300_000.,
                                       total_value=300_000., starting_cash=300_000.))
        self.logs, self.attempts, self.sells = [], [], []
        self.orders, self.open_orders, self.buy_specs = {}, {}, {}
        self.sell_limit = None
        self.ns.update(
            g=NS(), log=NS(info=self.logs.append, set_level=lambda *a: None),
            set_option=lambda *a: None, set_benchmark=lambda *a: None,
            set_slippage=lambda *a: None, FixedSlippage=lambda x: x,
            OrderCost=lambda **kw: NS(**kw), set_order_cost=lambda *a, **kw: None,
            run_daily=lambda *a, **kw: None,
            get_current_data=lambda: self.quotes,
            get_security_info=lambda c: NS(display_name=c),
            get_all_securities=lambda *a, **kw: pd.DataFrame(index=self.codes),
            attribute_history=self.history_close, history=self.history,
            get_orders=lambda: self.orders, get_open_orders=lambda: self.open_orders,
            order=self.buy, order_target=self.sell)
        self.ns['initialize'](self.context)
        self.g = self.ns['g']
        self.g.day = 1
        self.g.pool = self.codes.copy()
        self.g.pool_valid = True
        self.real_score = self.ns['momentum_score']
        self.ns['momentum_score'] = lambda tail, px: self.scores[
            next(c for c in self.codes if self.quotes[c].last_price == px)]

    def history_close(self, code, count, *args, **kwargs):
        return self.closes[code].tail(count).to_frame('close')

    def history(self, count, unit, field, security_list):
        if field == 'money':
            return pd.DataFrame({c: self.money[c][-count:] for c in security_list})
        return pd.DataFrame({c: self.closes[c].tail(count) for c in security_list})

    def hold(self, code, amount=1000, cost=None):
        self.context.portfolio.positions[code] = NS(
            total_amount=amount, avg_cost=cost or self.quotes[code].last_price,
            value=amount * self.quotes[code].last_price)

    def buy(self, code, amount):
        self.attempts.append((code, amount))
        spec = self.buy_specs.get(code, 'fill')
        if spec == 'reject':
            return None
        filled = amount if spec == 'fill' else (100 if spec == 'partial' else 0)
        active = spec in ('pending', 'partial')
        result = NS(order_id=str(len(self.orders) + 1), security=code, is_buy=True,
                    amount=amount, filled=filled, price=self.quotes[code].last_price,
                    commission=filled * self.quotes[code].last_price * .0001)
        self.orders[result.order_id] = result
        if filled:
            self.hold(code, filled)
        reserved = amount if active else filled
        self.context.portfolio.available_cash -= reserved * result.price * 1.0001
        if active:
            self.open_orders[result.order_id] = result
        return result

    def sell(self, code, target):
        assert target == 0
        self.sells.append(code)
        pos = self.context.portfolio.positions[code]
        filled = min(pos.total_amount, self.sell_limit or pos.total_amount)
        result = NS(order_id=str(len(self.orders) + 1), security=code, is_buy=False,
                    amount=pos.total_amount, filled=filled,
                    price=self.quotes[code].last_price,
                    commission=filled * self.quotes[code].last_price * .0001)
        self.orders[result.order_id] = result
        pos.total_amount -= filled
        if pos.total_amount == 0:
            pos.avg_cost = 0.  # 平台成交会原地改变持仓对象。
        self.context.portfolio.available_cash += filled * result.price - result.commission
        return result

    def run(self):
        self.ns['run_rotation'](self.context)
        self.context.current_dt += timedelta(days=1)


@pytest.fixture
def world():
    return World()


def test_entry_cannot_fall_through_to_rank_seven(world):
    for code in world.codes[:6]:
        world.money[code][-20:] *= .5
    world.run()
    assert world.attempts == []


def test_valid_empty_pool_still_stops_existing_position(world):
    world.g.pool = []
    world.hold('E1', cost=12.)
    world.run()
    assert world.sells == ['E1']
    assert len(world.g.trades) == 1


def test_liquidity_rejection_leaves_cash_instead_of_buying_rank_four(world):
    world.money['E1'][-20:] *= .5
    world.run()
    assert [c for c, _ in world.attempts] == ['E2', 'E3']


def test_failed_pool_rebuild_freezes_rank_exit_but_keeps_cost_stop(world):
    world.g.day = 0
    world.hold('E8')
    world.hold('E1', cost=12.)
    world.g.neg_run['E8'] = 2
    world.ns['rebuild_pool'] = lambda dt: (_ for _ in ()).throw(RuntimeError('offline'))
    world.run()
    assert world.sells == ['E1']
    assert world.g.neg_run['E8'] == 2
    assert world.attempts == []
    assert not world.g.pool_valid


def test_pool_failure_retries_on_next_evaluation(world):
    world.g.pool_valid = False
    world.ns['rebuild_pool'] = lambda dt: []
    world.run()
    assert world.g.pool_valid and world.g.pool == []


def test_bad_daily_history_does_not_manufacture_rank_exit(world):
    world.hold('E8')
    world.g.neg_run['E8'] = 2
    world.closes['E1'].iloc[-1] = np.nan
    world.run()
    assert world.sells == [] and world.attempts == []
    assert world.g.neg_run['E8'] == 2


def test_confirmed_empty_pool_exits_after_three_evaluations(world):
    world.g.pool = []
    world.hold('E1')
    world.run()
    world.run()
    assert not world.sells
    world.run()
    assert world.sells == ['E1']


def test_rank_buffer_retains_six_and_exits_seven(world):
    world.hold('E6')
    world.hold('E7')
    for _ in range(3):
        world.run()
    assert world.sells == ['E7']
    assert world.context.portfolio.positions['E6'].total_amount > 0


def test_partial_sell_occupies_slot_and_ledger_reconciles(world):
    for code in ('E6', 'E7', 'E8'):
        world.hold(code)
    world.g.neg_run.update(E7=2, E8=2)
    world.sell_limit = 400
    world.run()
    assert world.attempts == [] and world.g.trades == []
    world.ns['after_trading_end'](world.context)
    assert len(world.g.sell_fills) == 2  # 已记录的部分成交不重复入账。


@pytest.mark.parametrize('spec', ['pending', 'partial'])
def test_accepted_buy_reserves_slot_even_before_full_fill(world, spec):
    world.hold('E6')
    world.hold('E7')
    world.buy_specs['E1'] = spec
    world.run()
    assert [c for c, _ in world.attempts] == ['E1']
    world.run()
    assert [c for c, _ in world.attempts] == ['E1']


@pytest.mark.parametrize('spec', ['reject', 'cancel'])
def test_unsuccessful_buy_does_not_consume_budget_or_slot(world, spec):
    world.hold('E6')
    world.hold('E7')
    world.buy_specs['E1'] = spec
    world.run()
    assert [c for c, _ in world.attempts] == ['E1', 'E2']
    assert not any('买入成交 E1' in line for line in world.logs)


def test_existing_open_buy_counts_toward_maximum_positions(world):
    world.hold('E6')
    world.hold('E7')
    world.open_orders['old'] = NS(security='E8', is_buy=True)
    world.run()
    assert world.attempts == []


def test_pending_sell_is_not_submitted_twice(world):
    world.hold('E1', cost=12.)
    world.open_orders['old'] = NS(security='E1', is_buy=False)
    world.run()
    assert world.sells == []


def test_cost_stop_cannot_buy_back_on_same_evaluation(world):
    world.hold('E1', cost=12.)
    world.run()
    assert world.sells == ['E1']
    assert [c for c, _ in world.attempts] == ['E2', 'E3']


def test_valid_negative_score_can_exit_when_pool_is_unknown(world):
    world.g.pool_valid = False
    world.ns['rebuild_pool'] = lambda dt: (_ for _ in ()).throw(RuntimeError('offline'))
    world.hold('E1')
    world.scores['E1'] = -.1
    for _ in range(3):
        world.run()
    assert world.sells == ['E1']
    assert world.attempts == []


def test_recovery_resets_confirmation_counter(world):
    world.hold('E7')
    world.run()
    world.run()
    assert world.g.neg_run['E7'] == 2
    world.scores['E7'] = 2.
    world.run()
    assert world.g.neg_run['E7'] == 0
    world.scores['E7'] = .4
    world.run()
    assert world.sells == []


def test_initial_rebuild_and_buy_work_together(world):
    world.g.day, world.g.pool, world.g.pool_valid = 0, [], False
    world.run()
    assert world.g.pool_valid
    assert [c for c, _ in world.attempts] == ['E1', 'E2', 'E3']
    for code, amount in world.attempts:
        assert amount * world.quotes[code].last_price <= 100_000.


def test_all_missing_pool_data_is_failure_not_confirmed_empty_pool(world):
    world.g.day = 0
    for code in world.codes:
        world.money[code][:] = np.nan
    world.hold('E8')
    world.g.neg_run['E8'] = 2
    world.run()
    assert not world.g.pool_valid
    assert world.g.neg_run['E8'] == 2
    assert world.attempts == [] and world.sells == []


def test_actual_liquidity_filter_can_produce_valid_empty_pool(world):
    world.g.day = 0
    for code in world.codes:
        world.money[code][:] = 1_000_000.
    world.hold('E8')
    world.g.neg_run['E8'] = 2
    world.run()
    assert world.g.pool_valid and world.g.pool == []
    assert world.sells == ['E8'] and world.attempts == []


@pytest.mark.parametrize('values', [np.zeros(60), np.full(60, np.inf),
                                   np.r_[np.ones(59), np.nan], np.ones(59)])
def test_invalid_volume_is_ineligible(world, values):
    world.money['E1'] = values
    world.ns['history'] = lambda *a, **kw: pd.DataFrame({'E1': values})
    assert world.ns['volume_ratio']('E1') is None


def test_volume_uses_disjoint_twenty_and_forty_day_windows(world):
    world.money['E1'] = np.r_[np.full(40, 100.), np.full(20, 75.)]
    assert world.ns['volume_ratio']('E1') == pytest.approx(.75)


def test_short_high_score_candidate_cannot_remove_mature_representative(world):
    world.g.pool = []
    world.ns['momentum_score'] = world.real_score
    x = np.arange(250)
    world.closes['E1'][:] = np.exp(.001*x + .01*np.sin(x))
    world.closes['E2'] = pd.Series(np.exp(.004*x[50:] + .01*np.sin(x[50:])),
                                     index=world.closes['E1'].index[50:])
    for i, code in enumerate(world.codes[2:]):
        world.closes[code][:] = np.exp(-.001*x + .02*np.sin((.3 + i*.1)*x))
    pool = world.ns['rebuild_pool'](world.context.current_dt)
    assert 'E1' in pool and 'E2' not in pool


def test_small_pool_also_checks_maturity(world):
    world.g.pool = []
    world.codes = world.codes[:2]
    world.closes['E2'] = world.closes['E2'].tail(200)
    pool = world.ns['rebuild_pool'](world.context.current_dt)
    assert pool == ['E1']


def test_late_buy_fill_is_logged_once(world):
    world.buy_specs['E1'] = 'pending'
    world.run()
    pending = world.orders['1']
    pending.filled = pending.amount
    world.hold('E1', pending.amount)
    world.open_orders.pop('1')
    world.ns['after_trading_end'](world.context)
    world.ns['after_trading_end'](world.context)
    assert len([line for line in world.logs if '买入成交 E1' in line]) == 1


@pytest.mark.parametrize('bad', [np.nan, np.inf, 0., -1.])
def test_invalid_price_never_becomes_a_valid_score(world, bad):
    prices = np.linspace(10., 11., 25)
    assert world.real_score(prices, bad) == 0.


@pytest.mark.parametrize('slope', [.001, -.001])
def test_geometric_price_path_has_full_trend_fit(world, slope):
    prices = np.exp(slope * np.arange(26))
    expected = round(float(np.exp(slope * 250) - 1), 4)
    assert world.real_score(prices[:-1], prices[-1]) == expected


def test_latest_sharp_drop_vetoes_an_otherwise_positive_trend(world):
    prices = np.exp(.01 * np.arange(26))
    prices[-1] = prices[-2] * .94
    assert world.real_score(prices[:-1], prices[-1]) == 0.
