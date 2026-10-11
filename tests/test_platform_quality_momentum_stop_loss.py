# -*- coding: utf-8 -*-
"""v4.1.1实际持仓风险退出的模拟测试；不验证收益或聚宽盘中撮合。"""
import sys
from io import StringIO
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_platform_pool_rotation import RotationStub  # noqa: E402


BASE = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum'


class LossRotationStub(RotationStub):
    def _order_target(self, code, amount):
        pos = self.portfolio.positions.get(code)
        quantity = pos.total_amount if pos else 0
        cost = pos.avg_cost if pos else 0
        order = super()._order_target(code, amount)
        if order is not None and order.is_buy:
            pos = self.portfolio.positions[code]
            pos.avg_cost = (quantity * cost + order.filled * order.price) / pos.total_amount
        return order


@pytest.fixture
def world(monkeypatch):
    # 安装桩会替换平台模块；使用monkeypatch恢复其他测试原有的模块。
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    factors = ModuleType('jqfactor')
    monkeypatch.setitem(sys.modules, 'jqfactor', factors)
    cal = pd.bdate_range('2024-01-02', periods=360)

    def create(script='pool_rotation_v4_1_1.py'):
        codes = ['600%03d.XSHG' % i for i in range(8)]
        closes = {c: pd.Series(10.0, index=cal) for c in codes}
        stub = LossRotationStub(cal, closes).install()
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


def enter(world):
    create, cal = world
    stub, mod, codes = create()
    stub.run_days(cal[270:272])
    assert mod._held(stub.ctx) == set(codes[:6])
    return stub, mod, codes, cal


@pytest.mark.parametrize('price,stopped', [(9.001, False), (9.0, True), (8.8, True)])
def test_daily_loss_boundary_closes_whole_position_and_leaves_cash(world, price, stopped):
    stub, mod, codes, cal = enter(world)
    code = codes[0]
    quantity = stub.portfolio.positions[code].total_amount
    buys = len([o for o in stub.orders if o[0] == 'B'])
    stub.closes[code].loc[cal[272]:] = price
    stub.run_days(cal[272:273])
    assert (code not in mod._held(stub.ctx)) == stopped
    assert len([o for o in stub.orders if o[0] == 'B']) == buys
    if stopped:
        assert stub.orders[-1][0] == 'S' and stub.orders[-1][3] == quantity
        assert code not in stub.g.target and code in stub.g.stop_blocked
        assert stub.g.holding_rows[-1]['exit_reason'] == 'stop_loss'
        assert stub.g.holding_rows[-1]['stop_avg_cost'] == 10.0
        assert stub.g.holding_rows[-1]['stop_price'] == price


def test_gap_records_actual_price_without_assuming_threshold_fill(world):
    stub, mod, codes, cal = enter(world)
    stub.closes[codes[0]].loc[cal[272]:] = 7.0
    stub.run_days(cal[272:273])
    order = stub.g.order_rows[-1]
    assert order['reason'] == 'stop_loss' and order['price'] == 7.0
    assert order['stop_return'] == pytest.approx(-.3)
    assert order['stop_threshold'] == .1


def test_stop_uses_platform_cost_after_corporate_action(world):
    stub, mod, codes, cal = enter(world)
    pos = stub.portfolio.positions[codes[0]]
    pos.total_amount *= 2
    pos.avg_cost /= 2
    stub.closes[codes[0]].loc[cal[272]:] = 5.0
    stub.run_days(cal[272:273])
    assert codes[0] in mod._held(stub.ctx)
    assert not stub.g.stop_blocked


