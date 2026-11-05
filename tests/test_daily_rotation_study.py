"""用双账户假平台验证调仓周期对照实验的关键调度语义。

覆盖三点：daily1周中退出次日补位而base20持到节点；base20非节点日只补判数据
未知旧仓、确认失败进入待退出；受阻退出在数据恢复后的取消路径——daily1次日
重算即取消，base20须等节点日重算（与v5现行语义一致）。引擎记账细节已由
rank-control实验平台结果核对，不在此重复。
"""
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum/study_v5_daily_rotation_jq.py'

CODES = ['60000%d.XSHG' % i for i in range(8)]
BENCH = '000906.XSHG'
CAL = pd.bdate_range('2025-06-02', periods=300)
RUN_START = 260          # 首个回测交易日下标（保证253根收盘历史）
NP_YOY_CODE = '600007.XSHG'
ROE_CODE = '600005.XSHG'


def price(code, idx):
    # 各代码恒定正斜率：动量排序=代码序号倒序，波动率≈0。
    if code not in CODES:
        return 3000.0   # 基准指数点位
    return 10.0 * math.exp(0.001 * CODES.index(code) * idx)


def _rule_value(rules, code, idx, default):
    for start, c, value in rules:
        if c == code and idx >= start:
            return value
    return default


class Sub:
    def __init__(self, cash):
        self.long_positions = {}
        self.available_cash = cash

    @property
    def total_value(self):
        return self.available_cash + sum(p.total_amount * p.price
                                         for p in self.long_positions.values())


class Position(SimpleNamespace):
    pass


