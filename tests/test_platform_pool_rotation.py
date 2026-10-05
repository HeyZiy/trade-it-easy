# -*- coding: utf-8 -*-
"""质量池轮动的本地交易模拟；验证行为，不验证收益或供应商历史数据。

专用桩补齐差额调仓、T+1、最低佣金及部分成交，避免共享桩仅支持空仓买入
的近似掩盖再平衡错误。成交使用合成当日价格，不替代聚宽09:31撮合核验。
"""
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jq_fake import JQStub, _Portfolio  # noqa: E402

SCRIPT = (Path(__file__).resolve().parents[1] / 'research' / 'studies'
          / 'roe_quality' / 'pool_rotation_v1.py')


class PoolPortfolio(_Portfolio):
    @property
    def total_value(self):
        return self.cash + sum((p.total_amount + p.locked_amount) * self._stub.price(c)
                               for c, p in self.positions.items())

    @property
    def available_cash(self):
        return self.cash


class RotationStub(JQStub):
    def __init__(self, calendar, closes, cash=1_000_000):
        super().__init__(calendar, closes, starting_cash=cash)
        self.portfolio = PoolPortfolio(self, cash)
        self.ctx.portfolio = self.portfolio
        self.daily_orders = {}
        self.live_orders = {}
        self.exports = {}
        self.records = []
        self.costs = []
        self.log_levels = {}
        self.sell_caps = {}
        self.buy_caps = {}
        self.reject_buys = set()
        self.oid = 0

    def _history(self, count, unit='1d', field='close', security_list=None, **kw):
        return pd.DataFrame({c: self.closes[c][self.closes[c].index < self.today].tail(count)
                             for c in security_list})

    def _get_trade_days(self, start_date=None, end_date=None, count=None, **kw):
        days = self.calendar
        if start_date is not None:
            days = days[days >= pd.Timestamp(start_date).normalize()]
        if end_date is not None:
            days = days[days <= pd.Timestamp(end_date).normalize()]
        return list(days[-count:] if count is not None else days)

    def _order_target(self, code, amount):
        pos = self.portfolio.positions.setdefault(code, SimpleNamespace(
            total_amount=0, closeable_amount=0, locked_amount=0, avg_cost=0))
        delta = amount - pos.total_amount
        if delta == 0:
            return None
        px, is_buy = self.price(code), delta > 0
        if is_buy and code in self.reject_buys:
            return None
        quantity = (min(delta, self.buy_caps.get(code, delta)) if is_buy else
                    min(-delta, pos.closeable_amount, self.sell_caps.get(code, -delta)))
        if quantity <= 0:
            return None
        cost = self.costs[-1]
        commission = max(cost.min_commission, quantity * px * cost.open_commission)
        if is_buy:
            assert quantity % 100 == 0
            assert self.portfolio.cash >= quantity * px + commission
            self.portfolio.cash -= quantity * px + commission
            pos.total_amount += quantity
        else:
            self.portfolio.cash += quantity * px * (1 - cost.close_tax) - commission
            pos.total_amount -= quantity
            pos.closeable_amount -= quantity
        self.orders.append(('B' if is_buy else 'S', self.today.strftime('%F'),
                            code, quantity, px))
        self.oid += 1
        order = SimpleNamespace(security=code, is_buy=is_buy, filled=quantity,
                                price=px, status='held' if quantity == abs(delta) else 'canceled')
        self.daily_orders[self.oid] = order
        return order

    def _get_price(self, code, end_date=None, count=None, **kwargs):
        assert code == '000906.XSHG'
        assert pd.Timestamp(end_date).normalize() <= self.today
        days = self.calendar[self.calendar <= pd.Timestamp(end_date).normalize()][-count:]
        return pd.DataFrame({'close': [1000.0] * len(days)}, index=days)

    def install(self):
        super().install()
        mod = self._module
        mod.get_open_orders = lambda: self.live_orders
        mod.get_orders = lambda: self.daily_orders
        mod.get_price = self._get_price
        mod.OrderCost = lambda **kw: SimpleNamespace(**kw)
        mod.set_order_cost = lambda cost, **kw: self.costs.append(cost)
        mod.set_slippage = lambda *args: None
        mod.PriceRelatedSlippage = lambda value: value
        mod.record = lambda **values: self.records.append(values)
        mod.write_file = lambda name, content: self.exports.__setitem__(name, content)
        mod.log.set_level = lambda name, level: self.log_levels.__setitem__(name, level)
        return self

    def run_days(self, dates):
        for day in dates:
            self.today = pd.Timestamp(day).normalize()
            self.daily_orders = {}
            for pos in self.portfolio.positions.values():
                pos.closeable_amount = pos.total_amount
            for time in sorted(self.scheduled):
                hour, minute = map(int, time.split(':'))
                self.ctx.current_dt = self.today + pd.Timedelta(hours=hour, minutes=minute)
                self.scheduled[time](self.ctx)
            self.ctx.current_dt = self.today + pd.Timedelta(hours=15, minutes=30)
            self._script.after_trading_end(self.ctx)