def test_loss_on_execution_day_overrides_yesterdays_retention_and_rebalance(world):
    stub, mod, codes, cal = enter(world)
    stub.run_days(cal[272:291])  # 290日已生成留存计划，291日执行。
    assert codes[0] in stub.g.pending['selected']
    stub.closes[codes[0]].loc[cal[291]:] = 9.0
    stub.run_days(cal[291:292])
    assert codes[0] not in mod._held(stub.ctx)
    day_orders = [o for o in stub.orders if o[1] == str(cal[291].date()) and o[2] == codes[0]]
    assert len(day_orders) == 1 and day_orders[0][0] == 'S'
    assert codes[6] not in mod._held(stub.ctx)


def test_trigger_day_signal_cannot_reenter_but_later_fresh_signal_can(world):
    stub, mod, codes, cal = enter(world)
    stub.run_days(cal[272:290])
    stub.closes[codes[0]].loc[cal[290]:] = 9.0
    stub.run_days(cal[290:292])
    assert codes[0] not in mod._held(stub.ctx)
    assert codes[0] in stub.g.stop_blocked
    assert len(mod._held(stub.ctx)) == 5
    stub.run_days(cal[292:312])
    assert codes[0] in mod._held(stub.ctx)
    assert codes[0] not in stub.g.stop_blocked
    assert stub.portfolio.positions[codes[0]].avg_cost == 9.0
    assert any(r['kind'] == 'STOP_REENTRY_ELIGIBLE' for r in stub.g.event_rows)


@pytest.mark.parametrize('blocked_by', ['paused', 'lower_limit', 'no_closeable_shares'])
def test_blocked_sale_is_sticky_after_rebound_and_retried(world, blocked_by):
    stub, mod, codes, cal = enter(world)
    code = codes[0]
    stub.closes[code].loc[cal[272]:] = 9.0
    stub.today = cal[272]
    stub.ctx.current_dt = cal[272] + pd.Timedelta(hours=9, minutes=31)
    if blocked_by == 'no_closeable_shares':
        stub.portfolio.positions[code].closeable_amount = 0
    else:
        stub.quote_overrides[code] = ({'paused': True} if blocked_by == 'paused'
                                      else {'low_limit': 9.0})
    mod.on_open(stub.ctx)
    assert stub.g.exit_pending[code] == 'stop_loss'
    assert code in mod._held(stub.ctx)
    assert any('blocked_by=' + blocked_by in r['detail'] for r in stub.g.event_rows)
    stub.quote_overrides.clear()
    stub.closes[code].loc[cal[273]:] = 10.2
    stub.run_days(cal[273:274])
    assert code not in mod._held(stub.ctx)
    assert stub.g.order_rows[-1]['reason'] == 'stop_loss'
    assert stub.g.order_rows[-1]['stop_price'] == 9.0


def test_new_signal_does_not_cancel_unfilled_stop_even_if_rank_is_one(world):
    stub, mod, codes, cal = enter(world)
    code = codes[0]
    stub.quote_overrides[code] = {'low_limit': 9.0}
    stub.closes[code].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:292])
    assert stub.g.exit_pending[code] == 'stop_loss' and code in stub.g.stop_exits
    assert code not in stub.g.target
    signal_exit = next(r for r in stub.g.decision_rows
                       if r['code'] == code and r['signal_day'] == str(cal[290].date()))
    assert signal_exit['rank'] == 1 and signal_exit['exit_reason'] == 'stop_loss'
    stub.quote_overrides.clear()
    stub.closes[code].loc[cal[292]:] = 10.0
    stub.run_days(cal[292:293])
    assert code not in mod._held(stub.ctx)
    assert stub.g.holding_rows[-1]['stop_trigger_time'].startswith(str(cal[272].date()))


