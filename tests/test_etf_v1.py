"""全 ETF 独立脚本的合成交易世界；覆盖行为，不验证历史收益。"""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/etf/v1.py'

pytestmark = pytest.mark.skip(
    reason='本文件测的是已删除的 etf/v1.py 的 run_rotation / momentum_details 接口；'
           '当前 etf 基线的实现是 etf/momentum_v1.py，'
           'etf/momentum_v2.py 的分支与基线等价用例见 tests/test_etf_momentum_v2.py')


class World:
    def __init__(self, script=SCRIPT):
        tree = ast.parse(script.read_text(encoding='utf-8'))
        tree.body = [node for node in tree.body if not isinstance(node, ast.ImportFrom)]
        self.ns = {}
        exec(compile(tree, str(script), 'exec'), self.ns)
        self.codes = ['E%d' % i for i in range(1, 9)]
        dates = pd.bdate_range('2025-01-01', periods=250)
        self.closes = {c: pd.Series(10. + i, index=dates)
                       for i, c in enumerate(self.codes)}
        self.money = {c: np.full(60, 100_000_000.) for c in self.codes}
        self.scores = {c: 1. - i / 10. for i, c in enumerate(self.codes)}
        self.sharp_drops = set()
        self.quotes = {c: NS(last_price=float(self.closes[c].iloc[-1]), paused=False)
                       for c in self.codes}
        self.context = NS(current_dt=datetime(2026, 11, 3, 14, 55),
                          portfolio=NS(positions={}, available_cash=300_000.,
                                       total_value=300_000., starting_cash=300_000.))
        self.logs, self.attempts, self.sells = [], [], []
        self.files = {}
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
            write_file=lambda name, content: self.files.__setitem__(name, content),
            get_orders=lambda: self.orders, get_open_orders=lambda: self.open_orders,
            order=self.buy, order_target=self.sell)
        self.ns['initialize'](self.context)
        self.g = self.ns['g']
        self.g.day = 1
        self.g.pool = self.codes.copy()
        self.g.pool_candidates = self.codes.copy()
        self.g.pool_conflicts = {code: set() for code in self.codes}
        self.g.pool_valid = True
        if script == SCRIPT:
            self.real_details = self.ns['momentum_details']
            self.ns['momentum_details'] = self.score_details

    def score_details(self, tail, px):
        code = next(c for c in self.codes if self.quotes[c].last_price == px)
        return self.scores[code], code in self.sharp_drops

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
    world.logs.clear()
    world.run()
    assert world.attempts == []
    assert world.logs == []
    assert len(world.g.detail_log) == 1 and '周度目标 []' in world.g.detail_log[0]


def test_positive_holding_can_stay_below_the_old_cost_line(world):
    world.g.pool_candidates = []
    world.hold('E1', cost=12.)
    world.run()
    assert world.sells == [] and set(world.g.week_targets) == {'E1'}


def test_liquidity_rejection_leaves_cash_instead_of_buying_rank_four(world):
    world.money['E1'][-20:] *= .5
    world.run()
    assert [c for c, _ in world.attempts] == ['E2', 'E3']


def test_sharp_drop_blocks_new_buy_without_reordering_the_top_three(world):
    world.sharp_drops.add('E1')
    world.run()
    assert [c for c, _ in world.attempts] == ['E2', 'E3']
    assert any('跳水暂缓新买 E1 score=+1 rank=1' in line for line in world.g.detail_log)


def test_failed_pool_rebuild_defers_all_trades(world):
    world.g.day = 0
    world.hold('E8')
    world.hold('E1', cost=12.)
    world.ns['rebuild_pool'] = lambda dt, held: (_ for _ in ()).throw(RuntimeError('offline'))
    world.run()
    assert world.sells == [] and world.attempts == []
    assert not world.g.pool_valid


def test_pool_failure_retries_on_next_evaluation(world):
    world.g.pool_valid = False
    world.ns['rebuild_pool'] = lambda dt, held: ([], {})
    world.run()
    assert world.g.pool_valid and world.g.pool == []