def world(monkeypatch, np_yoy_rules=(), roe_rules=(), paused_days=None):
    """规则按(起始日历下标, code, 值)先匹配先用；roe值None表示因子缺失(unknown)。
    np_yoy单位为百分比(20=20%)，roe为ratio(0.15=15%，脚本内×100)。"""
    api = ModuleType('jqdata')
    api.g = SimpleNamespace()
    api.query = lambda *cols: SimpleNamespace(
        columns=cols, filter=lambda *a, **k: SimpleNamespace(columns=cols))
    api.indicator = SimpleNamespace(
        code=SimpleNamespace(in_=lambda lst: ('code', 'in', list(lst))),
        inc_net_profit_year_on_year=object(), statDate=object(), pubDate=object())
    api.valuation = SimpleNamespace(code=api.indicator.code, pe_ratio=object(), pb_ratio=object())
    api.SubPortfolioConfig = lambda cash, type: SimpleNamespace(cash=cash, type=type)
    api.OrderCost = lambda **k: k
    api.PriceRelatedSlippage = lambda x: x
    paused_days = paused_days or {}

    def get_fundamentals(q, date=None):
        if any(c is api.indicator.inc_net_profit_year_on_year for c in q.columns):
            idx = CAL.get_loc(pd.Timestamp(date).normalize())
            return pd.DataFrame({'code': list(CODES),
                                 'inc_net_profit_year_on_year': [
                                     _rule_value(np_yoy_rules, c, idx, 20.0) for c in CODES],
                                 'statDate': ['2025Q1'] * 8, 'pubDate': ['2025-05-20'] * 8})
        return pd.DataFrame({'code': list(CODES), 'pe_ratio': [20.0] * 8, 'pb_ratio': [2.0] * 8})

    def get_factor_values(securities, factors, end_date=None, count=None):
        idx = CAL.get_loc(pd.Timestamp(end_date).normalize())
        columns = {}
        for c in securities:
            value = _rule_value(roe_rules, c, idx, 0.15)
            if value is not None:
                columns[c] = [value]
        return {'roe_ttm': pd.DataFrame(columns, index=[pd.Timestamp(end_date).normalize()])}

    def get_trade_days(end_date=None, count=None):
        end = CAL.get_loc(pd.Timestamp(end_date).normalize())
        return list(CAL[max(0, end - count + 1):end + 1])

    state = {'day': None}   # 当前交易日历下标，由驱动循环设置

    def history(count, unit, field, security_list=None, skip_paused=False, df=True):
        end = state['day'] - 1
        positions = list(range(max(0, end - count + 1), end + 1))
        return pd.DataFrame({c: [price(c, i) for i in positions] for c in security_list},
                            index=[CAL[i] for i in positions])

    api.get_fundamentals = get_fundamentals
    api.get_factor_values = get_factor_values
    api.get_trade_days = get_trade_days
    api.history = history
    api.get_all_securities = lambda types, date=None: pd.DataFrame(
        {'display_name': list(CODES)}, index=list(CODES))
    api.get_industry = lambda codes, date=None: {
        c: {'sw_l1': {'industry_code': 'sw' + c}} for c in codes}
    api.get_price = lambda sec, end_date=None, count=None, frequency=None, fields=None: \
        pd.DataFrame({'close': [3000.0]}, index=[pd.Timestamp(end_date).normalize()])

    files, orders, day_orders, oid = {}, {}, {}, [0]

    def order_target(code, qty, pindex=0):
        sub = context.subportfolios[pindex]
        pos = sub.long_positions.get(code)
        current = pos.total_amount if pos is not None else 0
        diff = qty - current
        if diff == 0:
            return None
        px = price(code, state['day'])
        value = abs(diff) * px
        fee = value * 0.00025
        if diff > 0:
            assert sub.available_cash >= value * 1.001 + fee, 'cash overrun for ' + code
            sub.available_cash -= value * 1.001 + fee
            if pos is None:
                pos = Position(total_amount=0, closeable_amount=0, locked_amount=0,
                               price=px, avg_cost=px)
                sub.long_positions[code] = pos
            pos.total_amount += diff
        else:
            sub.available_cash += value * 0.999 - fee
            pos.total_amount += diff
            if pos.total_amount == 0:
                del sub.long_positions[code]
            else:
                pos.closeable_amount = pos.total_amount
        if pos.total_amount > 0:
            pos.closeable_amount = pos.total_amount
        pos.price = px
        oid[0] += 1
        row = SimpleNamespace(order_id=oid[0], security=code, filled=abs(diff), price=px,
                              is_buy=diff > 0, commission=fee, status='filled')
        orders[str(oid[0])] = row
        day_orders[str(oid[0])] = row
        return row

    def get_current_data():
        paused = paused_days.get(state['day'], set())
        return {c: SimpleNamespace(last_price=price(c, state['day']), paused=c in paused,
                                   is_st=False, high_limit=price(c, state['day']) * 1.1,
                                   low_limit=price(c, state['day']) * 0.9)
                for c in CODES + [BENCH]}

    monkeypatch.setitem(sys.modules, 'jqdata', api)
    jqfactor = ModuleType('jqfactor')
    jqfactor.get_factor_values = get_factor_values
    monkeypatch.setitem(sys.modules, 'jqfactor', jqfactor)
    spec = importlib.util.spec_from_file_location('daily_rotation_under_test', SCRIPT)
    strategy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strategy)
    strategy.set_option = strategy.set_benchmark = strategy.set_slippage = lambda *a, **k: None
    strategy.set_order_cost = strategy.run_daily = strategy.set_subportfolios = lambda *a, **k: None
    strategy.log = SimpleNamespace(info=lambda *a: None, set_level=lambda *a: None)
    strategy.record = lambda **k: None
    strategy.write_file = lambda name, text, append=False: files.__setitem__(
        name, (files.get(name, '') if append else '') + text)
    strategy.order_target = order_target
    strategy.get_current_data = get_current_data
    strategy.get_open_orders = lambda: {}
    strategy.get_orders = lambda: day_orders

    context = SimpleNamespace(
        portfolio=SimpleNamespace(starting_cash=2000000.0),
        subportfolios=[Sub(1000000.0), Sub(1000000.0)],
        current_dt=pd.Timestamp('2025-06-02'),
        run_params=SimpleNamespace(frequency='day', start_date='', end_date=''))
    strategy.initialize(context)
    return SimpleNamespace(strategy=strategy, context=context, files=files, orders=orders,
                           day_orders=day_orders, state=state, accounts=strategy.g.accounts)


def run_day(world, local_day):
    idx = RUN_START + local_day - 1
    world.state['day'] = idx
    world.context.current_dt = CAL[idx]
    world.day_orders.clear()          # 平台get_orders只含当日订单
    for sub in world.context.subportfolios:
        for code, pos in sub.long_positions.items():
            pos.price = price(code, idx)   # 逐日盯市，预算随权益走
    world.strategy.on_signal(world.context)
    world.strategy.on_open(world.context)
    world.strategy.after_trading_end(world.context)


def held(world, arm):
    sub = world.context.subportfolios[0 if arm == 'base20' else 1]
    return {c for c, p in sub.long_positions.items() if p.total_amount > 0}


