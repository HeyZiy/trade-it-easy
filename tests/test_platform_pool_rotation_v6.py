"""验证聚宽 v6 的日检、实际空位和新仓波动率门槛。"""
import sys
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from jq_fake import RotationStub


BASE = Path(__file__).resolve().parents[1] / 'research/studies/quality_momentum'


@pytest.fixture
def world(monkeypatch):
    # 安装桩会替换平台模块；使用monkeypatch恢复其他测试原有的模块。
    monkeypatch.setitem(sys.modules, 'jqdata', ModuleType('jqdata'))
    factors = ModuleType('jqfactor')
    monkeypatch.setitem(sys.modules, 'jqfactor', factors)
    cal = pd.bdate_range('2024-01-02', periods=360)

    def create(script):
        codes = ['600%03d.XSHG' % i for i in range(8)]
        closes = {c: pd.Series(10.0, index=cal) for c in codes}
        stub = RotationStub(cal, closes).install()
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


def start(world, script='pool_rotation.py'):
    create, calendar = world
    stub, strategy, codes = create(script)
    for code in codes:
        stub.quote_overrides[code] = {'low_limit': .01}
    strategy.after_trading_end = lambda context: None  # 交易桩每天调用这个可选回调。
    return stub, strategy, codes, calendar


def test_volatility_filters_new_entries_and_existing_positions_drift(world):
    stub, strategy, codes, calendar = start(world)
    # 第一名在建仓前已有高波动；第二名在建仓后才出现同样的波动。
    stub.closes[codes[0]].loc[calendar[269]:] *= .65
    stub.closes[codes[1]].loc[calendar[270]:] *= .65
    # 第二名的首日信号用第 269 日收盘，新的波动到下一轮才进入日线窗口。
    stub.run_days(calendar[270:292])

    assert strategy._held(stub.ctx) == set(codes[1:7])
    assert [row[0] for row in stub.orders] == ['B'] * 6
    assert not stub.exports and not stub.records


def test_financial_exit_refills_only_after_full_sale(world):
    stub, strategy, codes, calendar = start(world)
    code = codes[0]
    stub.run_days(calendar[270:271])
    assert strategy._held(stub.ctx) == set(codes[:6])
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.
    stub.sell_caps[code] = 100

    stub.run_days(calendar[271:272])
    assert strategy._held(stub.ctx) == set(codes[:6])
    assert stub.orders[-1][:3] == ('S', str(calendar[271].date()), code)
    assert code in stub.g.exit_pending

    stub.sell_caps.clear()
    stub.run_days(calendar[272:273])
    assert strategy._held(stub.ctx) == set(codes[1:7])
    assert [row[:3] for row in stub.orders[-2:]] == [
        ('S', str(calendar[272].date()), code),
        ('B', str(calendar[272].date()), codes[6]),
    ]


def test_confirmed_exit_survives_missing_entry_history(world):
    stub, strategy, codes, calendar = start(world)
    stub.run_days(calendar[270:271])
    stub.date_tables['indicator'].loc[0, 'inc_net_profit_year_on_year'] = 5.
    history = strategy.history
    strategy.history = lambda *args, **kwargs: history(*args, **kwargs).iloc[:-1]

    stub.run_days(calendar[271:272])

    assert strategy._held(stub.ctx) == set(codes[1:6])
    assert stub.orders[-1][:3] == ('S', str(calendar[271].date()), codes[0])