@pytest.fixture
def world():
    previous = sys.modules.get('jqdata')
    cal = pd.bdate_range('2025-01-02', periods=230)

    def create(n=40, cash=1_000_000):
        codes = ['600%03d.XSHG' % i for i in range(n)]
        closes = {c: pd.Series(10.0, index=cal) for c in codes}
        stub = RotationStub(cal, closes, cash).install()
        stub.set_date_table(pd.DataFrame({
            'code': codes, 'roe': [4.0] * n, 'inc_net_profit_year_on_year': [20.0] * n,
            'statDate': ['2025-03-31'] * n, 'pubDate': ['2025-04-01'] * n,
        }), table='indicator')
        stub.set_date_table(pd.DataFrame({
            'code': codes, 'pe_ratio': [20.0] * n, 'pb_ratio': [3.0] * n,
        }), table='valuation')
        stub.today = cal[150]
        stub.ctx.current_dt = cal[150] + pd.Timedelta(hours=9)
        mod = stub.load_script(SCRIPT)
        stub.initialize()
        return stub, mod, codes

    yield create, cal
    if previous is None:
        sys.modules.pop('jqdata', None)
    else:
        sys.modules['jqdata'] = previous


@pytest.mark.parametrize('operation', ['buy', 'sell'])
def test_missing_position_is_not_read_through_platform_compatibility_lookup(world, operation):
    create, cal = world
    stub, mod, codes = create(1)
    warnings = []

    class CompatibilityPositions(dict):
        # 聚宽兼容容器读取缺失键时返回零持仓，而不是普通dict的None。
        def __getitem__(self, code):
            if code not in self:
                warnings.append('Security(code=%s) 在 positions 中不存在' % code)
                return SimpleNamespace(total_amount=0, closeable_amount=0, locked_amount=0)
            return super().__getitem__(code)

        def get(self, code, default=None):
            return self[code]

    stub.portfolio.positions = CompatibilityPositions(stub.portfolio.positions)
    if operation == 'buy':
        stub.run_days(cal[150:152])
        assert len(stub.orders) == 1 and mod._held(stub.ctx) == {codes[0]}
    else:
        mod._sell(stub.ctx, mod.get_current_data(), codes[0], 0, 'out_of_pool')
        assert not stub.orders
    assert not warnings, '\n'.join(warnings)


def test_120_day_rank_uses_121_closes_and_excludes_signal_day(world):
    create, cal = world
    stub, mod, codes = create(3)
    # 截止T-1的120日收益为+20%、+10%、0%；T日行情不能倒置排名。
    stub.closes[codes[0]].iloc[29] = 10 / 1.2
    stub.closes[codes[1]].iloc[29] = 10 / 1.1
    stub.closes[codes[0]].iloc[150] = 9
    stub.closes[codes[2]].iloc[150] = 11
    queries = []
    original = mod.get_fundamentals

    def observe(query, date=None):
        queries.append(pd.Timestamp(date))
        return original(query, date=date)

    mod.get_fundamentals = observe
    stub.run_days(cal[150:151])
    assert stub.g.pending['selected'] == codes
    assert [r['momentum'] for r in stub.g.member_rows] == pytest.approx([.2, .1, 0])
    assert queries == [cal[149], cal[149]]
    assert not stub.orders


