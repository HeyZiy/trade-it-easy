# -*- coding: utf-8 -*-
"""ETF 再平衡测试：整手口径单点（executor）+ compare 纯计算核（价格注入）。

全 fake 注入零网络：compare 不自取行情，测试直接传 prices dict。
"""
from src.etf.rebalancer import ETFRebalancer
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

R = ETFRebalancer(_config.example_baseline())
TARGET = {a.code: a.neutral_weight for a in _config.example_baseline()}


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
    assert o.action == "buy" and o.quantity == round_lot(round(TARGET["563360"] * 100_000, 2))
    assert dev > 0 and odd == []


def test_missing_price_skips_buy_silently():
    """补价失败（prices 无此码）：不产生 0 价除零、不出单。"""
    orders, _, _ = R.compare(TARGET, [], 100_000, prices={})
    assert "563360" not in _orders_by_code(orders)


def test_position_price_wins_over_injection():
    """持仓自带价格优先，prices 不覆盖。"""
    positions = [_pos("515180", "红利ETF", 10000, 1.0)]  # 市值 1 万，占 20% > 目标 7%
    orders, _, _ = R.compare(TARGET, positions, 50_000,
                             prices={"515180": 99.0})
    o = _orders_by_code(orders)["515180"]
    assert o.action == "sell"
    assert o.quantity == floor_lot(min(int(abs(o.amount) / 1.0), 10000))


def test_odd_lot_leg_becomes_todo_not_abort_order():
    """残腿：应卖但持仓不足一手 → 不出卖单，
    记入 odd_lots 供报告列手动待办。
    夹具金额抬到 MIN_TRADE_AMOUNT(400) 以上，隔离阈值变量、单测整手残腿机制。"""
    positions = [_pos("515180", "红利ETF", 50, 10.0, mv=500)]
    orders, _, odd = R.compare(TARGET, positions, 1000)
    assert "515180" not in _orders_by_code(orders)
    assert odd == [{"code": "515180", "name": "515180", "count": 50}]


def test_sell_clamped_to_held_and_floored():
    """应卖 308 股、持仓 350 → 整手 300（向下取整，不多卖）。
    夹具金额抬到 MIN_TRADE_AMOUNT(400) 以上，隔离阈值变量。"""
    positions = [_pos("515180", "红利ETF", 350, 10.0, mv=3500)]
    orders, _, odd = R.compare(TARGET, positions, 5000)
    o = _orders_by_code(orders)["515180"]
    assert o.action == "sell" and o.quantity == 300
    assert odd == []


def test_rebalance_is_market_state_free():
    """契约：核心再平衡与市场状态完全无关。

    本测试锁住契约本身：任何人再把状态参数加回来都会红。
    """
    import inspect
    for fn in (R.compare,):
        assert "gate_state" not in inspect.signature(fn).parameters, fn.__name__


def test_gold_trimmed_regardless_of_market_state():
    """黄金超目标即减——任何状态下同规则。"""
    positions = [_pos("159934", "黄金ETF", 1000, 1.0, mv=1000)]
    orders, _, _ = R.compare(TARGET, positions, 2000)
    assert "159934" in _orders_by_code(orders)
    assert _orders_by_code(orders)["159934"].action == "sell"

# ══════════════ 持仓归属拆分：看台账 strategy 归属，不看代码名单 ══════════════

def test_split_rotation_uses_account_label():
    """非 core 标签的持仓进卫星侧；无标签（真实券商持仓）默认归核心。"""
    positions = [
        _pos("563360", "A500ETF", 1000, 1.20),
        _pos("159611", "电力ETF", 2000, 1.10),
        _pos("159530", "机器人ETF", 500, 1.00),
    ]
    positions[1]["account"] = "etf_rotation"
    positions[2]["account"] = "quality_pool"
    core, rot_mv, rot = R.split_rotation_positions(positions)
    assert [p["code"] for p in core] == ["563360"]
    assert [p["code"] for p in rot] == ["159611", "159530"]
    assert rot_mv == 2000 * 1.10 + 500 * 1.00


def test_split_rotation_does_not_read_pool_files():
    """归属不再依赖池快照/旧名单：卫星代码不在任何清单里也照样识别。"""
    positions = [_pos("999999", "无清单标的", 100, 2.0)]
    positions[0]["account"] = "etf_rotation"
    core, rot_mv, rot = R.split_rotation_positions(positions)
    assert core == [] and len(rot) == 1 and rot_mv == 200.0

