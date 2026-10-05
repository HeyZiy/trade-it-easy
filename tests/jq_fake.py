# -*- coding: utf-8 -*-
"""聚宽平台的本地桩（pytest 共享设施）。

目的：research/ 下的平台脚本（l2_etfself_v2.py / roe_rotation_jq.py / 后续同族）
不需要上聚宽就能在 pytest 里真跑——伪造 jqdata 全套 API + 合成行情世界，
驱动 信号→下单→撮合→持仓 全链路。聚宽回测成本高且静默 bug 看不出来
（如卖后回买继承旧买入日期），桩测试当场抓。

用法（见同目录 test_platform_* 示例）：
    stub = JQStub(calendar, closes)          # closes: {code: pd.Series(NaN=未上市/停牌)}
    stub.set_date_table(df, table='valuation')   # get_fundamentals(query(valuation...), date=dt)
    stub.set_date_table(df, table='indicator')   # get_fundamentals 按查询的表路由，statDate 一律抛错
    stub.set_extras('is_st', {code: True})   # get_extras
    stub.set_locked_shares(rows)             # get_locked_shares（DataFrame 含 code 列）
    stub.install().load_script(path)         # 注入 sys.modules['jqdata'] 并加载脚本
    stub.initialize()                        # 调脚本 initialize(ctx)
    stub.run_days(dates)                     # 按 9:31→14:55→…时序驱动每个交易日
    stub.logs                                # 捕获的 log.info

近似口径（写测试时须知）：
- 成交价 = 当日 close（脚本在 9:31 下单，平台≈开盘价；桩不区分）；
- day_open = 当日 close，high_limit/low_limit = 前一有效收盘 ±10%；
- money 缺省 = close 有效处 1e8、否则 0（money≤0 视为停牌）；
- order_target_value 语义 = 新开仓买入目标市值（平台脚本只在空槽买入），
  费率单边 fee_buy/fee_sell，无最低佣金，整手 100。
"""

import importlib.util
import sys
import types
from pathlib import Path

import pandas as pd


class _Pos(object):
    def __init__(self):
        self.total_amount = 0
        self.avg_cost = 0.0


class _Portfolio(object):
    def __init__(self, stub, starting_cash=1_000_000.0):
        self._stub = stub
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions = {}

    @property
    def total_value(self):
        return self.cash + sum(
            p.total_amount * self._stub.price(c)
            for c, p in self.positions.items() if p.total_amount > 0)


class _CurrentData(dict):
    """cd[code]：paused / last_price / day_open / high_limit / low_limit，按日缓存失效。"""

    def __init__(self, stub):
        super().__init__()
        self._stub = stub

    def __missing__(self, code):
        px = self._stub.price(code)
        prev = self._stub.prev_close(code)
        self[code] = types.SimpleNamespace(
            paused=self._stub.paused(code),
            last_price=px,
            day_open=px,
            high_limit=(prev * 1.10) if prev else 1e18,
            low_limit=(prev * 0.90) if prev else 0.0,
            is_st=self._stub.extras.get('is_st', {}).get(code, False))
        return self[code]


class _Chainable(object):
    """query()/indicator/valuation 的空对象：属性链与 .filter()/.in_() 都吞掉。

    带 _table 名（属性链向下传播），让 get_fundamentals 能按查询的表路由。
    """

    def __init__(self, table=None):
        self._table = table

    def __getattr__(self, name):
        return _Chainable(self._table)

    def in_(self, xs):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self


class _Query(object):
    """query(*tables) 的返回：记住查了哪些表，供 get_fundamentals 路由。"""

    def __init__(self, tables):
        self.tables = [t for t in tables if t]

    def in_(self, xs):
        return self

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self