def test_financial_visibility_hard_boundaries_and_legacy_negative_valuation(world):
    create, cal = world
    stub, _, codes = create(6)
    rows = stub.date_tables['indicator']
    rows.loc[1, 'pubDate'] = str(cal[150].date())
    rows.loc[2, 'roe'] = 3.0
    rows.loc[3, 'inc_net_profit_year_on_year'] = 10.0
    stub.date_tables['valuation'].loc[4, 'pe_ratio'] = 30.0
    stub.date_tables['valuation'].loc[5, ['pe_ratio', 'pb_ratio']] = [-1.0, -1.0]
    stub.run_days(cal[150:151])
    assert set(stub.g.pending['selected']) == {codes[0], codes[5]}


def test_rank_buffer_keeps_30_exits_31_and_new_positions_come_from_top20(world):
    create, _ = world
    _, mod, ranked = create(40)
    holdings = set(ranked[:18]) | {ranked[29], ranked[30]}
    selected, kept = mod.select_targets(ranked, holdings)
    assert ranked[29] in kept and ranked[30] not in selected
    assert ranked[18] in selected and ranked[19] not in selected
    assert len(selected) == len(set(selected)) == 20
    assert set(kept) == holdings - {ranked[30]}


def test_negative_momentum_still_ranks_and_ties_are_deterministic(world):
    create, _ = world
    _, mod, codes = create(3)
    closes = pd.DataFrame({c: [20.] + [10.] * 120 for c in codes})
    ranked, scores = mod.rank_pool(list(reversed(codes)), closes)
    assert ranked == codes and all(score == -.5 for score in scores.values())
    selected, _ = mod.select_targets(ranked, set())
    assert selected == codes


def test_rebalance_calendar_keeps_positions_without_full_liquidation(world):
    create, cal = world
    stub, mod, _ = create()
    stub.run_days(cal[150:193])
    assert [r['signal_day'] for r in stub.g.signal_rows] == [
        cal[i].strftime('%F') for i in (150, 170, 190)]
    assert len(mod._held(stub.ctx)) == 20
    assert len([o for o in stub.orders if o[0] == 'B']) == 20
    assert not [o for o in stub.orders if o[0] == 'S']
    assert {o[1] for o in stub.orders} == {cal[151].strftime('%F')}
    assert stub.portfolio.cash >= 0


def test_overweight_retained_stock_is_trimmed_without_round_trip(world):
    create, cal = world
    stub, mod, _ = create(20)
    stub.run_days(cal[150:152])
    kept = stub.g.target[0]
    amount = stub.portfolio.positions[kept].total_amount
    stub.closes[kept].loc[cal[160]:] = 10.5
    stub.run_days(cal[152:173])
    sells = [o for o in stub.orders if o[0] == 'S']
    assert len(sells) == 1 and sells[0][2] == kept
    assert 0 < sells[0][3] < amount
    assert sells[0][1] == cal[171].strftime('%F')
    assert stub.portfolio.positions[kept].total_amount > 0
    assert not [o for o in stub.orders if o[0] == 'B' and o[1] == sells[0][1] and o[2] == kept]


def test_thin_pool_leaves_cash_and_valid_empty_pool_exits(world):
    create, cal = world
    stub, mod, _ = create(5)
    stub.run_days(cal[150:152])
    assert len(mod._held(stub.ctx)) == 5
    assert stub.portfolio.cash / stub.portfolio.total_value == pytest.approx(.75, abs=.002)
    stub.date_tables['indicator']['roe'] = 0
    stub.run_days(cal[152:173])
    assert not mod._held(stub.ctx) and not stub.g.data_paused
    assert stub.g.signal_rows[-1]['pool_size'] == 0


@pytest.mark.parametrize('api', ['get_extras', 'get_locked_shares'])
def test_data_failure_skips_whole_batch_without_selling_old_positions(world, api):
    create, cal = world
    stub, mod, _ = create()
    stub.run_days(cal[150:170])
    amounts = {c: p.total_amount for c, p in stub.portfolio.positions.items()}
    order_count = len(stub.orders)

    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic unavailable API')

    setattr(mod, api, unavailable)
    stub.run_days(cal[170:173])
    assert stub.g.data_paused and stub.g.pending is None
    assert len(stub.orders) == order_count
    assert {c: p.total_amount for c, p in stub.portfolio.positions.items()} == amounts


