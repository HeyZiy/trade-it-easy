# -*- coding: utf-8 -*-
"""ROE 修订版：真实报告日期、披露截止、单季度年化与两种选股规则。"""

import sys
from types import SimpleNamespace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jq_fake import JQStub  # noqa: E402


BASE = Path(__file__).resolve().parent.parent / 'research' / 'studies' / 'roe_quality'
SCRIPTS = ['roe_rotation_v1_1.py', 'roe_rotation_v2_1.py', 'roe_rotation_v1_1_1.py',
           'roe_rotation_v1_1_3.py', 'roe_rotation_v1_1_4.py']


class AsOfJQStub(JQStub):
    """补齐本组用到的平台约定：日线不含当天，交易日 count 取末尾。"""

    def _history(self, count, unit='1d', field='close', security_list=None, **kw):
        panel = self.closes if field == 'close' else self.money
        return pd.DataFrame({c: panel[c][panel[c].index < self.today].tail(count)
                             for c in security_list})

    def _get_trade_days(self, start_date=None, end_date=None, count=None, **kw):
        days = self.calendar
        if start_date is not None:
            days = days[days >= pd.Timestamp(start_date).normalize()]
        if end_date is not None:
            days = days[days <= pd.Timestamp(end_date).normalize()]
        return list(days[-count:] if count is not None else days)


def load_world(script, rows, closes=None):
    cal = pd.bdate_range('2025-01-02', periods=100)
    codes = list(rows['code'])
    if closes is None:
        closes = {c: pd.Series(10.0, index=cal) for c in codes}
    stub = AsOfJQStub(cal, closes).install()
    stub.set_date_table(rows, table='indicator')
    stub.set_date_table(pd.DataFrame({
        'code': codes, 'pe_ratio': [20.0] * len(codes),
        'pb_ratio': [3.0] * len(codes),
    }), table='valuation')
    mod = stub.load_script(BASE / script)
    stub.initialize()
    stub.today = cal[-1]
    stub.ctx.current_dt = cal[-1] + pd.Timedelta(hours=14, minutes=55)
    return stub, mod


def fundamentals(codes):
    return pd.DataFrame({
        'code': codes, 'roe': [4.0] * len(codes),
        'inc_net_profit_year_on_year': [20.0] * len(codes),
        'statDate': ['2025-03-31'] * len(codes),
        'pubDate': ['2025-04-30'] * len(codes),
    })


@pytest.mark.parametrize('script', SCRIPTS)
def test_fundamentals_and_valuation_use_previous_trade_day(script):
    stub, mod = load_world(script, fundamentals(['600519.XSHG']))
    queries = []
    original = mod.get_fundamentals

    def observe(q, date=None, **kwargs):
        queries.append(pd.Timestamp(date).normalize())
        return original(q, date=date, **kwargs)

    mod.get_fundamentals = observe
    assert mod.build_signal(stub.ctx) == ['600519.XSHG']
    assert queries == [stub.calendar[-2], stub.calendar[-2]]


@pytest.mark.parametrize('script', SCRIPTS)
def test_single_quarter_roe_annualization_is_independent_of_quarter(script):
    _, mod = load_world(script, fundamentals(['600519.XSHG']))
    rows = fundamentals(['600001.XSHG', '600002.XSHG', '600003.XSHG', '600004.XSHG'])
    rows['statDate'] = ['2024-03-31', '2024-06-30', '2024-09-30', '2024-12-31']
    rows['pubDate'] = ['2025-04-30'] * 4
    rows['roe'] = [4.0] * 4
    prepared = mod._prepare_fundamentals(rows, pd.Timestamp('2025-05-20'))
    assert prepared['roe_ann'].tolist() == [16.0] * 4


@pytest.mark.parametrize('script', SCRIPTS)
def test_report_date_accepts_real_dates_and_quarter_strings(script):
    _, mod = load_world(script, fundamentals(['600519.XSHG']))
    values = ['2025-03-31', date(2025, 6, 30), pd.Timestamp('2025-09-30'), '2025q4']
    assert [mod._report_date(value).quarter for value in values] == [1, 2, 3, 4]


