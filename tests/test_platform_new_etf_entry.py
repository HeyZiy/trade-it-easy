"""华夏新ETF完整交易流程：历史边界、成本、一次入场、延迟退出和复权。"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jq_fake import JQStub

SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/new_etf_entry/chinaamc_new_etf_v1_1.py'
CODE = '159001.XSHE'


class ETFStub(JQStub):
    def __init__(self, dates, closes, opens=None):
        super().__init__(dates, closes, starting_cash=1000000, fee_buy=.0002, fee_sell=.0002)
        self.opens = self.closes if opens is None else {c: pd.Series(v, index=dates) for c,v in opens.items()}
        self.price_calls = []
        self.metadata_calls = []
        self.split = None
        self.partial_sell_once = False
        self.buy_fail = False

    def price(self, code):
        panel = self.opens if self.ctx.current_dt.hour < 15 else self.closes
        return float(panel[code].asof(self.today))

    def install(self):
        super().install()
        mod = self._module
        mod.set_slippage = lambda *a, **k: None
        mod.PriceRelatedSlippage = lambda value: value
        mod.record = lambda **kw: None
        mod.get_price = self.get_price
        mod.get_all_securities = self.metadata
        mod.get_current_data = self.current
        # 回归聚宽通配导入遮蔽 builtin 的问题。
        mod.any = np.any
        mod.max = np.max
        mod.min = np.min
        mod.sum = np.sum
        return self

    def _get_trade_days(self, start_date=None, end_date=None, count=None, **kwargs):
        # 行情样本可以从回测首日开始，但交易日历还需提供上一交易日。
        days = pd.bdate_range(self.calendar[0] - pd.Timedelta(days=30), self.calendar[-1])
        if start_date is not None:
            days = days[days >= pd.Timestamp(start_date)]
        if end_date is not None:
            days = days[days <= pd.Timestamp(end_date)]
        if count is not None:
            days = days[-count:]
        return list(days)

    def metadata(self, types, date=None):
        day = pd.Timestamp(date)
        assert day < self.today
        self.metadata_calls.append(day)
        frame = super()._get_all_securities(types, date)
        return frame[pd.to_datetime(frame['start_date']) <= day]

    def current(self):
        return {c: SimpleNamespace(paused=self.paused(c), last_price=self.price(c),
                    day_open=float(self.opens[c].asof(self.today)), high_limit=1e18, low_limit=0)
                for c in self.closes}

    def get_price(self, codes, start_date=None, end_date=None, count=None, fields=None, **kwargs):
        end = pd.Timestamp(end_date)
        assert end <= self.today
        if end == self.today:
            assert self.ctx.current_dt.hour >= 15, 'Unfinished daily bar read'
        self.price_calls.append((self.today, self.ctx.current_dt.hour, end))
        batch = isinstance(codes, list)
        members = codes if batch else [codes]
        days = self.calendar[self.calendar <= end]
        days = days[days >= pd.Timestamp(start_date)] if start_date is not None else days[-count:]
        rows=[]
        for code in members:
            for day in days:
                if day < pd.Timestamp(self.security_meta[code][1]):
                    continue
                row={'time':day, 'code':code}
                for field in fields:
                    if field == 'money':
                        row[field] = float(self.money[code].loc[day])
                    else:
                        value = float((self.opens if field == 'open' else self.closes)[code].loc[day])
                        if self.split and self.today >= self.split[0] and day < self.split[0]:
                            value *= self.split[1]
                        row[field] = value
                rows.append(row)
        frame=pd.DataFrame(rows, columns=['time','code'] + fields)
        return frame if batch else frame.set_index('time').drop(columns='code')

    def _order_target_value(self, code, value):
        if self.buy_fail:
            return SimpleNamespace(filled=0, price=0)
        before=self.portfolio.cash
        qty=int(min(value, before) / (self.price(code)*(1+self.fee_buy)) // 100)*100
        if qty <= 0:
            return None
        cost=qty*self.price(code)
        commission=max(5, cost*self.fee_buy)
        position=self.portfolio.positions.setdefault(code, SimpleNamespace(total_amount=0, avg_cost=0))
        position.total_amount += qty
        position.avg_cost=self.price(code)
        self.portfolio.cash -= cost+commission
        self.orders.append(('B',self.today,code,qty,self.price(code)))
        return SimpleNamespace(filled=qty, price=self.price(code))

    def _order_target(self, code, target):
        position=self.portfolio.positions[code]
        qty=position.total_amount
        if self.partial_sell_once:
            qty=max(1, qty//2)
            self.partial_sell_once=False
        amount=qty*self.price(code)
        self.portfolio.cash += amount-max(5,amount*self.fee_sell)
        position.total_amount -= qty
        self.orders.append(('S',self.today,code,qty,self.price(code)))
        return SimpleNamespace(filled=qty, price=self.price(code))

    def run_days(self, dates):
        for day in dates:
            self.today=pd.Timestamp(day)
            if self.split and self.today == self.split[0]:
                for position in self.portfolio.positions.values():
                    position.total_amount=int(position.total_amount/self.split[1])
                    position.avg_cost *= self.split[1]
            for time in sorted(self.scheduled):
                hour,minute=map(int,time.split(':'))
                self.ctx.current_dt=self.today+pd.Timedelta(hours=hour,minutes=minute)
                self.scheduled[time](self.ctx)


def world(closes=None, opens=None, dates=None):
    dates=pd.bdate_range('2020-01-01',periods=12) if dates is None else dates
    closes={CODE: [100.]*len(dates)} if closes is None else closes
    stub=ETFStub(dates,closes,opens)
    stub.set_security_meta({c:('测试ETF',dates[1]) for c in closes})
    stub.install()
    module=stub.load_script(SCRIPT)
    module.CHINAAMC_NAMES={c[:6]:'华夏测试股票ETF' for c in closes}
    module.ENTRY_DELAY_DAYS=2
    module.ENTRY_RETRY_DAYS=3
    module.MAX_HOLD_DAYS=30
    stub.ctx.current_dt=dates[0]
    stub.initialize()
    return stub,module,dates


def test_listing_delay_no_future_data_and_no_second_entry():
    prices=[100]*12
    prices[3]=90
    stub,module,dates=world({CODE:prices},{CODE:[100]*12})
    stub.run_days(dates)
    buys=[o for o in stub.orders if o[0]=='B']
    sells=[o for o in stub.orders if o[0]=='S']
    assert len(buys)==len(sells)==1
    assert buys[0][1]==dates[3]  # 上市完成两个交易日
    assert sells[0][1]==dates[4] # 收盘触发，次日才卖
    assert stub.g.events[CODE]['status']=='closed'
    assert all(end < today for today,hour,end in stub.price_calls if hour < 15)


def test_gap_after_stop_is_realized_and_fees_are_in_cash_pnl():
    closes=[100]*12
    closes[3]=90
    opens=[100]*12
    opens[4]=85
    stub,module,dates=world({CODE:closes},{CODE:opens})
    stub.run_days(dates)
    event=stub.g.events[CODE]
    qty=event['buy_quantity']
    expected=qty*85-max(5,qty*85*.0002)-(qty*100+max(5,qty*100*.0002))
    assert event['cash_pnl']==pytest.approx(expected)
    assert event['cash_pnl']/event['entry_outlay'] < -.08
    assert event['exit_reason']=='固定止损'


def test_split_rebases_entry_and_peak_without_false_stop():
    prices=[100]*4+[50]*8
    stub,module,dates=world({CODE:prices})
    stub.split=(dates[4],.5)
    stub.run_days(dates[:8])
    event=stub.g.events[CODE]
    assert event['status']=='holding'
    assert event['exit_reason']==''
    assert event['peak']==pytest.approx(50)
    assert len(stub.orders)==1


def test_trailing_exit_keeps_previous_high_and_executes_next_day():
    closes=[100]*12
    closes[3:6]=[110,120,95]
    stub,module,dates=world({CODE:closes},{CODE:[100]*12})
    stub.run_days(dates)
    event=stub.g.events[CODE]
    assert event['exit_reason']=='高点回撤'
    assert event['exit_date']==dates[6].date()


def test_partial_exit_waits_for_full_settlement_before_recording_completion():
    closes=[100]*12
    closes[3]=90
    stub,module,dates=world({CODE:closes},{CODE:[100]*12})
    stub.partial_sell_once=True
    stub.run_days(dates[:5])
    assert stub.g.events[CODE]['status']=='holding'
    assert not stub.g.completed
    stub.run_days(dates[5:6])
    event=stub.g.events[CODE]
    assert event['status']=='closed'
    assert event['sold_quantity']==event['buy_quantity']
    assert len(stub.g.completed)==1
    assert len([o for o in stub.orders if o[0]=='B'])==1


def test_low_liquidity_is_counted_as_missed_instead_of_removed():
    stub,module,dates=world()
    stub.money[CODE]=pd.Series(1000.,index=dates)
    stub.run_days(dates)
    assert not stub.orders
    event=stub.g.events[CODE]
    assert event['status']=='missed'
    assert event['last_block']=='成交额不足'
    module.on_strategy_end(stub.ctx)
    assert any('missed' in line for line in stub.logs)


def test_rejected_order_does_not_mark_as_bought_and_window_is_bounded():
    stub,module,dates=world()
    stub.buy_fail=True
    stub.run_days(dates)
    event=stub.g.events[CODE]
    assert event['status']=='missed'
    assert event['attempts']==3
    assert not stub.g.active
    assert event['last_block']=='买入未成交'


def test_capacity_does_not_create_forced_rotation_or_repeat_entry():
    other='159002.XSHE'
    stub,module,dates=world({CODE:[100]*12,other:[100]*12})
    module.MAX_POSITIONS=1
    stub.run_days(dates)
    assert len([o for o in stub.orders if o[0]=='B'])==1
    assert not any(o[0]=='S' for o in stub.orders)
    assert stub.g.events[other]['status']=='missed'
    assert stub.g.events[other]['last_block']=='仓位名额不足'


def test_old_products_and_other_issuer_are_not_listing_events():
    other='159002.XSHE'
    stub,module,dates=world({CODE:[100]*12,other:[100]*12})
    stub.security_meta[CODE]=('华夏旧ETF',dates[0]-pd.Timedelta(days=20))
    module.CHINAAMC_NAMES.pop(other[:6])
    stub.run_days(dates)
    assert not stub.g.events
    assert stub.g.old_count==1
    assert not stub.orders


def test_max_holding_exits_at_next_open_after_last_allowed_close():
    stub,module,dates=world()
    module.MAX_HOLD_DAYS=2
    stub.run_days(dates)
    event=stub.g.events[CODE]
    assert event['entry_date']==dates[3].date()
    assert event['exit_date']==dates[5].date()
    assert event['exit_reason']=='持有到期'


def test_non_equity_filter_preserves_gold_equity_theme():
    spec=importlib.util.spec_from_file_location('etf_name_rules',SCRIPT)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert not module._equity_name('华夏黄金ETF')
    assert not module._equity_name('华夏公司债ETF')
    assert not module._equity_name('华夏豆粕期货ETF')
    assert module._equity_name('华夏中证沪深港黄金产业股票ETF')
    assert module._equity_name('华夏中证500自由现金流ETF')
    assert not module._equity_name('华夏现金管理ETF')


def test_incomplete_position_is_left_open_and_summary_prints_without_export():
    stub,module,dates=world()
    stub.run_days(dates[:5])
    module.on_strategy_end(stub.ctx)
    assert stub.g.events[CODE]['status']=='holding'
    assert len(stub.g.active)==1
    assert any('组合收益' in line for line in stub.logs)
    assert any('holding' in line and '盈亏=NA' in line for line in stub.logs)


def test_exit_signal_survives_suspension_until_trading_resumes():
    closes = [100.] * 12
    closes[3] = 90.
    stub, module, dates = world({CODE: closes}, {CODE: [100.] * 12})
    stub.money[CODE].loc[dates[4]] = 0.
    stub.run_days(dates[:5])
    event = stub.g.events[CODE]
    assert event['status'] == 'holding'
    assert event['exit_reason'] == '固定止损'
    assert stub.g.blocked['卖出停牌'] == 1
    stub.run_days(dates[5:6])
    assert event['status'] == 'closed'
    assert event['exit_date'] == dates[5].date()
    assert len([order for order in stub.orders if order[0] == 'B']) == 1


def test_historical_issuer_name_supplements_codes_absent_from_snapshot():
    stub, module, dates = world()
    module.CHINAAMC_NAMES = {}
    stub.security_meta[CODE] = ('华夏历史股票ETF', dates[1])
    stub.run_days(dates[:5])
    event = stub.g.events[CODE]
    assert event['source'] == '历史名称补充'
    assert event['status'] == 'holding'
