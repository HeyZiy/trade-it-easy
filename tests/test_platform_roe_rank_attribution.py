"""按真实成交及入场排名归因，不改变原轮动交易。"""
from types import SimpleNamespace

import pandas as pd
import pytest

from test_platform_roe_rotation_revisions import fundamentals, load_world


SCRIPT = 'roe_rotation_v1_1_2.py'


def world(codes=None):
    codes = codes or ['600001.XSHG', '600002.XSHG']
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    stub, mod = load_world(SCRIPT, rows)
    records = []
    mod.record = lambda **values: records.append(values)
    return stub, mod, records


def order(code, buy, qty, price, cost=None):
    return SimpleNamespace(security=code, is_buy=buy, filled=qty,
                           price=price, avg_cost=cost)


def test_same_day_rebuy_keeps_old_sell_rank_and_tags_actual_new_buy():
    stub, mod, records = world()
    code = '600001.XSHG'
    stub.portfolio.positions[code] = SimpleNamespace(total_amount=100)
    mod.g.rank_owned[code] = 1
    mod.g.rank_today_old = {code: 1}
    mod.g.rank_today_buy = {code: 5}
    mod.g.rank_stats[5]['selected'] = 1
    # 故意把新买入放在卖出前；归因不能依赖字典顺序。
    mod.get_orders = lambda: {
        1: order(code, True, 100, 12),
        2: order(code, False, 200, 12, 10),
        3: order('600002.XSHG', True, 0, None),
    }
    mod.after_trading_end(stub.ctx)
    assert mod.g.rank_stats[1]['pnl'] == 400
    assert mod.g.rank_stats[1]['sells'] == 1
    assert mod.g.rank_stats[5]['buys'] == 1
    assert mod.g.rank_owned[code] == 5
    assert records[-1]['R01_平仓贡献_pct'] == pytest.approx(.04)
    mod.after_trading_end(stub.ctx)
    assert mod.g.rank_stats[1]['sells'] == 1  # 重复回调不重复累计


def test_partial_sell_keeps_rank_and_uses_platform_adjusted_cost():
    stub, mod, _ = world()
    code = '600001.XSHG'
    stub.portfolio.positions[code] = SimpleNamespace(total_amount=150)
    mod.g.rank_owned[code] = 3
    mod.g.rank_today_old = {code: 3}
    # 平台 avg_cost 已反映除权等因素；不使用旧买入价或旧股数计算。
    mod._collect_rank_orders(stub.ctx, {1: order(code, False, 50, 8, 6)})
    assert mod.g.rank_stats[3]['pnl'] == 100
    assert mod.g.rank_stats[3]['cost'] == 300
    assert mod.g.rank_owned[code] == 3
    stub.ctx.current_dt += pd.Timedelta(days=1)
    stub.portfolio.positions[code].total_amount = 0
    mod._collect_rank_orders(stub.ctx, {2: order(code, False, 150, 5, 6)})
    assert mod.g.rank_stats[3]['pnl'] == -50
    assert mod.g.rank_stats[3]['wins'] == 1
    assert code not in mod.g.rank_owned


def test_unfilled_and_unknown_sells_are_not_fabricated_as_rank_profit():
    stub, mod, _ = world()
    mod.g.rank_today_buy = {'600001.XSHG': 1}
    mod._collect_rank_orders(stub.ctx, {
        1: order('600001.XSHG', True, 0, None),
        2: order('600002.XSHG', False, 100, 10, 8),
    })
    assert mod.g.rank_stats[1]['buys'] == 0
    assert not mod.g.rank_owned
    assert mod.g.rank_unattributed_sells == 1
    assert sum(s['pnl'] for s in mod.g.rank_stats.values()) == 0


def test_annual_attribution_uses_sell_year_and_reports_unsampled_ranks():
    stub, mod, _ = world()
    mod.g.rank_today_old = {'600001.XSHG': 1}
    stub.ctx.current_dt = pd.Timestamp('2026-01-05')
    mod._collect_rank_orders(stub.ctx, {1: order('600001.XSHG', False, 100, 11, 10)})
    assert mod.g.rank_years == {2026: {1: 100}}
    mod._log_rank_summary(stub.ctx)
    assert any('10 | 0 | 0 | 0 | 0 | +0.00 | 无样本' in line for line in stub.logs)
    assert any('排名年度平仓贡献 2026' in line for line in stub.logs)


def test_rank_diagnostics_preserve_baseline_orders_and_count_small_lot_failures():
    codes = ['600%03d.XSHG' % i for i in range(12)]
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[0]][:] = 2000.0  # 单仓预算不足一手；入选但买不到
    base_stub, _ = load_world('roe_rotation_v1_1.py', rows, closes)
    base_stub.run_days(cal[70:93])
    stub, mod = load_world(SCRIPT, rows, closes)
    mod.record = lambda **values: None
    captured = {}
    original_sell, original_buy = mod.order_target, mod.order_target_value

    def sell(code, amount):
        pos = stub.portfolio.positions[code]
        qty, cost = pos.total_amount, pos.avg_cost
        result = original_sell(code, amount)
        captured.setdefault(stub.today.date(), []).append(
            order(code, False, qty - pos.total_amount, stub.price(code), cost))
        return result

    def buy(code, value):
        before = stub.portfolio.positions.get(code)
        qty = before.total_amount if before is not None else 0
        result = original_buy(code, value)
        # 共享桩只维护现金和股数；本测试适配平台实际提供的持仓成本字段。
        if stub.portfolio.positions[code].total_amount > qty:
            stub.portfolio.positions[code].avg_cost = stub.price(code)
        captured.setdefault(stub.today.date(), []).append(
            order(code, True, stub.portfolio.positions[code].total_amount - qty,
                  stub.price(code)))
        return result

    mod.order_target, mod.order_target_value = sell, buy
    mod.get_orders = lambda: dict(enumerate(captured.get(stub.today.date(), [])))
    for day in cal[70:93]:
        stub.run_days([day])
        mod.after_trading_end(stub.ctx)
    assert stub.orders == base_stub.orders
    assert stub.portfolio.total_value == pytest.approx(base_stub.portfolio.total_value)
    assert mod.g.hold_since == base_stub.g.hold_since
    assert mod.g.rank_stats[1]['selected'] == 2
    assert mod.g.rank_stats[1]['buys'] == 0
    assert sum(s['sells'] for s in mod.g.rank_stats.values()) == 9
    assert mod.g.rank_unattributed_sells == 0