@pytest.mark.parametrize('script', SCRIPTS)
def test_unknown_report_or_publication_date_is_not_guessed(script):
    codes = ['600%03d.XSHG' % i for i in range(7)]
    rows = fundamentals(codes)
    rows.loc[1, 'statDate'] = '2025q5'
    rows.loc[2, 'statDate'] = None
    rows.loc[3, 'statDate'] = '2025-03-15'
    rows.loc[4, 'pubDate'] = '2025-05-21'  # 信号日披露：在 T-1 尚不可见
    rows.loc[5, 'pubDate'] = None
    rows.loc[6, 'statDate'] = '2025-06-30'  # 晚于观察日期
    stub, mod = load_world(script, rows)
    assert mod.build_signal(stub.ctx) == [codes[0]]


@pytest.mark.parametrize('script', SCRIPTS)
def test_nonfinite_financial_values_are_excluded(script):
    codes = ['600%03d.XSHG' % i for i in range(5)]
    rows = fundamentals(codes)
    rows.loc[1, 'roe'] = np.inf
    rows.loc[2, 'roe'] = np.nan
    rows.loc[3, 'inc_net_profit_year_on_year'] = np.inf
    rows.loc[4, 'inc_net_profit_year_on_year'] = np.nan
    stub, mod = load_world(script, rows)
    assert mod.build_signal(stub.ctx) == [codes[0]]


@pytest.mark.parametrize('script', SCRIPTS)
def test_original_st_locked_shares_paused_and_young_filters_are_kept(script):
    codes = ['600%03d.XSHG' % i for i in range(6)]
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[3]].iloc[:60] = np.nan
    stub, mod = load_world(script, fundamentals(codes), closes)
    stub.set_extras('is_st', {codes[1]: True})
    stub.set_locked_shares(pd.DataFrame({
        'code': [codes[2]], 'day': [cal[-1] + pd.Timedelta(days=5)],
    }))
    stub.money[codes[4]].iloc[-1] = 0.0
    assert set(mod.build_signal(stub.ctx)) == {codes[0], codes[5]}


def test_v1_keeps_hard_gates_and_ranks_previous_day_change():
    codes = ['600%03d.XSHG' % i for i in range(4)]
    rows = fundamentals(codes)
    rows.loc[2, 'roe'] = 2.9  # 年化 11.6%，不达 12% 门槛
    rows.loc[3, 'inc_net_profit_year_on_year'] = 9.9
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[0]].iloc[-2] = 10.1
    closes[codes[1]].iloc[-2] = 10.2
    closes[codes[0]].iloc[-1] = 11.0  # 信号日涨幅不参与旧日线排序
    closes[codes[1]].iloc[-1] = 9.0
    stub, mod = load_world(SCRIPTS[0], rows, closes)
    assert mod.build_signal(stub.ctx) == [codes[1], codes[0]]


def test_v2_keeps_rank_scoring_without_v1_growth_gate():
    codes = ['600%03d.XSHG' % i for i in range(3)]
    rows = fundamentals(codes)
    rows['roe'] = [4.0, 8.0, -1.0]
    rows['inc_net_profit_year_on_year'] = [20.0, -5.0, 100.0]
    stub, mod = load_world(SCRIPTS[1], rows)
    stub.date_tables['valuation']['pe_ratio'] = [25.0, 5.0, 5.0]
    stub.date_tables['valuation']['pb_ratio'] = [4.0, 0.5, 0.5]
    assert mod.build_signal(stub.ctx) == [codes[1], codes[0]]


@pytest.mark.parametrize('script', SCRIPTS)
def test_twenty_day_rotation_still_sells_and_rebuys(script):
    codes = ['600%03d.XSHG' % i for i in range(10)]
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    stub, mod = load_world(script, rows)
    stub.run_days(stub.calendar[70:93])
    buys = [o for o in stub.orders if o[0] == 'B']
    sells = [o for o in stub.orders if o[0] == 'S']
    first, second = stub.calendar[71], stub.calendar[91]
    assert {o[1] for o in buys} == {first.strftime('%F'), second.strftime('%F')}
    assert len(buys) == 20
    assert len(sells) == 10
    assert {o[1] for o in sells} == {second.strftime('%F')}
    assert set(stub.g.hold_since.values()) == {second}
    assert stub.portfolio.cash >= 0
    mod.on_strategy_end(stub.ctx)
    assert any('ROE 质量轮动' in line for line in stub.logs)