@pytest.mark.parametrize('missing_code', ['E1', 'E8'])
def test_bad_daily_history_does_not_manufacture_a_sell(world, missing_code):
    world.hold('E8')
    world.closes[missing_code].iloc[-1] = np.nan
    world.run()
    assert world.sells == [] and world.attempts == []


@pytest.mark.parametrize('score', [0., -.1])
def test_nonpositive_holding_leaves_empty_weekly_target(world, score):
    world.g.pool_candidates = []
    world.hold('E1')
    world.scores['E1'] = score
    world.run()
    assert world.sells == ['E1'] and world.attempts == []
    assert not any('持仓评估' in line or '卖出成交' in line for line in world.logs)
    world.ns['on_strategy_end'](world.context)
    assert any('【台账核对】' in line for line in world.logs)
    [(name, content)] = world.files.items()
    assert name.startswith('etf_v1_details_') and name.endswith('.txt')
    assert any(name in line for line in world.logs)
    assert '2026-11-03 14:55:00 - 周度目标 []' in content
    assert '卖出成交 E1 E1 触发[未入选周度目标]' in content


def test_rank_changes_only_affect_the_next_weekly_selection(world):
    world.hold('E7')
    world.scores['E7'] = 2.
    world.run()
    world.scores['E7'] = .1
    world.run()
    assert world.sells == []
    assert set(world.g.week_targets) == {'E7', 'E1', 'E2'}
    world.context.current_dt = datetime(2026, 11, 9, 14, 55)
    world.run()
    assert world.sells == ['E7']
    assert set(world.g.week_targets) == {'E1', 'E2', 'E3'}


def test_partial_sell_occupies_slot_and_ledger_reconciles(world):
    for code in ('E6', 'E7', 'E8'):
        world.hold(code)
    world.scores['E6'] = 2.
    world.sell_limit = 400
    world.run()
    assert world.attempts == [] and world.g.trades == []
    world.ns['after_trading_end'](world.context)
    assert len(world.g.sell_fills) == 2  # 已记录的部分成交不重复入账。
    world.sell_limit = None
    world.scores['E1'] = -.1
    world.run()
    assert [c for c, _ in world.attempts] == ['E1', 'E2']


@pytest.mark.parametrize('spec', ['pending', 'partial'])
def test_accepted_buy_reserves_slot_even_before_full_fill(world, spec):
    world.hold('E6')
    world.hold('E7')
    world.scores.update(E6=2., E7=1.5)
    world.buy_specs['E1'] = spec
    world.run()
    assert [c for c, _ in world.attempts] == ['E1']
    world.run()
    assert [c for c, _ in world.attempts] == ['E1']


@pytest.mark.parametrize('spec', ['reject', 'cancel'])
def test_unsuccessful_buy_does_not_consume_budget_or_slot(world, spec):
    world.hold('E6')
    world.scores['E6'] = 2.
    world.buy_specs['E1'] = spec
    world.run()
    assert [c for c, _ in world.attempts] == ['E1', 'E2']
    assert not any('买入成交 E1' in line for line in world.g.detail_log)
    world.buy_specs['E1'] = 'fill'
    world.run()
    assert [c for c, _ in world.attempts] == ['E1', 'E2', 'E1']


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


def test_paused_holding_keeps_its_weekly_slot(world):
    world.hold('E8')
    world.quotes['E8'].paused = True
    world.run()
    assert [c for c, _ in world.attempts] == ['E1', 'E2']
    world.quotes['E8'].paused = False
    world.run()
    assert world.sells == []


def test_negative_score_does_not_trade_before_complete_weekly_ranking(world):
    world.g.pool_valid = False
    world.ns['rebuild_pool'] = lambda dt, held: (_ for _ in ()).throw(RuntimeError('offline'))
    world.hold('E1')
    world.scores['E1'] = -.1
    world.run()
    assert world.sells == []
    assert world.attempts == []


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
    world.run()
    assert not world.g.pool_valid
    assert world.attempts == [] and world.sells == []