def test_short_history_excludes_new_stock_but_missing_held_history_pauses_batch(world):
    create, cal = world
    stub, mod, codes = create(21)
    young = codes[0]
    stub.closes[young].iloc[:70] = float('nan')
    stub.run_days(cal[150:152])
    assert stub.g.pool_size == 21 and stub.g.ranked_size == 20
    assert young not in mod._held(stub.ctx)
    held = stub.g.target[0]
    stub.closes[held].iloc[60] = float('nan')  # 在120日窗内、60日波动窗外
    order_count = len(stub.orders)
    stub.run_days(cal[152:173])
    assert stub.g.data_paused and len(stub.orders) == order_count


@pytest.mark.parametrize('table,column', [('indicator', 'pubDate'), ('valuation', 'pe_ratio')])
def test_unusable_snapshot_is_not_mistaken_for_empty_pool(world, table, column):
    create, cal = world
    stub, mod, _ = create()
    stub.run_days(cal[150:170])
    held = mod._held(stub.ctx)
    order_count = len(stub.orders)
    stub.date_tables[table][column] = None
    stub.run_days(cal[170:173])
    assert stub.g.data_paused and mod._held(stub.ctx) == held
    assert len(stub.orders) == order_count


def test_partial_exit_reserves_slot_retries_and_does_not_chase_buy(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    outgoing = codes[0]
    quantity = stub.portfolio.positions[outgoing].total_amount
    stub.date_tables['indicator'].loc[
        stub.date_tables['indicator']['code'] == outgoing, 'roe'] = 0
    stub.sell_caps[outgoing] = quantity // 2
    buy_count = len([o for o in stub.orders if o[0] == 'B'])
    stub.run_days(cal[170:172])
    assert outgoing in stub.g.exit_pending
    assert len(mod._held(stub.ctx)) == 20
    assert len([o for o in stub.orders if o[0] == 'B']) == buy_count
    assert stub.g.order_rows[-1]['filled'] == quantity // 2
    stub.run_days(cal[172:174])
    assert outgoing not in stub.g.exit_pending and len(mod._held(stub.ctx)) == 19
    assert len([o for o in stub.orders if o[0] == 'B']) == buy_count


def test_rank_drop_is_executed_with_reason_and_replacement(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    # 第二轮信号截止169，120日前为49；让原首只排名跌至最后。
    stub.closes[codes[0]].iloc[49] = 10 / .9
    stub.run_days(cal[170:172])
    assert codes[0] not in mod._held(stub.ctx)
    assert codes[20] in mod._held(stub.ctx)
    assert len(mod._held(stub.ctx)) == 20
    assert any(r['code'] == codes[0] and 'rank_below_buffer' in r['detail']
               for r in stub.g.event_rows)
    decision = next(r for r in stub.g.decision_rows if r['code'] == codes[0] and r['action'] == 'exit')
    assert decision['rank'] == len(codes) and decision['previous_rank'] == 1
    assert decision['pool_reason'] == 'passed' and decision['held_days_at_signal'] == 19
    closed = next(r for r in stub.g.holding_rows if r['code'] == codes[0])
    assert closed['holding_days'] == 20 and closed['exit_reason'] == 'rank_below_buffer'


def test_limit_up_rejection_and_partial_buy_leave_cash_without_backfill(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:151])
    stub.closes[codes[0]].loc[cal[151]:] = 11
    stub.reject_buys.add(codes[1])
    stub.buy_caps[codes[2]] = 100
    stub.run_days(cal[151:155])
    held = mod._held(stub.ctx)
    assert held == set(codes[2:20]) and codes[20] not in held
    assert stub.portfolio.positions[codes[2]].total_amount == 100
    assert len(stub.g.order_rows) == 18
    assert stub.portfolio.cash > 100_000


def test_paused_exit_is_queued_until_it_can_sell(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    stub.date_tables['indicator'].loc[0, 'roe'] = 0
    stub.money[codes[0]].iloc[171] = 0
    stub.run_days(cal[170:172])
    assert codes[0] in stub.g.exit_pending and len(mod._held(stub.ctx)) == 20
    stub.run_days(cal[172:173])
    assert codes[0] not in mod._held(stub.ctx)


def test_stale_signal_and_duplicate_callbacks_do_not_trade_or_double_count(world):
    create, cal = world
    stub, mod, _ = create()
    stub.run_days(cal[150:151])
    stub.run_days(cal[152:153])
    assert not stub.orders and stub.g.pending is None
    assert any(r['kind'] == 'STALE_SIGNAL' for r in stub.g.event_rows)
    count = len(stub.g.nav_rows)
    day_count = stub.g.day
    mod.on_open(stub.ctx)
    mod.on_signal(stub.ctx)
    mod.after_trading_end(stub.ctx)
    assert len(stub.g.nav_rows) == count and stub.g.day == day_count


def test_inflight_sell_prevents_duplicate_order_and_still_occupies_slot(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    outgoing = codes[0]
    stub.date_tables['indicator'].loc[0, 'roe'] = 0
    pos = stub.portfolio.positions[outgoing]
    pos.locked_amount, pos.total_amount = pos.total_amount, 0
    stub.live_orders[99] = SimpleNamespace(security=outgoing, is_buy=False)
    count = len(stub.orders)
    stub.run_days(cal[170:172])
    assert outgoing in stub.g.exit_pending
    assert len(mod._held(stub.ctx)) == 20 and len(stub.orders) == count


def test_small_budget_respects_lots_fees_and_never_overdraws(world):
    create, cal = world
    stub, mod, _ = create(cash=30_000)
    stub.run_days(cal[150:152])
    assert len(mod._held(stub.ctx)) == 20
    assert all(o[3] % 100 == 0 for o in stub.orders)
    assert stub.portfolio.cash >= 0
    assert mod._affordable_shares(1005, 10) == 0  # 滑点与最低佣金会使一手预算不足


def test_tax_switch_and_actual_fill_exports(world):
    create, cal = world
    stub, mod, _ = create()
    stub.ctx.current_dt = pd.Timestamp('2023-08-25')
    mod._set_costs(stub.ctx)
    assert stub.costs[-1].close_tax == .001
    stub.ctx.current_dt = pd.Timestamp('2023-08-28')
    mod._set_costs(stub.ctx)
    assert stub.costs[-1].close_tax == .0005
    stub.run_days(cal[150:173])
    assert stub.log_levels['order'] == 'error'
    assert {'SIGNAL', 'SUBMITTED'} <= {row['kind'] for row in stub.g.event_rows}
    assert not any(' SIGNAL ' in line or ' SUBMITTED ' in line for line in stub.logs)
    mod.on_strategy_end(stub.ctx)
    assert set(stub.exports) == {'pool_rotation_v1_nav.csv', 'pool_rotation_v1_signals.csv',
                                 'pool_rotation_v1_members.csv', 'pool_rotation_v1_events.csv',
                                 'pool_rotation_v1_orders.csv', 'pool_rotation_v1_decisions.csv',
                                 'pool_rotation_v1_holding_periods.csv'}
    assert len(stub.g.order_rows) == len(stub.orders)
    assert any('绝对最大回撤' in line for line in stub.logs)
    assert any('[诊断汇总]' in line for line in stub.logs)
    assert any('[导出完成]' in line for line in stub.logs)
    assert any('[运行汇总]' in line for line in stub.logs)
    assert any('[文件位置]' in line and 'https://www.joinquant.com/research' in line
               for line in stub.logs)
    # 没有平仓样本时也保留表头，下载后可以直接读取。
    periods = pd.read_csv(StringIO(stub.exports['pool_rotation_v1_holding_periods.csv']))
    assert periods.empty and 'holding_days' in periods.columns


@pytest.mark.parametrize('failure,reason', [
    ('roe', 'roe_low'), ('profit', 'profit_growth_low'),
    ('roe_and_profit', 'roe_low|profit_growth_low'),
    ('pe', 'pe_high'), ('pb', 'pb_high'), ('st', 'st'),
    ('paused', 'paused'), ('vol', 'vol_high'), ('unlock', 'unlock_90d'),
    ('missing_financial', 'financial_missing'),
    ('not_visible', 'financial_not_visible_or_invalid'),
])
def test_exit_diagnostics_explain_pool_stage_and_actual_holding_period(world, failure, reason):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    code = codes[0]
    if failure in ('roe', 'roe_and_profit'):
        stub.date_tables['indicator'].loc[0, 'roe'] = 0
    if failure in ('profit', 'roe_and_profit'):
        stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 0
    if failure == 'pe':
        stub.date_tables['valuation'].loc[0, 'pe_ratio'] = 30
    if failure == 'pb':
        stub.date_tables['valuation'].loc[0, 'pb_ratio'] = 5
    if failure == 'st':
        original = mod.get_extras

        def with_st(*args, **kwargs):
            frame = original(*args, **kwargs)
            frame.loc[frame.index[-1], code] = True
            return frame

        mod.get_extras = with_st
    if failure == 'paused':
        stub.money[code].iloc[170] = 0
    if failure == 'vol':
        stub.closes[code].iloc[110:170] = [10., 12.] * 30
    if failure == 'unlock':
        mod.get_locked_shares = lambda **kwargs: pd.DataFrame({'code': [code]})
    if failure == 'missing_financial':
        stub.date_tables['indicator'] = stub.date_tables['indicator'].iloc[1:].copy()
    if failure == 'not_visible':
        stub.date_tables['indicator'].loc[0, 'pubDate'] = str(cal[171].date())
    stub.run_days(cal[170:172])
    decision = next(r for r in stub.g.decision_rows if r['code'] == code and r['action'] == 'exit')
    assert decision['exit_reason'] == 'out_of_pool' and decision['pool_reason'] == reason
    assert decision['held_days_at_signal'] == 19
    closed = next(r for r in stub.g.holding_rows if r['code'] == code)
    assert closed['holding_days'] == 20 and closed['pool_reason'] == reason
    assert closed['signal_day'] == str(cal[170].date())
    assert closed['entry_source'] == closed['closure_source'] == 'filled_order'
    order = next(r for r in stub.g.order_rows if r['code'] == code and not r['is_buy'])
    assert order['reason'] == 'out_of_pool' and order['pool_reason'] == reason


def test_partial_exit_does_not_close_holding_period_until_flat(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    code = codes[0]
    stub.date_tables['indicator'].loc[0, 'roe'] = 0
    stub.sell_caps[code] = stub.portfolio.positions[code].total_amount // 2
    stub.run_days(cal[170:172])
    assert not stub.g.holding_rows and code in stub.g.hold_entries
    stub.run_days(cal[172:173])
    closed = next(r for r in stub.g.holding_rows if r['code'] == code)
    assert closed['holding_days'] == 21 and closed['pool_reason'] == 'roe_low'
    assert closed['signal_day'] == str(cal[170].date())
    mod.on_strategy_end(stub.ctx)
    assert mod._exit_reason_counts(stub.g.holding_rows) == {'out_of_pool/roe_low': 1}


def test_data_pause_is_recorded_without_false_exit_or_rank_update(world):
    create, cal = world
    stub, mod, _ = create()
    stub.run_days(cal[150:170])
    previous_ranks = stub.g.previous_ranks.copy()

    def unavailable(**kwargs):
        raise RuntimeError('synthetic locked shares unavailable')

    mod.get_locked_shares = unavailable
    stub.run_days(cal[170:172])
    paused = [r for r in stub.g.decision_rows if r['action'] == 'data_pause']
    assert len(paused) == 20 and all(not r['exit_reason'] for r in paused)
    assert all('unavailable' in r['data_error'] for r in paused)
    assert stub.g.previous_ranks == previous_ranks and not stub.g.holding_rows


def test_reselected_pending_exit_preserves_original_entry_and_cancels_old_reason(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[150:170])
    code = codes[0]
    stub.date_tables['indicator'].loc[0, 'roe'] = 0
    original = mod.get_current_data

    def with_blocked_exit():
        frame = original()
        if cal[171] <= stub.today <= cal[190]:
            frame[code].low_limit = frame[code].last_price
        return frame

    mod.get_current_data = with_blocked_exit
    stub.run_days(cal[170:190])
    assert code in stub.g.exit_pending
    stub.date_tables['indicator'].loc[0, 'roe'] = 4
    stub.run_days(cal[190:192])
    assert code not in stub.g.exit_pending and code not in stub.g.exit_details
    assert code in mod._held(stub.ctx) and not stub.g.holding_rows
    assert stub.g.hold_entries[code]['entry_date'] == str(cal[151].date())


@pytest.mark.parametrize('setting,value', [('KEEP_RANK', 19), ('MOMENTUM_DAYS', True),
                                         ('ROTATE_EVERY', 0)])
def test_invalid_contract_parameters_are_rejected(world, setting, value):
    create, _ = world
    stub, mod, _ = create()
    setattr(mod, setting, value)
    with pytest.raises(ValueError):
        stub.initialize()
