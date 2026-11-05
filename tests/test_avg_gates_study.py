# -*- coding: utf-8 -*-
"""聚宽平台行为桩：均值闸门对照实验（inst20 vs avg10d，两臂无再平衡）闭环测试。

场景覆盖：两臂日1收敛；节点日瞬时PE尖峰只打掉inst20（均值窗口稀释）；
持续PE越界时avg10d先退出、inst20等到节点日；全程不存在equal_weight再平衡
委托；导出表完整落地。
"""
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum/study_v5_avg_gates_jq.py'
CAL = pd.bdate_range('2025-06-02', periods=300)
RUN_START = 260
CODES = ['60000%d.XSHG' % i for i in range(8)]


def _idx(date):
    return CAL.get_loc(pd.Timestamp(date).normalize())


def _rule_value(rules, code, idx, default):
    for rule in rules:
        start, end = rule[0], (rule[1] if len(rule) == 4 else None)
        target, value = rule[-2], rule[-1]
        if target == code and start <= idx and (end is None or idx <= end):
            return value
    return default


def _price(code, idx):
    return 10.0 * math.exp(0.001 * int(code[5]) * idx)


def world(np_yoy_rules=None, roe_rules=None, pe_rules=None, starting_cash=2000000):
    """两账户平台桩：8只样本股、指数级价格、可注入财务/估值规则。"""
    np_yoy_rules = np_yoy_rules or [(0, c, 15.0) for c in CODES]
    roe_rules = roe_rules or [(0, c, 20.0) for c in CODES]
    pe_rules = pe_rules or []
    state = {'day': 1, 'date': CAL[RUN_START]}
    files, orders, day_orders = {}, {}, {}
    counter = [0]
    positions = {0: {}, 1: {}}
    cash = {0: starting_cash // 2, 1: starting_cash // 2}

    class Position:
        def __init__(self, amount, price):
            self.total_amount = amount
            self.closeable_amount = amount
            self.locked_amount = 0
            self.price = price
            self.avg_cost = price

    class SubPortfolio:
        def __init__(self, index):
            self.index = index
            self.long_positions = positions[index]

        @property
        def available_cash(self):
            return cash[self.index]

        @property
        def total_value(self):
            return cash[self.index] + _portfolio_value(self.index)

    def _portfolio_value(index):
        return sum(pos.total_amount * pos.price for pos in positions[index].values())

    class OrderCost:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PriceRelatedSlippage:
        def __init__(self, value):
            self.value = value

    class SubPortfolioConfig:
        def __init__(self, cash, type):
            self.cash, self.type = cash, type

    class _Field:
        def __init__(self, name):
            self.name = name

        def in_(self, codes):
            self.codes = list(codes)
            return self

    indicator = SimpleNamespace(code=_Field('code'),
                                inc_net_profit_year_on_year=_Field('inc_net_profit_year_on_year'),
                                statDate=_Field('statDate'), pubDate=_Field('pubDate'))
    valuation = SimpleNamespace(code=_Field('code'), pe_ratio=_Field('pe_ratio'),
                                 pb_ratio=_Field('pb_ratio'))

    class _Query:
        def __init__(self, *fields):
            self._fields = [f.name for f in fields]

        def filter(self, field):
            return self

    class _Log:
        @staticmethod
        def info(message):
            print(message)

        @staticmethod
        def set_level(*args):
            pass

    log = _Log()

    def get_all_securities(types=None, date=None):
        return pd.DataFrame({'display_name': [c[:6] for c in CODES]}, index=pd.Index(CODES))

    def get_fundamentals(q, date=None):
        as_of = pd.Timestamp(date).normalize() if date is not None else state['date']
        idx = _idx(as_of)
        if 'inc_net_profit_year_on_year' in q._fields:
            return pd.DataFrame({'code': list(CODES),
                                 'inc_net_profit_year_on_year': [_rule_value(np_yoy_rules, c, idx, 15.0) for c in CODES],
                                 'statDate': ['2025q1'] * len(CODES),
                                 'pubDate': ['2025-05-20'] * len(CODES)})
        return pd.DataFrame({'code': list(CODES),
                             'pe_ratio': [_rule_value(pe_rules, c, idx, 20.0) for c in CODES],
                             'pb_ratio': [2.0] * len(CODES)})

    def get_factor_values(securities, factors, end_date, count=1):
        idx = _idx(end_date)
        frame = pd.DataFrame({c: [_rule_value(roe_rules, c, idx, 20.0)] for c in securities},
                             index=pd.DatetimeIndex([pd.Timestamp(end_date).normalize()]))
        return {'roe_ttm': frame}

    def get_trade_days(end_date=None, count=None):
        end = _idx(end_date)
        return [CAL[i] for i in range(end - count + 1, end + 1)]

    def history(count, unit='1d', field='close', security_list=None, skip_paused=False, df=True):
        end = _idx(state['date']) - 1
        dates = [CAL[i] for i in range(max(0, end - count + 1), end + 1)]
        data = {c: [_price(c, _idx(d)) for d in dates] for c in security_list}
        return pd.DataFrame(data, index=pd.DatetimeIndex(dates))

    def get_industry(codes, date=None):
        return {c: {'sw_l1': {'industry_code': 'SW-' + c}} for c in codes}

    def get_current_data():
        idx = _idx(state['date'])
        data = {}
        for c in CODES:
            px = _price(c, idx)
            data[c] = SimpleNamespace(last_price=px, paused=False, is_st=False,
                                       high_limit=px * 1.1, low_limit=px * 0.9)
        return data

    def order_target(code, quantity, pindex=0):
        pos = positions[pindex].get(code)
        current = pos.total_amount if pos else 0
        if quantity == current:
            return None
        idx = _idx(state['date'])
        px = _price(code, idx)
        if quantity > current:
            cost = (quantity - current) * px
            fee = max(5.0, cost * 0.00025)
            assert cash[pindex] >= cost + fee, 'insufficient cash in sub %d' % pindex
            cash[pindex] -= cost + fee
            if pos:
                pos.total_amount = pos.closeable_amount = quantity
                pos.price = px
            else:
                positions[pindex][code] = Position(quantity, px)
            is_buy = True
        else:
            proceeds = (current - quantity) * px
            fee = max(5.0, proceeds * 0.00025)
            cash[pindex] += proceeds - fee
            if quantity == 0:
                positions[pindex].pop(code, None)
            else:
                pos.total_amount = pos.closeable_amount = quantity
                pos.price = px
            is_buy = False
        oid = 'o%d' % counter[0]
        counter[0] += 1
        order = SimpleNamespace(order_id=oid, security=code, is_buy=is_buy,
                                filled=abs(quantity - current), price=px, status='filled',
                                commission=fee)
        orders[oid] = order
        day_orders[oid] = order
        return order

    def get_price(security, end_date=None, count=1, frequency='daily', fields=None):
        day = pd.Timestamp(end_date).normalize()
        return pd.DataFrame({'close': [3000.0]}, index=pd.DatetimeIndex([day]))

    def get_open_orders():
        return {}

    def get_orders():
        return dict(day_orders)

    def write_file(name, content, append=False):
        files[name] = files.get(name, '') + content if append else content

    api = SimpleNamespace(
        get_all_securities=get_all_securities, get_fundamentals=get_fundamentals,
        get_factor_values=get_factor_values, get_trade_days=get_trade_days,
        history=history, get_industry=get_industry, get_current_data=get_current_data,
        query=_Query, indicator=indicator, valuation=valuation, order_target=order_target,
        get_price=get_price, get_open_orders=get_open_orders, get_orders=get_orders,
        write_file=write_file, record=lambda **kwargs: None, log=log, g=SimpleNamespace(),
        set_option=lambda *a, **k: None, set_benchmark=lambda *a, **k: None,
        set_slippage=lambda *a, **k: None, set_order_cost=lambda *a, **k: None,
        set_subportfolios=lambda *a, **k: None, run_daily=lambda *a, **k: None,
        OrderCost=OrderCost, PriceRelatedSlippage=PriceRelatedSlippage,
        SubPortfolioConfig=SubPortfolioConfig)
    context = SimpleNamespace(
        current_dt=state['date'],
        portfolio=SimpleNamespace(starting_cash=starting_cash),
        subportfolios=[SubPortfolio(0), SubPortfolio(1)],
        run_params=SimpleNamespace(frequency='day', start_date='2016-01-04', end_date='2026-09-01'))
    return SimpleNamespace(api=api, context=context, files=files, state=state,
                           positions=positions, day_orders=day_orders)


def _load(api):
    for name in ('jqdata', 'jqfactor'):
        sys.modules.pop(name, None)
    jqdata = ModuleType('jqdata')
    for key, value in vars(api).items():
        setattr(jqdata, key, value)
    jqfactor = ModuleType('jqfactor')
    jqfactor.get_factor_values = api.get_factor_values
    sys.modules['jqdata'] = jqdata
    sys.modules['jqfactor'] = jqfactor
    spec = importlib.util.spec_from_file_location('study_v5_avg_gates_jq_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup(**kwargs):
    w = world(**kwargs)
    strategy = _load(w.api)
    strategy.initialize(w.context)
    return w, strategy


def _run(w, strategy, days):
    for day in days:
        w.state['day'] = day
        w.state['date'] = CAL[RUN_START + day - 1]
        w.context.current_dt = w.state['date']
        w.day_orders.clear()
        strategy.on_signal(w.context)
        strategy.on_open(w.context)
        strategy.after_trading_end(w.context)


def _held(w, index):
    return set(w.positions[index].keys())


def test_day1_convergence():
    w, strategy = _setup()
    _run(w, strategy, [1])
    assert _held(w, 0) == set(CODES[2:8])
    assert _held(w, 1) == set(CODES[2:8])


def test_transient_pe_spike_hits_inst20_only():
    spike = RUN_START + 19  # day21的asof：节点日单日PE=35
    w, strategy = _setup(pe_rules=[(spike, spike, CODES[3], 35.0)])
    _run(w, strategy, list(range(1, 26)))
    # inst20在节点日撞上瞬时越界→退出600003并补位600001
    assert _held(w, 0) == set(CODES[1:8]) - {CODES[3]}
    # avg10d窗口均值21.5<30，单日尖峰被稀释，不退出
    assert _held(w, 1) == set(CODES[2:8])


def test_persistent_pe_breach_avg_exits_before_node():
    start = RUN_START + 3  # day5的asof起持续PE=35
    w, strategy = _setup(pe_rules=[(start, CODES[3], 35.0)])
    _run(w, strategy, list(range(1, 11)))
    assert _held(w, 0) == set(CODES[2:8])
    assert _held(w, 1) == set(CODES[2:8])
    _run(w, strategy, [11])  # 窗口均值(3*20+7*35)/10=30.5≥30
    assert _held(w, 1) == set(CODES[1:8]) - {CODES[3]}
    assert _held(w, 0) == set(CODES[2:8])  # inst20要等到下一个节点日
    _run(w, strategy, list(range(12, 21)))
    assert _held(w, 0) == set(CODES[2:8])
    _run(w, strategy, [21])  # inst20节点日撞上瞬时越界
    assert _held(w, 0) == set(CODES[1:8]) - {CODES[3]}
    assert _held(w, 1) == set(CODES[1:8]) - {CODES[3]}


def test_no_rebalance_reasons_and_exports():
    start = RUN_START + 3
    w, strategy = _setup(pe_rules=[(start, CODES[3], 35.0)])
    _run(w, strategy, list(range(1, 26)))
    reasons = {r['reason'] for account in strategy.g.accounts for r in account.order_rows}
    assert reasons <= {'entry', 'out_of_pool'}
    assert 'entry' in reasons and 'out_of_pool' in reasons
    strategy.on_strategy_end(w.context)
    prefix = 'quality_momentum_v5_avg_gates__'  # OUTPUT_PREFIX尾下划线+'%s_%s'拼接=双下划线
    for name in ('settings', 'summary', 'annual', 'comparison', 'exit_stats', 'nav',
                 'signals', 'orders', 'decisions', 'holding_periods', 'events',
                 'attribution', 'members', 'positions', 'reviews'):
        assert (prefix + name + '.csv') in w.files, name