def test_daily_refills_midcycle_base20_waits_for_node(monkeypatch):
    # 头号持仓自day5的T-1起单季净利同比5%：daily1当日卖出并补入600001，base20持到day21节点。
    w = world(monkeypatch, np_yoy_rules=[(RUN_START + 3, NP_YOY_CODE, 5.0)])
    for day in range(1, 6):
        run_day(w, day)
    assert held(w, 'daily1') == set(CODES[1:7])
    assert held(w, 'base20') == set(CODES[2:8])
    for day in range(6, 22):
        run_day(w, day)
    assert held(w, 'daily1') == set(CODES[1:7])
    assert held(w, 'base20') == set(CODES[1:7])   # day21节点两臂收敛


def test_transient_failure_invisible_to_base20_caught_by_daily1(monkeypatch):
    # 600005的ROE因子：T-1在day5-6缺失(unknown)、day7-10为5%(failed)、day11起恢复15%。
    # daily1在day7确认失败卖出并按新前6名补入600001；base20的补判只覆盖节点日已
    # unknown的仓，该瞬时失败对其不可见，day21节点asof已恢复→全程不退出。
    roe_rules = [(RUN_START + 9, ROE_CODE, 0.15),
                 (RUN_START + 5, ROE_CODE, 0.05),
                 (RUN_START + 3, ROE_CODE, None)]
    w = world(monkeypatch, roe_rules=roe_rules)
    for day in range(1, 5):
        run_day(w, day)
    assert held(w, 'base20') == held(w, 'daily1') == set(CODES[2:8])
    run_day(w, 5)
    # unknown日：daily1冻结该仓不卖（deferred），两臂持仓不变。
    assert held(w, 'daily1') == set(CODES[2:8])
    assert ROE_CODE not in w.accounts[1].exit_pending
    run_day(w, 7)
    # daily1：确认失败→当日卖出并按新前6名（600005已出池）补入600001。
    assert held(w, 'daily1') == set(CODES[1:5]) | {CODES[6], CODES[7]}
    assert held(w, 'base20') == set(CODES[2:8])
    for day in range(8, 22):
        run_day(w, day)
    # base20直到day21节点也未见该失败（asof已恢复），两臂不再收敛。
    assert held(w, 'base20') == set(CODES[2:8])
    assert held(w, 'daily1') == set(CODES[1:5]) | {CODES[6], CODES[7]}


def test_confirmed_exit_while_paused_base20_sells_daily1_recovers(monkeypatch):
    # 600005在day21节点确认ROE失败（两臂设待退出），day21-30停牌挡卖；
    # asof在day23-24转unknown、day25起恢复15%。daily1次日重算恢复通过即取消退出；
    # base20的确认退出不回头，day31首个未停牌开盘卖出，且无计划不回补。
    roe_rules = [(RUN_START + 23, ROE_CODE, 0.15),
                 (RUN_START + 21, ROE_CODE, None),
                 (RUN_START + 19, ROE_CODE, 0.05)]
    paused = {idx: {ROE_CODE} for idx in range(RUN_START + 20, RUN_START + 30)}
    w = world(monkeypatch, roe_rules=roe_rules, paused_days=paused)
    for day in range(1, 21):
        run_day(w, day)
    base, daily = w.accounts[0], w.accounts[1]
    assert held(w, 'base20') == held(w, 'daily1') == set(CODES[2:8])
    run_day(w, 21)
    # 节点日两臂确认失败并设待退出；停牌挡卖，买入也因名额被占受阻。
    assert base.exit_pending.get(ROE_CODE) == 'out_of_pool'
    assert daily.exit_pending.get(ROE_CODE) == 'out_of_pool'
    assert ROE_CODE in held(w, 'base20') and ROE_CODE in held(w, 'daily1')
    for day in range(22, 25):
        run_day(w, day)
    assert base.exit_pending.get(ROE_CODE) == 'out_of_pool'
    assert daily.exit_pending.get(ROE_CODE) == 'out_of_pool'   # unknown保留退出意图
    run_day(w, 25)
    # daily1重算确认通过→退出取消、继续持有；base20维持退出意图。
    assert ROE_CODE not in daily.exit_pending
    assert base.exit_pending.get(ROE_CODE) == 'out_of_pool'
    for day in range(26, 32):
        run_day(w, day)
    # base20在day31首个未停牌开盘卖出，无计划不回补，仅5只持仓；daily1持有6只。
    assert ROE_CODE not in held(w, 'base20')
    assert held(w, 'base20') == set(CODES[2:8]) - {ROE_CODE}
    assert held(w, 'daily1') == set(CODES[2:8])
    assert ROE_CODE in held(w, 'daily1')
