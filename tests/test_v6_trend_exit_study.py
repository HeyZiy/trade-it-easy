"""用假平台验证均线账户的入场、确认退出、成交与补位。"""
import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum/study_v6_trend_exit_jq.py'

CODES = ['60000%d.XSHG' % i for i in range(8)]
BREAKER = CODES[7]
ST_CODE = CODES[6]     # ST场景中被标记的风险警示股
BENCH = '000906.XSHG'
CAL = pd.bdate_range('2025-06-02', periods=420)
RUN_START = 290        # 首个回测交易日下标（保证253根收盘历史）
PREFIX = 'quality_momentum_v6_ma_exit_'
MA20_PREFIX = 'quality_momentum_v6_ma20_entry_exit_'
FILES = ['settings', 'summary', 'annual', 'exit_stats', 'nav', 'signals', 'orders',
         'decisions', 'holding_periods', 'events', 'attribution', 'members', 'positions']


def price(code, idx, decline_start=RUN_START):
    if code == BENCH:
        return 3000.0
    if code == BREAKER:
        if idx < 100:
            return 10.0
        if idx < 260:
            return 10.0 * 2.0 ** ((idx - 100) / 160.0)
        if idx < decline_start:
            return 20.0 * 1.005 ** (idx - 260)
        return 20.0 * 1.005 ** (decline_start - 260) * 0.99 ** (idx - decline_start)
    return 10.0 * math.exp(0.001 * (CODES.index(code) + 1) * idx)


def _rule_value(rules, code, idx, default):
    for start, c, value in rules:
        if c == code and idx >= start:
            return value
    return default


class Position(SimpleNamespace):
    pass


class Sub:
    def __init__(self, cash):
        self.long_positions = {}
        self.available_cash = cash

    @property
    def total_value(self):
        return self.available_cash + sum(p.total_amount * p.price
                                         for p in self.long_positions.values())


def world(np_yoy_rules=(), roe_rules=(), st_rules=(), paused_days=None, quote_price=price,
          opening_price=None):
    """规则按(起始日历下标, code, 值)先匹配先用；roe值None表示因子缺失。
    np_yoy单位为百分比(20=20%)，roe为ratio(0.15=15%，脚本内×100)。
    paused_days: {code: {日历下标}}。"""
    api = ModuleType('jqdata')
    if opening_price is None:
        opening_price = quote_price
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
    paused_map = {c: set(days) for c, days in (paused_days or {}).items()}
    logs = []

    class Log:
        @staticmethod
        def info(message):
            logs.append(str(message))

        @staticmethod
        def set_level(*args):
            pass

    api.log = Log

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
        return pd.DataFrame({c: [quote_price(c, i) for i in positions] for c in security_list},
                            index=[CAL[i] for i in positions])

    api.get_fundamentals = get_fundamentals
    api.get_factor_values = get_factor_values
    api.get_trade_days = get_trade_days
    api.history = history
    api.get_all_securities = lambda types, date=None: pd.DataFrame(
        {'display_name': list(CODES)}, index=list(CODES))
    api.get_industry = lambda codes, date=None: {
        c: {'sw_l1': {'industry_code': 'sw' + c}} for c in codes}
    benchmark_requests = []

    def get_price(sec, end_date=None, count=None, frequency=None, fields=None):
        benchmark_requests.append(count)
        days = get_trade_days(end_date=end_date, count=count)
        return pd.DataFrame({'close': 3000.0}, index=days)

    api.get_price = get_price

    def get_current_data():
        idx = state['day']
        data = {}
        for c in CODES:
            px = opening_price(c, idx)
            data[c] = SimpleNamespace(paused=idx in paused_map.get(c, set()),
                                       is_st=bool(_rule_value(st_rules, c, idx, False)),
                                       high_limit=px * 1.1, low_limit=px * 0.9,
                                       last_price=px)
        return data

    api.get_current_data = get_current_data

    files, orders, day_orders, oid = {}, {}, {}, [0]
    context = SimpleNamespace(current_dt=CAL[RUN_START],
                              portfolio=SimpleNamespace(starting_cash=2000000.0),
                              subportfolios=[Sub(1000000.0), Sub(1000000.0)],
                              run_params=SimpleNamespace(frequency='day',
                                                         start_date=str(CAL[RUN_START].date()),
                                                         end_date=str(CAL[-1].date())))

    def order_target(code, qty, pindex=0):
        sub = context.subportfolios[pindex]
        pos = sub.long_positions.get(code)
        current = pos.total_amount if pos is not None else 0
        delta = qty - current
        if delta == 0:
            return None
        px = opening_price(code, state['day'])
        oid[0] += 1
        order = SimpleNamespace(order_id=oid[0], security=code, is_buy=delta > 0,
                                filled=abs(delta), price=px, status='filled')
        day_orders[oid[0]] = order
        orders[oid[0]] = order
        if delta > 0:
            sub.available_cash -= delta * px
            if pos is None:
                pos = Position(total_amount=0, closeable_amount=0, locked_amount=0, price=px)
                sub.long_positions[code] = pos
            pos.total_amount += delta
            pos.closeable_amount += delta
        else:
            sub.available_cash += (-delta) * px
            pos.total_amount += delta
            pos.closeable_amount = max(0, pos.closeable_amount + delta)
            if pos.total_amount <= 0:
                del sub.long_positions[code]
        pos.price = px
        return order

    api.order_target = order_target
    api.get_orders = lambda: dict(day_orders)
    api.get_open_orders = lambda: {}
    api.write_file = lambda name, content, append=False: files.update(
        {name: files.get(name, '') + content})
    api.record = lambda **curves: None
    run_daily_handlers = []
    api.run_daily = lambda func, time='before_open': run_daily_handlers.append((func, time))
    api.set_option = lambda *a, **k: None
    api.set_benchmark = lambda bench: None
    api.set_slippage = lambda x: None

    def set_order_cost(cost, type=None):
        pass

    api.set_order_cost = set_order_cost

    def set_subportfolios(configs):
        context.subportfolios = [Sub(c.cash) for c in configs]

    api.set_subportfolios = set_subportfolios
    return SimpleNamespace(api=api, context=context, files=files, orders=orders,
                           day_orders=day_orders, state=state, logs=logs,
                           handlers=run_daily_handlers, price=quote_price,
                           benchmark_requests=benchmark_requests)