def test_liquidity_filter_can_leave_only_the_current_holding(world):
    world.g.day = 0
    for code in world.codes:
        world.money[code][:] = 1_000_000.
    world.hold('E8')
    world.run()
    assert world.g.pool_valid and world.g.pool_candidates == []
    assert world.g.pool == ['E8']
    assert world.sells == [] and world.attempts == []


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
    world.ns['momentum_details'] = world.real_details
    x = np.arange(250)
    world.closes['E1'][:] = np.exp(.001*x + .01*np.sin(x))
    world.closes['E2'] = pd.Series(np.exp(.004*x[50:] + .01*np.sin(x[50:])),
                                     index=world.closes['E1'].index[50:])
    for i, code in enumerate(world.codes[2:]):
        world.closes[code][:] = np.exp(-.001*x + .02*np.sin((.3 + i*.1)*x))
    candidates, conflicts = world.ns['rebuild_pool'](world.context.current_dt, set())
    pool = world.ns['select_pool'](candidates, conflicts, set())
    assert 'E1' in pool and 'E2' not in pool


def test_small_pool_also_checks_maturity(world):
    world.g.pool = []
    world.codes = world.codes[:2]
    world.closes['E2'] = world.closes['E2'].tail(200)
    world.closes['E1'].iloc[0] = np.nan
    candidates, _ = world.ns['rebuild_pool'](world.context.current_dt, {'E1', 'E2'})
    assert candidates == ['E1']


def test_holding_seeds_pool_until_full_exit_and_replacement_can_enter(world):
    world.g.day = 0
    world.codes = ['E1', 'E2']
    world.ns['momentum_details'] = world.real_details
    x = np.arange(250)
    world.closes['E1'][:] = np.exp(.003 * x + .01 * np.sin(x))
    world.closes['E2'] = (world.closes['E1'] * 2).tail(160)
    for code in world.codes:
        world.quotes[code].last_price = float(world.closes[code].iloc[-1])
    world.money['E2'][:] = 1_000_000.
    world.hold('E2')

    world.run()
    assert world.g.pool_candidates == ['E1']
    assert world.g.pool == ['E2'] and world.attempts == []

    world.ns['momentum_details'] = world.score_details
    world.scores['E2'] = -.1
    world.context.current_dt = datetime(2026, 11, 9, 14, 55)
    world.sell_limit = 400
    world.run()
    assert world.context.portfolio.positions['E2'].total_amount == 600
    assert world.g.pool == ['E2'] and world.attempts == []

    world.sell_limit = None
    world.run()
    assert world.context.portfolio.positions['E2'].total_amount == 0
    world.context.current_dt = datetime(2026, 11, 16, 14, 55)
    world.run()
    assert world.g.pool == ['E1']
    assert [code for code, _ in world.attempts] == ['E1']


def test_late_buy_fill_is_logged_once(world):
    world.buy_specs['E1'] = 'pending'
    world.run()
    pending = world.orders['1']
    pending.filled = pending.amount
    world.hold('E1', pending.amount)
    world.open_orders.pop('1')
    world.ns['after_trading_end'](world.context)
    world.ns['after_trading_end'](world.context)
    assert len([line for line in world.g.detail_log if '买入成交 E1' in line]) == 1


@pytest.mark.parametrize('bad', [np.nan, np.inf, 0., -1.])
def test_invalid_price_never_becomes_a_valid_score(world, bad):
    prices = np.linspace(10., 11., 25)
    assert world.real_details(prices, bad) == (None, None)


@pytest.mark.parametrize('slope', [.001, -.001, .0000001])
def test_geometric_price_path_has_full_trend_fit(world, slope):
    prices = np.exp(slope * np.arange(26))
    score, sharp_drop = world.real_details(prices[:-1], prices[-1])
    assert score == pytest.approx(np.exp(slope * 250) - 1)
    assert not sharp_drop


def test_sharp_drop_does_not_sell_a_positive_trend_holding(world):
    world.ns['momentum_details'] = world.real_details
    closes = world.closes['E1']
    closes[:] = np.exp(.01 * np.arange(len(closes)))
    shocked = closes.iloc[-1] * .94
    world.hold('E1', cost=shocked)
    for price in (shocked, shocked * 1.02, shocked * 1.03):
        score, sharp_drop = world.real_details(closes.to_numpy(), price)
        assert score > 0 and sharp_drop
        world.quotes['E1'].last_price = price
        world.run()
        closes.loc[closes.index[-1] + pd.offsets.BDay(1)] = price

    assert world.sells == []