def test_partial_stop_retries_remaining_shares_with_original_trigger(world):
    stub, mod, codes, cal = enter(world)
    code = codes[0]
    quantity = stub.portfolio.positions[code].total_amount
    stub.sell_caps[code] = 100
    stub.closes[code].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:273])
    assert stub.portfolio.positions[code].total_amount == quantity - 100
    assert not stub.g.holding_rows and code in stub.g.stop_exits
    del stub.sell_caps[code]
    stub.closes[code].loc[cal[273]:] = 10.0
    stub.run_days(cal[273:274])
    assert code not in mod._held(stub.ctx)
    assert stub.g.holding_rows[-1]['exit_reason'] == 'stop_loss'
    assert stub.g.holding_rows[-1]['stop_price'] == 9.0
    assert len([r for r in stub.g.event_rows if r['kind'] == 'STOP_LOSS']) == 1
    assert len([r for r in stub.g.order_rows if r['reason'] == 'stop_loss']) == 2


def test_pool_data_pause_does_not_disable_daily_stop(world):
    stub, mod, codes, cal = enter(world)
    stub.run_days(cal[272:290])

    def missing(*args, **kwargs):
        raise RuntimeError('financial source unavailable')

    mod.build_pool = missing
    stub.run_days(cal[290:291])
    assert stub.g.data_paused and stub.g.pending is None
    stub.closes[codes[0]].loc[cal[291]:] = 9.0
    stub.run_days(cal[291:292])
    assert codes[0] not in mod._held(stub.ctx)
    assert stub.g.order_rows[-1]['reason'] == 'stop_loss'


@pytest.mark.parametrize('cost', [0, None, float('nan'), float('inf')])
def test_missing_cost_warns_and_prevents_rebalance_buy_without_invented_stop(world, cost):
    stub, mod, codes, cal = enter(world)
    code = codes[0]
    stub.portfolio.positions[code].avg_cost = cost
    stub.closes[code].loc[cal[272]:] = 9.1
    stub.g.pending = {'signal_day': cal[271], 'selected': codes[:6], 'exit_reasons': {}}
    count = len(stub.orders)
    stub.run_days(cal[272:273])
    assert code in stub.g.loss_data_blocked and code not in stub.g.stop_blocked
    assert not any(o[2] == code for o in stub.orders[count:])
    assert any(r['kind'] == 'LOSS_DATA_MISSING' for r in stub.g.event_rows)


def test_invalid_price_does_not_invent_a_loss(world):
    stub, mod, codes, cal = enter(world)
    stub.quote_overrides[codes[0]] = {'last_price': None}
    stub.run_days(cal[272:273])
    assert codes[0] in stub.g.loss_data_blocked
    assert not stub.g.stop_blocked
    assert codes[0] in mod._held(stub.ctx)


def test_stop_exclusion_keeps_original_top_six_without_promoting_seventh(world):
    _, mod, codes, _ = enter(world)
    industries = {c: (c, '') for c in codes}
    selected, kept, _ = mod.select_targets(codes, {codes[0]}, industries, {codes[0]})
    assert selected == codes[1:6] and not kept


def test_outstanding_buy_is_canceled_before_stop_sale(world):
    from types import SimpleNamespace

    stub, mod, codes, cal = enter(world)
    stub.live_orders[99] = SimpleNamespace(security=codes[0], is_buy=True)
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:273])
    assert not stub.live_orders
    assert codes[0] not in mod._held(stub.ctx)
    assert any(r['kind'] == 'STOP_CANCEL_BUY' for r in stub.g.event_rows)


def test_inflight_sell_occupies_slot_and_is_not_duplicated(world):
    from types import SimpleNamespace

    stub, mod, codes, cal = enter(world)
    pos = stub.portfolio.positions[codes[0]]
    pos.locked_amount, pos.total_amount = pos.total_amount, 0
    stub.live_orders[99] = SimpleNamespace(security=codes[0], is_buy=False)
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    count = len(stub.orders)
    stub.run_days(cal[272:273])
    assert len(stub.orders) == count
    assert codes[0] in mod._held(stub.ctx) and codes[0] in stub.g.stop_exits