def _load(api, exit_arms=('ma120', 'blend')):
    for name in ('jqdata', 'jqfactor'):
        sys.modules.pop(name, None)
    jqfactor = ModuleType('jqfactor')
    jqfactor.get_factor_values = api.get_factor_values
    sys.modules['jqdata'] = api
    sys.modules['jqfactor'] = jqfactor
    spec = importlib.util.spec_from_file_location('strategy_v6_trend_exit', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if exit_arms is not None:
        mod.EXIT_ARMS = exit_arms
    return mod


def _run(mod, env, days):
    handlers = env.handlers
    before_open = [f for f, t in handlers if t == 'before_open'][0]
    open_ = [f for f, t in handlers if t == 'open'][0]
    for d in range(1, days + 1):
        idx = RUN_START + d - 1
        env.context.current_dt = CAL[idx]
        env.state['day'] = idx
        before_open(env.context)
        open_(env.context)
        for sub in env.context.subportfolios:
            for code, position in sub.long_positions.items():
                position.price = env.price(code, idx)
        # after_trading_end/on_strategy_end是聚宽按函数名自动调度的框架函数，
        # 不经run_daily注册（重复注册会在平台被调用两次），驱动时直接按名调用。
        mod.after_trading_end(env.context)
        env.day_orders.clear()
    mod.on_strategy_end(env.context)


def _holdings(env, arm=0):
    return set(env.context.subportfolios[arm].long_positions)


def _row_dicts(env, name, prefix=PREFIX):
    return pd.read_csv(pd.io.common.StringIO(env.files[prefix + name + '.csv']))


def test_default_ma20_run_rejects_entry_when_yesterday_closed_below_ma20():
    def under_ma20_only(code, idx):
        if code == BREAKER and idx >= RUN_START - 3:
            return 21.5 - (idx - (RUN_START - 3)) * 0.1
        return price(code, idx)

    env = world(quote_price=under_ma20_only)
    env.context.portfolio.starting_cash = 1000000.0
    mod = _load(env.api, exit_arms=None)
    mod.initialize(env.context)
    _run(mod, env, 1)

    assert {f for f in env.files} == {MA20_PREFIX + name + '.csv' for name in FILES}
    summary = _row_dicts(env, 'summary', MA20_PREFIX)
    assert list(summary['account']) == ['ma20']
    assert list(summary['initial_equity']) == [1000000.0]
    assert _holdings(env) == set(CODES[1:7])
    members = _row_dicts(env, 'members', MA20_PREFIX)
    assert members.loc[members['code'] == BREAKER, 'ranking_status'].item() == 'entry_filter_blocked'


def test_default_ma20_run_rejects_gap_below_line_then_retries_next_day():
    def gap_below_ma20(code, idx):
        if code == BREAKER and idx == RUN_START:
            return 20.0
        return price(code, idx)

    env = world(opening_price=gap_below_ma20)
    env.context.portfolio.starting_cash = 1000000.0
    mod = _load(env.api, exit_arms=None)
    mod.initialize(env.context)
    _run(mod, env, 2)

    events = _row_dicts(env, 'events', MA20_PREFIX)
    blocked = events[(events['kind'] == 'BUY_BLOCKED') & (events['code'] == BREAKER)]
    assert blocked['detail'].item() == 'open_at_or_below_ma20'
    orders = _row_dicts(env, 'orders', MA20_PREFIX)
    breaker_orders = orders[orders['code'] == BREAKER]
    assert list(breaker_orders['date']) == [str(CAL[RUN_START + 1].date())]
    assert list(breaker_orders['reason']) == ['entry']


def test_initial_selection_and_budget():
    env = world()
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 3)
    for index, sub in enumerate(env.context.subportfolios):
        assert _holdings(env, index) == set(CODES[2:8])
        values = [p.total_amount * p.price for p in sub.long_positions.values()]
        assert all(140000 < value < 180000 for value in values)
    nav = _row_dicts(env, 'nav')
    assert nav.groupby('account').size().to_dict() == {'blend': 3, 'ma120': 3}
    assert nav['benchmark_close'].eq(3000.0).all()
    assert env.benchmark_requests == [3]
    assert set(_row_dicts(env, 'summary')['account']) == {'ma120', 'blend'}