def test_random_rank_is_reproducible_and_independent_of_input_and_price_change():
    _, mod = load_world(SCRIPTS[2], fundamentals(['600519.XSHG']))
    codes = ['600%03d.XSHG' % i for i in range(40)]
    change = {c: i / 1000 for i, c in enumerate(codes)}
    day = pd.Timestamp('2025-05-20')
    ranked = mod._rank_candidates(codes, change, day)
    assert set(ranked) == set(codes)
    assert ranked == mod._rank_candidates(list(reversed(codes)), {}, day)
    mod.RANDOM_SEED = 1
    assert ranked != mod._rank_candidates(codes, change, day)
    mod.RANDOM_SEED = 0
    assert ranked != mod._rank_candidates(codes, change, day + pd.Timedelta(days=1))


def test_prev_change_switch_restores_v1_1_ranking_including_ties():
    codes = ['600%03d.XSHG' % i for i in range(4)]
    rows = fundamentals(codes)
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[0]].iloc[-2] = 10.1
    closes[codes[1]].iloc[-2] = 10.2
    base_stub, base = load_world(SCRIPTS[0], rows, closes)
    expected = base.build_signal(base_stub.ctx)
    stub, mod = load_world(SCRIPTS[2], rows, closes)
    mod.RANK_MODE = 'prev_change'
    assert mod.build_signal(stub.ctx) == expected
    assert mod.g.diag_reference_ranked == expected


def test_invalid_rank_mode_is_rejected_before_backtest():
    stub, mod = load_world(SCRIPTS[2], fundamentals(['600519.XSHG']))
    mod.RANK_MODE = 'typo'
    with pytest.raises(ValueError, match='RANK_MODE'):
        stub.initialize()


def test_signal_diagnostics_distinguish_pool_and_both_top_lists():
    _, mod = load_world(SCRIPTS[2], fundamentals(['600519.XSHG']))
    mod.TOP_N = 2
    mod.g.diag_reference_ranked = ['d', 'c', 'b', 'a']
    mod._update_signal_diagnostics(['a', 'b', 'c', 'd'])
    assert mod.g.diag == {'候选数': 4}
    mod.g.diag_reference_ranked = ['c', 'e', 'b']
    mod._update_signal_diagnostics(['b', 'c', 'e'])
    assert mod.g.diag == {
        '候选数': 3, '候选留存率_pct': 50.0,
        '前十留存率_pct': 50.0, '原涨幅前十留存率_pct': 50.0,
    }
    mod.g.diag_reference_ranked = []
    mod._update_signal_diagnostics([])
    assert mod.g.diag['前十留存率_pct'] == 0.0
    mod.g.diag_reference_ranked = ['a']
    mod._update_signal_diagnostics(['a'])
    assert mod.g.diag == {'候选数': 1}


def test_empty_signal_clears_reference_diagnostics():
    stub, mod = load_world(SCRIPTS[2], fundamentals(['600519.XSHG']))
    assert mod.build_signal(stub.ctx)
    stub.date_tables['indicator']['roe'] = 0.0
    assert mod.build_signal(stub.ctx) == []
    assert mod.g.diag_reference_ranked == []


def test_chart_uses_partial_fills_ignores_cancellations_and_resets_each_day():
    stub, mod = load_world(SCRIPTS[2], fundamentals(['600519.XSHG']))
    recorded = []
    mod.record = lambda **values: recorded.append(values)
    orders = {
        1: SimpleNamespace(security='A', is_buy=False, filled=40, price=10.0),
        2: SimpleNamespace(security='A', is_buy=True, filled=30, price=11.0),
        3: SimpleNamespace(security='B', is_buy=True, filled=20, price=5.0),
        4: SimpleNamespace(security='B', is_buy=False, filled=0, price=None),
    }
    mod.get_orders = lambda: orders
    mod._update_signal_diagnostics(['A', 'B'])
    mod.after_trading_end(stub.ctx)
    assert recorded[-1]['每日双边换手_pct'] == pytest.approx(830 / 1_000_000 * 100)
    assert recorded[-1]['同日回买数'] == 1
    orders.clear()
    mod.after_trading_end(stub.ctx)
    assert recorded[-1]['每日双边换手_pct'] == 0.0
    assert recorded[-1]['同日回买数'] == 0
    assert recorded[-1]['候选数'] == 2
    mod.PLOT_DIAGNOSTICS = False
    mod.get_orders = lambda: pytest.fail('disabled charts must not query orders')
    mod.after_trading_end(stub.ctx)
    assert len(recorded) == 2