def test_failed_fresh_signal_does_not_release_reentry_block(world):
    stub, mod, codes, cal = enter(world)
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:290])
    assert codes[0] not in mod._held(stub.ctx)
    original = mod.select_targets

    def failed_selection(*args):
        raise RuntimeError('selection unavailable')

    mod.select_targets = failed_selection
    stub.run_days(cal[290:292])
    assert stub.g.data_paused and codes[0] in stub.g.stop_blocked
    assert codes[0] not in mod._held(stub.ctx)
    mod.select_targets = original
    stub.run_days(cal[292:312])
    assert codes[0] not in stub.g.stop_blocked
    # 解禁仅恢复资格；该票下跌现已进入动量窗口，名次不足仍不买入。
    assert codes[0] not in mod._held(stub.ctx)
    assert any(r['kind'] == 'STOP_REENTRY_ELIGIBLE' and r['code'] == codes[0]
               for r in stub.g.event_rows)


def test_cancel_still_processing_blocks_duplicate_sell_and_buy(world):
    from types import SimpleNamespace

    stub, mod, codes, cal = enter(world)
    stub.live_orders[99] = SimpleNamespace(security=codes[0], is_buy=True)
    mod.cancel_order = lambda order: None  # 尚未确认撤单，订单仍在途。
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    count = len(stub.orders)
    stub.run_days(cal[272:273])
    assert len(stub.orders) == count and codes[0] in stub.g.stop_exits
    assert stub.g.exit_pending[codes[0]] == 'stop_loss'


def test_rejected_stop_order_remains_pending_and_retries(world):
    stub, mod, codes, cal = enter(world)
    original = mod.order_target
    mod.order_target = lambda code, quantity: None
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:273])
    assert codes[0] in stub.g.stop_exits
    assert not stub.g.holding_rows
    mod.order_target = original
    stub.run_days(cal[273:274])
    assert codes[0] not in mod._held(stub.ctx)
    assert stub.g.holding_rows[-1]['exit_reason'] == 'stop_loss'


def test_normal_rotation_matches_v4_1_when_no_stop_is_triggered(world):
    create, cal = world
    baseline, _, _ = create('pool_rotation_v4_1.py')
    baseline.run_days(cal[270:315])
    variant, _, _ = create()
    variant.run_days(cal[270:315])
    assert variant.orders == baseline.orders
    assert variant.g.signal_rows[0]['selected'] == baseline.g.signal_rows[0]['selected']
    assert variant.g.nav_rows[-1]['equity'] == baseline.g.nav_rows[-1]['equity']
    assert not variant.g.stop_blocked


def test_exports_keep_stop_trigger_and_duplicate_callbacks_do_not_retrade(world):
    stub, mod, codes, cal = enter(world)
    stub.closes[codes[0]].loc[cal[272]:] = 9.0
    stub.run_days(cal[272:273])
    count = len(stub.orders)
    rows = len(stub.g.nav_rows)
    mod.on_open(stub.ctx)
    mod.on_signal(stub.ctx)
    mod.after_trading_end(stub.ctx)
    assert len(stub.orders) == count and len(stub.g.nav_rows) == rows
    mod.on_strategy_end(stub.ctx)
    assert len(stub.exports) == 7
    assert all(name.startswith('pool_rotation_v4_1_1_') for name in stub.exports)
    periods = pd.read_csv(StringIO(stub.exports['pool_rotation_v4_1_1_holding_periods.csv']))
    assert periods.iloc[0]['exit_reason'] == 'stop_loss'
    assert periods.iloc[0]['stop_return'] == pytest.approx(-.1)
    assert any('[止损汇总]' in line for line in stub.logs)


@pytest.mark.parametrize('threshold', [0, -1, 1, float('nan'), float('inf'), True])
def test_invalid_threshold_is_rejected_before_trading(world, threshold):
    stub, mod, _, _ = enter(world)
    mod.STOP_LOSS_PERCENT = threshold
    with pytest.raises(ValueError, match='STOP_LOSS_PERCENT'):
        mod.initialize(stub.ctx)