def test_selection_does_not_request_history_for_unheld_valuation_or_st_failures():
    env = world(st_rules=((RUN_START, CODES[0], True),))
    get_fundamentals = env.api.get_fundamentals
    history = env.api.history
    requests = []

    def fundamentals_with_high_pe(q, date=None):
        frame = get_fundamentals(q, date)
        if 'pe_ratio' in frame.columns:
            frame.loc[frame['code'] == CODES[1], 'pe_ratio'] = 30.0
        return frame

    def capture_history(count, unit, field, security_list=None, **kwargs):
        requests.append(list(security_list))
        return history(count, unit, field, security_list=security_list, **kwargs)

    env.api.get_fundamentals = fundamentals_with_high_pe
    env.api.history = capture_history
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 1)
    assert requests == [CODES[2:]]
    assert _holdings(env) == _holdings(env, 1) == set(CODES[2:])


def test_both_accounts_exit_on_third_break_then_refill_independently():
    env = world(np_yoy_rules=((300, BREAKER, 5.0),))
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 27)
    orders = _row_dicts(env, 'orders')
    decisions = _row_dicts(env, 'decisions')
    positions = _row_dicts(env, 'positions')
    for arm, exit_day in [('ma120', 314), ('blend', 305)]:
        arm_orders = orders[orders['account'] == arm]
        breaker_orders = arm_orders[arm_orders['code'] == BREAKER]
        assert list(breaker_orders['date']) == [
            str(CAL[RUN_START].date()), str(CAL[exit_day].date())]
        assert list(breaker_orders['reason']) == ['entry', 'trend_break']
        buy = arm_orders[(arm_orders['code'] == CODES[1]) & arm_orders['is_buy']]
        assert list(buy['date']) == [str(CAL[exit_day + 1].date())]
        row = decisions[(decisions['account'] == arm) & (decisions['action'] == 'exit')].iloc[0]
        assert row['asof'] == str(CAL[exit_day - 1].date())
        assert row['pool_state'] == 'failed'
        assert row['pool_reason'] == 'profit_growth_low'
        assert row['close'] < row['trend_line'] and row['trend_gap_pct'] < 0
    between_exits = positions[(positions['date'] == str(CAL[308].date()))
                              & (positions['code'] == BREAKER)]
    assert list(between_exits['account']) == ['ma120']
    assert {f for f in env.files} == {PREFIX + name + '.csv' for name in FILES}