def test_diagnostic_mode_has_identical_execution_to_v1_1_when_rank_restored():
    codes = ['600%03d.XSHG' % i for i in range(12)]
    rows = fundamentals(codes)
    rows['pubDate'] = '2025-04-01'
    base_stub, _ = load_world(SCRIPTS[0], rows)
    base_stub.run_days(base_stub.calendar[70:93])
    stub, mod = load_world(SCRIPTS[2], rows)
    mod.RANK_MODE = 'prev_change'
    mod.record = lambda **values: None
    mod.get_orders = lambda: {}
    for day in stub.calendar[70:93]:
        stub.run_days([day])
        mod.after_trading_end(stub.ctx)
    assert stub.orders == base_stub.orders
    assert stub.g.hold_since == base_stub.g.hold_since
    assert stub.portfolio.total_value == pytest.approx(base_stub.portfolio.total_value)


def test_roe_rank_uses_same_hard_gates_and_ignores_price_change():
    codes = ['600%03d.XSHG' % i for i in range(4)]
    rows = fundamentals(codes)
    rows['roe'] = [4.0, 8.0, 8.0, 2.9]
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    closes[codes[0]].iloc[-2] = 10.2  # 昨日涨幅最高但 ROE 排序较低
    closes[codes[2]].iloc[-2] = 10.1  # 同ROE不以涨幅破同分
    stub, mod = load_world(SCRIPTS[3], rows, closes)
    queries = []
    original = mod.get_fundamentals

    def observe(query, date=None, **kwargs):
        queries.append(date)
        return original(query, date=date, **kwargs)

    mod.get_fundamentals = observe
    assert mod.build_signal(stub.ctx) == [codes[1], codes[2], codes[0]]
    assert mod.g.diag_reference_ranked == [codes[0], codes[2], codes[1]]
    assert len(queries) == 2  # 复用原财务/估值查询，没有新增请求
    assert mod._rank_candidates(list(reversed(codes[:3])), {}, cal[-2],
                                {codes[0]: 16, codes[1]: 32, codes[2]: 32}) == [
                                    codes[1], codes[2], codes[0]]


def test_v1_1_3_can_restore_original_rank_and_reject_missing_roe_scores():
    codes = ['600001.XSHG', '600002.XSHG']
    stub, mod = load_world(SCRIPTS[3], fundamentals(codes))
    with pytest.raises(ValueError, match='quality scores'):
        mod._rank_candidates(codes, {}, stub.calendar[-2])
    mod.RANK_MODE = 'prev_change'
    assert mod._rank_candidates(codes, {codes[0]: .01, codes[1]: .02},
                                stub.calendar[-2]) == list(reversed(codes))


def test_five_day_rank_uses_six_completed_closes_and_reuses_history():
    codes = ['600001.XSHG', '600002.XSHG']
    cal = pd.bdate_range('2025-01-02', periods=100)
    closes = {c: pd.Series(10.0, index=cal) for c in codes}
    # A昨日下跌但5日上涨；B昨日上涨但5日下跌。
    closes[codes[0]].iloc[-7:-1] = [10.0, 10.1, 10.2, 10.3, 10.4, 10.3]
    closes[codes[1]].iloc[-7:-1] = [10.0, 9.9, 9.8, 9.7, 9.6, 9.7]
    # 第7根日线和当天收盘若误入窗口，会把排序颠倒。
    closes[codes[0]].iloc[-8] = 11.0
    closes[codes[1]].iloc[-8] = 9.0
    closes[codes[0]].iloc[-1] = 1.0
    closes[codes[1]].iloc[-1] = 100.0
    stub, mod = load_world(SCRIPTS[4], fundamentals(codes), closes)
    calls = []
    original = mod.history

    def observe(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    mod.history = observe
    assert mod.build_signal(stub.ctx) == codes
    assert len(calls) == 1
    assert calls[0][0][:3] == (61, '1d', 'close')
    assert mod.g.diag_reference_ranked == list(reversed(codes))
    mod.MOMENTUM_DAYS = 1
    assert mod.build_signal(stub.ctx) == list(reversed(codes))


@pytest.mark.parametrize('window', [0, 61, 5.5, True])
def test_momentum_window_rejects_invalid_parameters(window):
    stub, mod = load_world(SCRIPTS[4], fundamentals(['600001.XSHG']))
    mod.MOMENTUM_DAYS = window
    with pytest.raises(ValueError, match='MOMENTUM_DAYS'):
        stub.initialize()
