# -*- coding: utf-8 -*-
"""ETF 再平衡测试：整手口径单点（executor）+ compare 纯计算核（价格注入）+ RebalancePlan。

全 fake 注入零网络：compare/build_plan 不再自取行情，测试直接传 prices dict。
"""
from src.etf.rebalancer import RebalancePlan, ETFRebalancer
from src.etf import config as _config
from src.mx.executor import floor_lot, round_lot


# ══════════════ 整手口径单点（买卖两套语义）══════════════

def test_round_lot_always_at_least_one_lot():
    assert round_lot(1700.9) == 1700      # 正常向下取整
    assert round_lot(56) == 100           # 有量即强凑一手（买侧现口径）


def test_floor_lot_can_go_to_zero():
    assert floor_lot(280) == 200
    assert floor_lot(99) == 0             # 不足一手 → 0，调用方跳过


# ══════════════ compare：价格注入 + 卖侧钳制 ══════════════

R = ETFRebalancer()
TARGET = R.calculate_target()


def _pos(code, name, count, price, mv=None):
    return {"code": code, "name": name, "count": count, "current_price": price,
            "market_value": mv if mv is not None else count * price}


def _orders_by_code(orders):
    return {o.code: o for o in orders}


def test_unheld_target_buys_with_injected_price():
    """未持仓的基准 ETF：价格经 prices 参数注入（决策核零 I/O）。"""
    orders, dev, odd = R.compare(TARGET, [], 100_000,
                                 prices={"563360": 1.0})
    o = _orders_by_code(orders)["563360"]
    assert o.action == "buy" and o.quantity == round_lot(17000)  # 0.17 × 10万
    assert dev > 0 and odd == []


def test_missing_price_skips_buy_silently():
    """补价失败（prices 无此码）：不产生 0 价除零、不出单。"""
    orders, _, _ = R.compare(TARGET, [], 100_000, prices={})
    assert "563360" not in _orders_by_code(orders)


def test_position_price_wins_over_injection():
    """持仓自带价格优先，prices 不覆盖。"""
    positions = [_pos("515180", "红利ETF", 10000, 1.0)]  # 市值 1 万，占 20% > 目标 14%
    orders, _, _ = R.compare(TARGET, positions, 50_000,
                             prices={"515180": 99.0})
    o = _orders_by_code(orders)["515180"]
    assert o.action == "sell"
    assert o.quantity == floor_lot(min(int(abs(o.amount) / 1.0), 10000))


def test_odd_lot_leg_becomes_todo_not_abort_order():
    """残腿：应卖但持仓不足一手 → 不出卖单，
    记入 odd_lots 供报告列手动待办。"""
    positions = [_pos("515180", "红利ETF", 50, 1.0, mv=50)]
    orders, _, odd = R.compare(TARGET, positions, 200)
    assert "515180" not in _orders_by_code(orders)
    assert odd == [{"code": "515180", "name": "红利ETF", "count": 50}]


def test_sell_clamped_to_held_and_floored():
    """应卖 280 股、持仓 350 → 整手 200（向下取整，不多卖）。"""
    positions = [_pos("515180", "红利ETF", 350, 1.0, mv=350)]
    orders, _, odd = R.compare(TARGET, positions, 500)
    o = _orders_by_code(orders)["515180"]
    assert o.action == "sell" and o.quantity == 200
    assert odd == []


def test_rebalance_is_market_state_free():
    """契约：核心再平衡与市场状态完全无关。

    本测试锁住契约本身：任何人再把状态参数加回来都会红。
    """
    import inspect
    for fn in (R.compare, R.build_plan):
        assert "gate_state" not in inspect.signature(fn).parameters, fn.__name__


def test_gold_trimmed_regardless_of_market_state():
    """黄金超目标即减——任何状态下同规则。"""
    positions = [_pos("159934", "黄金ETF", 1000, 1.0, mv=1000)]
    orders, _, _ = R.compare(TARGET, positions, 2000)
    assert "159934" in _orders_by_code(orders)
    assert _orders_by_code(orders)["159934"].action == "sell"


# ══════════════ build_plan：纯值产物 ══════════════

def _at_target(total=50_000.0, **overrides):
    """构建"全部资产恰在目标权重"的组合；overrides 传 {code: 实际权重} 制造偏离。

    注意必须先铺满组合：只放一只持仓会让其余基准资产变成巨额未持仓偏离，
    测不到"小幅偏离"场景。
    """
    positions = []
    for a in _config.CORE_BASELINE:
        if a.code == "CASH":
            continue
        w = overrides.get(a.code, a.neutral_weight)
        mv = w * total
        positions.append(_pos(a.code, a.name, int(mv), 1.0, mv=mv))
    return positions


def test_build_plan_carries_trigger_verdict():
    """plan 同时携带订单、触发层结论 should 与执行层结论 reason。"""
    flat = R.build_plan(TARGET, _at_target(), 50_000)
    assert isinstance(flat, RebalancePlan)
    assert flat.orders == [] and flat.odd_lots == []
    assert flat.should is False, flat.reason
    assert "无指令" in flat.reason, flat.reason

    # 未持仓 + 有行情 → 买入指令，总偏离巨大 → 触发层达线
    plan = R.build_plan(TARGET, [], 100_000, prices={"563360": 1.0})
    assert plan.orders and plan.should is True
    # 卖出在前（高波动先减）、买入按波动率升序——纯结构断言不锁死具体顺序
    assert all(o.action == "buy" for o in plan.orders)


def test_reason_matches_execution_decision():
    """文案与执行一致：有指令时 reason 不得说"无指令/无需"，
    单类 2%~5% 的批次照样执行（执行门＝有指令即执行），reason 如实写触发层未达线。
    """
    # 红利 17% vs 目标 14% → 单类偏离 3%（> 2% 出指令，≤ 5% 触发层不达线）
    plan = R.build_plan(TARGET, _at_target(**{"515180": 0.17}), 50_000)
    assert plan.orders, plan.reason
    assert plan.should is False, plan.reason
    assert "无指令" not in plan.reason and "无需" not in plan.reason, plan.reason
    assert "笔指令" in plan.reason and "触发层" in plan.reason, plan.reason
    assert "最大单类 3.0%" in plan.reason, plan.reason


def test_trigger_layer_does_not_block_execution():
    """契约：触发层只分级、不拦执行——plan.should 为 False 时 orders 仍非空，
    消费方按 orders 执行（改执行门到 should 属策略语义变更，需先裁决）。"""
    plan = R.build_plan(TARGET, _at_target(**{"515180": 0.17}), 50_000)
    assert plan.should is False and len(plan.orders) > 0


def test_trigger_fires_on_magnitude_not_on_executability():
    """总偏离够大就报 True，即使当时无单可出——不许出现"总偏离 71% 却 should=False"。"""
    no_prices = R.build_plan(TARGET, [], 100_000)
    assert no_prices.orders == [] and no_prices.should is True, no_prices.reason
    assert "无指令" in no_prices.reason and "触发层" in no_prices.reason, no_prices.reason


def test_no_orders_reason_is_about_the_order_filter_not_absence_of_deviation():
    """无指令时也不能说"无偏离"：偏离存在但都 < 2%（碎股噪声）。"""
    plan = R.build_plan(TARGET, _at_target(**{"515180": 0.145}), 50_000)
    assert plan.orders == [], plan.reason
    assert "无指令" in plan.reason and "2%" in plan.reason, plan.reason
    assert "无偏离" not in plan.reason, plan.reason


# 卫星仓买侧场景测试已随日频轮动迁移迁往 tests/test_industry_momentum.py