def test_shared_entry_filter_blocks_name_under_both_lines_despite_positive_year():
    env = world(quote_price=lambda code, idx: price(code, idx, decline_start=260))
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 2)
    assert env.price(BREAKER, RUN_START - 1) > env.price(BREAKER, RUN_START - 253)
    assert _holdings(env) == _holdings(env, 1) == set(CODES[1:7])
    members = _row_dicts(env, 'members')
    blocked = members[members['code'] == BREAKER]
    assert set(blocked['account']) == {'ma120', 'blend'}
    assert set(blocked['ranking_status']) == {'entry_filter_blocked'}


def test_st_exits_even_when_trend_history_is_missing():
    env = world(st_rules=((298, ST_CODE, True),))
    history = env.api.history
    env.api.history = lambda *a, **kw: (pd.DataFrame() if env.state['day'] == 298
                                         else history(*a, **kw))
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 10)
    assert _holdings(env) == _holdings(env, 1) == set(CODES[1:6] + [BREAKER])
    orders = _row_dicts(env, 'orders')
    st_exit = orders[(orders['code'] == ST_CODE) & (~orders['is_buy'])].iloc[0]
    assert st_exit['reason'] == 'st'
    assert st_exit['date'] == str(CAL[298].date())
    holdings_csv = _row_dicts(env, 'holding_periods')
    st_hold = holdings_csv[holdings_csv['code'] == ST_CODE].iloc[0]
    assert st_hold['trigger'] == 'st' and st_hold['exit_reason'] == 'st'
    decisions = _row_dicts(env, 'decisions')
    st_decision = decisions[(decisions['code'] == ST_CODE)
                            & (decisions['action'] == 'exit')].iloc[0]
    assert st_decision['pool_reason'] == 'st'


def test_paused_exit_retries_then_refills_next_day():
    env = world(paused_days={BREAKER: {305}})
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 18)
    events = _row_dicts(env, 'events')
    blocked = events[events['kind'] == 'SELL_BLOCKED']
    assert len(blocked) == 1 and blocked.iloc[0]['code'] == BREAKER
    filled = events[events['kind'] == 'EXIT_FILLED']
    assert len(filled) == 1
    assert filled.iloc[0]['time'].startswith(str(CAL[306].date()))
    orders = _row_dicts(env, 'orders')
    breaker_sell = orders[(orders['code'] == BREAKER) & (~orders['is_buy'])].iloc[0]
    assert breaker_sell['account'] == 'blend'
    assert breaker_sell['date'] == str(CAL[306].date())
    assert BREAKER in _holdings(env) and BREAKER not in _holdings(env, 1)
    buy = orders[(orders['code'] == CODES[1]) & orders['is_buy']].iloc[0]
    assert buy['date'] == str(CAL[307].date())


def test_failed_optional_annotation_does_not_delay_exit():
    env = world()
    mod = _load(env.api)
    mod.initialize(env.context)

    def fail_annotation(*args):
        raise mod.PoolDataError('Missing financial snapshot')

    mod._exit_pool_annotation = fail_annotation
    _run(mod, env, 17)
    orders = _row_dicts(env, 'orders')
    sells = orders[~orders['is_buy']]
    assert list(sells['account']) == ['blend']
    assert list(sells['date']) == [str(CAL[305].date())]
    decisions = _row_dicts(env, 'decisions')
    exit_rows = decisions[decisions['action'] == 'exit']
    assert exit_rows.iloc[0]['data_error'] == 'PoolDataError: Missing financial snapshot'


def test_recovery_cancels_unfilled_exit():
    def recover_at_close(code, idx):
        return price(code, idx) * (1.10 if code == BREAKER and idx == 305 else 1)

    env = world(paused_days={BREAKER: {305}}, quote_price=recover_at_close)
    mod = _load(env.api)
    mod.initialize(env.context)
    _run(mod, env, 17)
    events = _row_dicts(env, 'events')
    cancelled = events[events['kind'] == 'EXIT_CANCELLED']
    assert list(cancelled['account']) == ['blend']
    assert cancelled.iloc[0]['time'].startswith(str(CAL[306].date()))
    assert BREAKER in _holdings(env, 1)