class JQStub(object):
    def __init__(self, calendar, closes, money=None, *, starting_cash=1_000_000.0,
                 fee_buy=0.00025, fee_sell=0.00025):
        self.calendar = pd.DatetimeIndex(calendar)
        self.closes = {c: pd.Series(v, index=self.calendar).astype(float)
                       for c, v in closes.items()}
        if money is None:
            money = {c: pd.Series(
                [1e8 if pd.notna(x) else 0.0 for x in s.values], index=self.calendar)
                for c, s in self.closes.items()}
        self.money = {c: pd.Series(v, index=self.calendar).astype(float)
                      for c, v in money.items()}
        self.security_meta = {}          # code -> (display_name, start_date)
        self.date_tables = {}            # 'valuation'/'indicator' -> DataFrame（date= 查询）
        self.extras = {}                 # 'is_st' -> {code: bool}
        self.locked_rows = None          # DataFrame 含 code 列
        self.fee_buy, self.fee_sell = fee_buy, fee_sell
        self.today = None
        self.portfolio = _Portfolio(self, starting_cash)
        self.orders = []                 # (side, date, code, qty, price)
        self.logs = []
        self.scheduled = {}              # time_str -> fn
        self.g = types.SimpleNamespace()
        self.ctx = types.SimpleNamespace(current_dt=None, portfolio=self.portfolio)
        self.current_data = _CurrentData(self)
        self._module = None
        self._script = None

    # ── 行情原语 ────────────────────────────────────────────────
    def price(self, code):
        s = self.closes.get(code)
        if s is None or self.today is None:
            return 0.0
        v = s.asof(self.today)
        return float(v) if pd.notna(v) else 0.0

    def prev_close(self, code):
        s = self.closes.get(code)
        if s is None or self.today is None:
            return None
        valid = s[s.index < self.today].dropna()
        return float(valid.iloc[-1]) if len(valid) else None

    def paused(self, code):
        m = self.money.get(code)
        if m is None or self.today is None:
            return True
        v = m.asof(self.today)
        return bool(pd.isna(v) or v <= 0)

    # ── 世界配置 ────────────────────────────────────────────────
    def set_security_meta(self, meta):
        self.security_meta = dict(meta)

    def set_date_table(self, df, table='valuation'):
        self.date_tables[table] = df

    def set_extras(self, field, bool_map):
        self.extras[field] = dict(bool_map)

    def set_locked_shares(self, rows):
        self.locked_rows = rows

    # ── API 实现（挂到伪造 jqdata 模块上）───────────────────────
    def _history(self, count, unit='1d', field='close', security_list=None, **kw):
        panel = self.closes if field == 'close' else self.money
        cols = {}
        for c in security_list:
            s = panel[c]
            cols[c] = s[s.index <= self.today].tail(count)
        return pd.DataFrame(cols)

    def _attribute_history(self, code, count, unit='1d', fields=('close',),
                           skip_paused=True, **kw):
        s = self.closes[code]
        sub = s[s.index < self.today]
        if skip_paused:
            sub = sub.dropna()
        out = pd.DataFrame({f: sub if f == 'close' else None for f in fields})
        return out.tail(count)

    def _get_all_securities(self, types_, date=None):
        rows = {}
        for c, s in self.closes.items():
            if c in self.security_meta:
                name, start = self.security_meta[c]
            else:
                valid = s.dropna()
                name, start = c, (valid.index[0] if len(valid) else self.calendar[0])
            rows[c] = {'display_name': name, 'start_date': start}
        return pd.DataFrame(rows).T

    def _get_fundamentals(self, q=None, date=None, statDate=None, **kw):
        if statDate is not None:
            raise AssertionError('avoid_future_data=True 的回测禁用 statDate（平台行为）')
        for table in getattr(q, 'tables', []):
            if table not in self.date_tables:
                continue
            # 平台行为：市值表当日快照 15:00 后才可得，盘中（9:31/14:55）只能取 T-1
            if table == 'valuation' and pd.Timestamp(date) >= self.today:
                raise AssertionError('市值表未来数据：date=%s >= 当日 %s'
                                     % (date, self.today.date()))
            return self.date_tables[table]
        raise KeyError('未配置的 date 表: %s' % (getattr(q, 'tables', []),))

    def _get_extras(self, field, security_list=None, end_date=None, count=1, **kw):
        m = self.extras.get(field, {})
        return pd.DataFrame({c: [bool(m.get(c, False))] for c in security_list})

    def _get_locked_shares(self, stock_list=None, start_date=None, end_date=None, **kw):
        rows = self.locked_rows
        if rows is None or not len(rows):
            return pd.DataFrame(columns=['code'])
        m = rows.copy()
        if start_date is not None:
            m = m[m['day'] >= pd.Timestamp(start_date)]
        if end_date is not None:
            m = m[m['day'] <= pd.Timestamp(end_date)]
        if stock_list is not None:
            m = m[m['code'].isin(stock_list)]
        return m

    def _get_trade_days(self, start_date=None, end_date=None, count=None, **kw):
        days = self.calendar
        if start_date is not None:
            days = days[days >= pd.Timestamp(start_date)]
        if end_date is not None:
            days = days[days <= pd.Timestamp(end_date)]
        return list(days)

    def _order_target(self, code, amount):
        pos = self.portfolio.positions.setdefault(code, _Pos())
        px = self.price(code)
        if amount == 0 and pos.total_amount > 0:
            self.portfolio.cash += pos.total_amount * px * (1 - self.fee_sell)
            self.orders.append(('S', self.today.strftime('%F'), code,
                                pos.total_amount, px))
            pos.total_amount = 0
        return None

    def _order_target_value(self, code, value):
        px = self.price(code)
        if px <= 0:
            return None
        pos = self.portfolio.positions.setdefault(code, _Pos())
        delta = value - pos.total_amount * px
        if delta <= 0:
            return None
        qty = int(min(delta, self.portfolio.cash) / (px * (1 + self.fee_buy)) // 100) * 100
        if qty < 100:
            return None
        cost = qty * px * (1 + self.fee_buy)
        self.portfolio.cash -= cost
        pos.total_amount = qty
        self.orders.append(('B', self.today.strftime('%F'), code, qty, px))
        return None

    # ── 装配与驱动 ──────────────────────────────────────────────
    def install(self):
        mod = types.ModuleType('jqdata')
        mod.history = self._history
        mod.attribute_history = self._attribute_history
        mod.get_current_data = lambda: _CurrentData(self)
        mod.get_all_securities = self._get_all_securities
        mod.get_fundamentals = self._get_fundamentals
        mod.query = lambda *a, **k: _Query([t._table for t in a])
        mod.indicator = _Chainable('indicator')
        mod.valuation = _Chainable('valuation')
        mod.get_extras = self._get_extras
        mod.get_locked_shares = self._get_locked_shares
        mod.get_trade_days = self._get_trade_days
        mod.order_target = self._order_target
        mod.order_target_value = self._order_target_value
        mod.OrderCost = lambda **k: types.SimpleNamespace(k)
        mod.set_option = lambda *a, **k: None
        mod.set_benchmark = lambda *a, **k: None
        mod.set_order_cost = lambda *a, **k: None

        class _Log(object):
            def set_level(self, *a):
                pass

            def info(self, msg, _self=self):
                _self.logs.append(str(msg))

        mod.log = _Log()
        mod.g = self.g
        mod.run_daily = lambda fn, time='9:30', _s=self: _s.scheduled.__setitem__(time, fn)
        sys.modules['jqdata'] = mod
        self._module = mod
        return self

    def load_script(self, path):
        spec = importlib.util.spec_from_file_location(
            'jq_script_%s' % Path(path).stem, str(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._script = mod
        return mod

    def initialize(self):
        self._script.initialize(self.ctx)

    def run_days(self, dates):
        """按日驱动：每个交易日按注册时间（9:31 → 14:55 → …）调用全部 run_daily 函数。"""
        for d in dates:
            d = pd.Timestamp(d)
            self.today = d
            self.ctx.current_dt = d
            self.current_data = _CurrentData(self)   # cd 按日重建（缓存失效）
            times = sorted(self.scheduled,
                           key=lambda t: int(t.split(':')[0]) * 60 + int(t.split(':')[1]))
            for t in times:
                self.scheduled[t](self.ctx)
