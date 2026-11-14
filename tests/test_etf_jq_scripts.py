"""聚宽回测/研究平台脚本的合成交易世界测试域；覆盖行为，不验证历史收益。

research/studies 下被测脚本分两类：
- 贴进聚宽沙箱的回测脚本（含 initialize(context)）：v1.py 基线、
  medium_correlation_jq.py——JqScriptWorld 桩替整个平台（订单簿、持仓、
  委托表、行情接口），支持 fill/pending/partial/reject/cancel 成交语义；
- 研究平台脚本（无 initialize）：study_breadth_jq.py——最小 ns 直接 exec。

三类测试合居一个文件：桩替骨架同源，平台坑（沙箱旧版 pandas）的守卫也在此。
本地 runner（v1_local×回测引擎衔接）与建池研究模块（etf_pool）被测对象不同，
各自独立：见 test_etf_v1_local.py、test_etf_pool.py。
"""

import ast
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pandas as pd
import pytest

sys.modules.setdefault('jqdata', ModuleType('jqdata'))

ROOT = Path(__file__).resolve().parents[1]
V1_SCRIPT = ROOT / 'research/studies/etf/v1.py'
MEDIUM_SCRIPT = ROOT / 'research/studies/etf/medium_correlation_jq.py'
BREADTH_SCRIPT = ROOT / 'research/studies/etf/study_breadth_jq.py'

DATES = pd.bdate_range('2025-01-01', periods=260)
SCORE_EXIT_CONFIRM_CALLS = 3              # 与脚本 SCORE_EXIT_CONFIRM 一致


class JqTradingStub:
    """订单簿/持仓/委托表的桩替。

    成交语义：fill 即时全成；pending/partial 挂单占名额；reject/cancel 失败
    不占名额不扣预算；sell_limit 制造部分卖出（剩余持仓触发当日禁买）。
    """

    def hold(self, code, amount=1000, cost=None):
        self.context.portfolio.positions[code] = NS(
            total_amount=amount, avg_cost=cost or self.quotes[code].last_price,
            value=amount * self.quotes[code].last_price)

    def buy(self, code, amount):
        self.attempts.append((code, amount))
        spec = self.buy_specs.get(code, 'fill')
        if spec == 'reject':
            return None
        filled = amount if spec == 'fill' else (100 if spec == 'partial' else 0)
        active = spec in ('pending', 'partial')
        result = NS(order_id=str(len(self.orders) + 1), security=code, is_buy=True,
                    amount=amount, filled=filled, price=self.quotes[code].last_price,
                    commission=filled * self.quotes[code].last_price * .0001)
        self.orders[result.order_id] = result
        if filled:
            self.hold(code, filled)
        reserved = amount if active else filled
        self.context.portfolio.available_cash -= reserved * result.price * 1.0001
        if active:
            self.open_orders[result.order_id] = result
        return result

    def sell(self, code, target):
        assert target == 0
        self.sells.append(code)
        pos = self.context.portfolio.positions[code]
        filled = min(pos.total_amount, self.sell_limit or pos.total_amount)
        result = NS(order_id=str(len(self.orders) + 1), security=code, is_buy=False,
                    amount=pos.total_amount, filled=filled,
                    price=self.quotes[code].last_price,
                    commission=filled * self.quotes[code].last_price * .0001)
        self.orders[result.order_id] = result
        pos.total_amount -= filled
        if pos.total_amount == 0:
            pos.avg_cost = 0.  # 平台成交会原地改变持仓对象。
        self.context.portfolio.available_cash += filled * result.price - result.commission
        return result

    def run(self):
        self.ns['run_rotation'](self.context)
        self.context.current_dt += timedelta(days=1)

    def history(self, count, unit, field, security_list):
        if field == 'money':
            return pd.DataFrame({c: self.money[c][-count:] for c in security_list})
        if field == 'paused':
            return pd.DataFrame({c: self.paused[c][-count:] for c in security_list})
        return pd.DataFrame({c: self.closes[c][-count:] for c in security_list
                             if c not in getattr(self, 'missing', ())})


class JqScriptWorld(JqTradingStub):
    """exec 聚宽脚本并注入平台桩；子类只负责造数据和脚本专属桩。"""

    script = None

    def __init__(self):
        tree = ast.parse(self.script.read_text(encoding='utf-8'))
        tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom)]
        self.ns = {}
        exec(compile(tree, str(self.script), 'exec'), self.ns)
        self.logs, self.attempts, self.sells = [], [], []
        self.orders, self.open_orders, self.buy_specs = {}, {}, {}
        self.sell_limit = None
        self._build_world()
        self.ns.update(
            g=NS(), log=NS(info=lambda t: self.logs.append(str(t)),
                           set_level=lambda *a: None),
            set_option=lambda *a: None, set_benchmark=lambda *a: None,
            set_slippage=lambda *a: None, PriceRelatedSlippage=lambda x: x,
            FixedSlippage=lambda x: x,
            OrderCost=lambda **kw: NS(**kw), set_order_cost=lambda *a, **kw: None,
            run_daily=lambda *a, **kw: None,
            get_current_data=lambda: self.quotes,
            get_security_info=lambda c: NS(display_name=c),
            get_all_securities=lambda *a, **kw: pd.DataFrame(index=self.codes),
            history=self.history,
            get_orders=lambda: self.orders, get_open_orders=lambda: self.open_orders,
            order=self.buy, order_target=self.sell)
        self._script_stubs()
        self.ns['initialize'](self.context)
        self.g = self.ns['g']
        self._seed()

    def _script_stubs(self):
        """子类追加脚本专属的平台桩（如逐票取数的 attribute_history）。"""

    def _seed(self):
        """初始化后的状态：视作已完成一次成功建池。"""


class V1World(JqScriptWorld):
    """v1.py（25 日回归动量基线，逐票 attribute_history 取数）。"""

    script = V1_SCRIPT

    def _build_world(self):
        self.codes = ['E%d' % i for i in range(1, 9)]
        dates = pd.bdate_range('2025-01-01', periods=250)
        self.closes = {c: pd.Series(10. + i, index=dates)
                       for i, c in enumerate(self.codes)}
        self.money = {c: np.full(60, 100_000_000.) for c in self.codes}
        self.scores = {c: 1. - i / 10. for i, c in enumerate(self.codes)}
        self.quotes = {c: NS(last_price=float(self.closes[c].iloc[-1]), paused=False)
                       for c in self.codes}
        self.context = NS(current_dt=datetime(2026, 11, 3, 14, 55),
                          portfolio=NS(positions={}, available_cash=300_000.,
                                       total_value=300_000., starting_cash=300_000.))

    def _script_stubs(self):
        self.ns['attribute_history'] = self.history_close

    def _seed(self):
        self.g.day = 1
        self.g.pool = self.codes.copy()
        self.g.pool_valid = True
        self.real_details = self.ns['momentum_details']
        self.ns['momentum_details'] = self.score_details

    def score_details(self, tail, px):
        code = next(c for c in self.codes if self.quotes[c].last_price == px)
        return self.scores[code]

    def history_close(self, code, count, *args, **kwargs):
        return self.closes[code].tail(count).to_frame('close')


class MediumWorld(JqScriptWorld):
    """medium_correlation_jq.py（中期动量+相关性限制，批量 history 取数）。"""

    script = MEDIUM_SCRIPT

    def _build_world(self):
        self.codes = ['E%d' % i for i in range(1, 7)]
        # 六只品种，三条独立走势（各两只高相关），让相关性限制可被触发。
        drift = {'E1': .0004, 'E2': .0004, 'E3': .0002, 'E4': .0002,
                 'E5': -.0002, 'E6': -.0002}
        rng = np.random.default_rng(7)
        self.returns = {c: drift[c] + rng.normal(0, .01, len(DATES))
                        for c in self.codes}
        self.returns['E2'] = .8 * self.returns['E1'] + .2 * rng.normal(0, .01, len(DATES))
        self.returns['E4'] = .8 * self.returns['E3'] + .2 * rng.normal(0, .01, len(DATES))
        self.returns['E6'] = .8 * self.returns['E5'] + .2 * rng.normal(0, .01, len(DATES))
        self.closes = {c: pd.Series(2. * np.exp(np.log1p(r).cumsum()), index=DATES)
                       for c, r in self.returns.items()}
        self.paused = {c: pd.Series(0, index=DATES) for c in self.codes}
        self.money = {c: np.full(260, 100_000_000.) for c in self.codes}
        self.missing = set()
        self.quotes = {c: NS(last_price=float(self.closes[c].iloc[-1]), paused=False)
                       for c in self.codes}
        self.context = NS(current_dt=datetime(2026, 1, 4, 9, 30),
                          portfolio=NS(positions={}, available_cash=300_000.,
                                       total_value=300_000., starting_cash=300_000.))

    def _seed(self):
        self.g.day = 1
        self.g.pool = self.codes.copy()
        self.g.pool_valid = True
        self.g.frame = pd.DataFrame({c: self.closes[c] for c in self.codes})
        self.g.frame_paused = pd.DataFrame({c: self.paused[c] for c in self.codes})
        self.g.mature = set(self.codes)


@pytest.fixture
def v1():
    return V1World()


@pytest.fixture
def mc():
    return MediumWorld()


def scores_of(world):
    rows, complete = world.ns['score_pool'](set())
    return {r['code']: r['score'] for r in rows}, complete


# ---------------------------------------------------------------------------
# v1.py：基线轮动的订单簿行为
# ---------------------------------------------------------------------------

def test_positive_holding_can_stay_below_the_old_cost_line(v1):
    v1.hold('E1', cost=12.)
    v1.run()
    assert v1.sells == []


def test_failed_pool_rebuild_defers_all_trades(v1):
    v1.g.day = 0
    v1.hold('E8')
    v1.hold('E1', cost=12.)
    v1.ns['rebuild_pool'] = lambda dt, held: (_ for _ in ()).throw(RuntimeError('offline'))
    v1.run()
    assert v1.sells == [] and v1.attempts == []
    assert not v1.g.pool_valid


def test_pool_failure_retries_on_next_evaluation(v1):
    v1.g.pool_valid = False
    v1.ns['rebuild_pool'] = lambda dt, held: []
    v1.run()
    assert v1.g.pool_valid and v1.g.pool == []


@pytest.mark.parametrize('missing_code', ['E1', 'E8'])
def test_bad_daily_history_does_not_manufacture_a_sell(v1, missing_code):
    v1.hold('E8')
    v1.closes[missing_code].iloc[-1] = np.nan
    v1.run()
    assert v1.sells == [] and v1.attempts == []


@pytest.mark.parametrize('spec', ['pending', 'partial'])
def test_accepted_buy_reserves_slot_even_before_full_fill(v1, spec):
    v1.hold('E6')
    v1.hold('E7')
    v1.scores.update(E6=2., E7=1.5)
    v1.buy_specs['E1'] = spec
    v1.run()
    assert [c for c, _ in v1.attempts] == ['E1']
    v1.run()
    assert [c for c, _ in v1.attempts] == ['E1']


@pytest.mark.parametrize('spec', ['reject', 'cancel'])
def test_unsuccessful_buy_does_not_consume_budget_or_slot(v1, spec):
    v1.hold('E6')
    v1.scores['E6'] = 2.
    v1.buy_specs['E1'] = spec
    v1.run()
    assert [c for c, _ in v1.attempts] == ['E1', 'E2']
    v1.buy_specs['E1'] = 'fill'
    v1.run()
    assert [c for c, _ in v1.attempts] == ['E1', 'E2', 'E1']


def test_existing_open_buy_counts_toward_maximum_positions(v1):
    v1.hold('E6')
    v1.hold('E7')
    v1.open_orders['old'] = NS(security='E8', is_buy=True)
    v1.run()
    assert v1.attempts == []


def test_pending_sell_is_not_submitted_twice(v1):
    v1.hold('E1', cost=12.)
    v1.scores['E1'] = -.1
    v1.g.neg_run['E1'] = v1.ns['SCORE_EXIT_CONFIRM'] - 1
    v1.open_orders['old'] = NS(security='E1', is_buy=False)
    v1.run()
    assert v1.sells == []


def test_paused_holding_keeps_its_slot(v1):
    v1.hold('E8')
    v1.quotes['E8'].paused = True
    v1.run()
    assert [c for c, _ in v1.attempts] == ['E1', 'E2']
    v1.quotes['E8'].paused = False
    v1.run()
    assert v1.sells == []


def test_initial_rebuild_and_buy_work_together(v1):
    v1.g.day, v1.g.pool, v1.g.pool_valid = 0, [], False
    v1.run()
    assert v1.g.pool_valid
    assert [c for c, _ in v1.attempts] == ['E1', 'E2', 'E3']
    for code, amount in v1.attempts:
        assert amount * v1.quotes[code].last_price <= 100_000.


def test_all_missing_pool_data_is_failure_not_confirmed_empty_pool(v1):
    v1.g.day = 0
    for code in v1.codes:
        v1.money[code][:] = np.nan
    v1.hold('E8')
    v1.run()
    assert not v1.g.pool_valid
    assert v1.attempts == [] and v1.sells == []


def test_small_pool_also_checks_maturity(v1):
    v1.g.pool = []
    v1.codes = v1.codes[:2]
    v1.closes['E2'] = v1.closes['E2'].tail(200)
    candidates = v1.ns['rebuild_pool'](v1.context.current_dt, set())
    assert candidates == ['E1']


@pytest.mark.parametrize('bad', [np.nan, np.inf, 0., -1.])
def test_invalid_price_never_becomes_a_valid_score(v1, bad):
    prices = np.linspace(10., 11., 25)
    assert v1.real_details(prices, bad) is None


@pytest.mark.parametrize('slope', [.001, -.001, .0000001])
def test_geometric_price_path_has_full_trend_fit(v1, slope):
    prices = np.exp(slope * np.arange(26))
    score = v1.real_details(prices[:-1], prices[-1])
    assert score == pytest.approx(np.exp(slope * 250) - 1)


# ---------------------------------------------------------------------------
# medium_correlation_jq.py：中期动量评分、相关性限制、批量取数口径
# ---------------------------------------------------------------------------

def test_medium_scores_match_pandas_pct_change(mc):
    """121 根窗口的两个端点涨幅与本地 pct_change(60)/pct_change(120) 一致。"""
    rows, complete = scores_of(mc)
    assert complete
    for code, score in rows.items():
        px = mc.closes[code]
        expected = (px.iloc[-1] / px.iloc[-61] - 1 + px.iloc[-1] / px.iloc[-121] - 1) / 2
        assert score == pytest.approx(expected)


def test_endpoint_pause_blocks_score(mc):
    """端点日（T、T-60、T-120）停牌不给分并置 complete=False；中间停牌不影响。"""
    mc.paused['E1'].iloc[-1] = 1          # 端点 T 停牌
    rows, complete = scores_of(mc)
    assert 'E1' not in rows and not complete
    mc2 = MediumWorld()
    mc2.paused['E1'].iloc[-50] = 1        # 窗口中间停牌
    rows2, complete2 = scores_of(mc2)
    assert 'E1' in rows2 and complete2


def test_correlation_blocks_second_highly_correlated_pick(mc):
    """E1/E2 相关 ~0.94：E1 持仓时 E2 被挡下且不产生委托，E3 不受影响。"""
    mc.hold('E1', amount=100_000)
    mc.context.portfolio.available_cash = 200_000.
    before = len(mc.attempts)
    mc.ns['execute_buys'](mc.context,
                          [{'code': 'E2', 'name': 'E2', 'score': .4},
                           {'code': 'E3', 'name': 'E3', 'score': .3}],
                          set())
    new_attempts = [c for c, _ in mc.attempts[before:]]
    assert 'E2' not in new_attempts and 'E3' in new_attempts


def test_correlation_uses_endpoint_mask_so_pause_between_does_not_zero_returns(mc):
    """窗口中间停牌日不产生虚假零收益：掩码后 E2 与 E1 仍高相关被挡下。"""
    mc.paused['E1'].iloc[-50] = 1
    mc.paused['E2'].iloc[-50] = 1
    assert mc.ns['correlated']('E2', {'E1'}) is True


def test_correlation_insufficient_overlap_treats_as_correlated(mc):
    """重叠观测不足 45 个时保守返回 True。"""
    mc.g.frame['E3'] = mc.closes['E3'].iloc[-30:].reindex(DATES)
    mc.g.frame_paused['E3'] = mc.paused['E3'].iloc[-30:].reindex(DATES)
    assert mc.ns['correlated']('E3', {'E1'}) is True


def test_zero_variance_series_treated_as_correlated(mc):
    flat = pd.Series(2.0, index=DATES)
    mc.g.frame['E5'] = flat
    mc.g.frame_paused['E5'] = pd.Series(0, index=DATES)
    assert mc.ns['correlated']('E5', {'E1'}) is True


def test_liquidity_and_maturity_windows_drop_signal_day(mc):
    """21 根 money 取前 20 根、250 根 close 取前 249 根（截至今日 = 本地 T-1）。"""
    w = MediumWorld()
    w.g.day = 0
    w.g.pool = []
    w.g.pool_valid = False
    w.codes = ['E1']
    w.g.frame, w.g.frame_paused = None, None
    w.g.mature = set()
    w.money['E1'] = np.concatenate([np.full(234, 0.), np.full(26, 40_000_000.)])
    # 最近 21 根里前 20 根（截至 T-1）均额 4000 万达标；
    # 若错把信号日计入窗口（后 20 根），最后 1 根仍是 4000 万，无法区分——
    # 故让倒数第 2 根为 0：掐头窗口避开它，含信号日的窗口会被它拉爆。
    w.money['E1'][-2] = 0.
    pool = w.ns['rebuild_pool'](w.context.current_dt, set())
    assert pool == ['E1']
    # 成熟：250 根里前 249 根有效、仅信号日停牌 → 仍成熟
    w.paused['E1'].iloc[-1] = 1
    w.g.day = 0
    pool = w.ns['rebuild_pool'](w.context.current_dt, set())
    assert pool == ['E1']
    # 前 249 根中有停牌 → 不成熟，池不含它
    w.paused['E1'].iloc[-100] = 1
    w.g.day = 0
    pool = w.ns['rebuild_pool'](w.context.current_dt, set())
    assert pool == []


def test_incomplete_pool_data_sets_rank_invalid(mc):
    """池内某只缺 121 根数据（列缺失）→ complete=False，不进排名。"""
    mc.missing.add('E5')                   # 评分批量取数拿不到该列
    rows, complete = scores_of(mc)
    assert not complete and 'E5' not in rows


def test_pool_failure_defers_trades(mc):
    mc.g.day = 0
    mc.hold('E1')
    mc.ns['rebuild_pool'] = lambda dt, held: (_ for _ in ()).throw(ValueError('offline'))
    mc.run()
    assert mc.sells == [] and mc.attempts == []
    assert not mc.g.pool_valid


def test_unfinished_sell_blocks_buys_same_day(mc):
    """卖出未完成（仍持仓）当日不买，对齐本地 fill() 对倒防护。"""
    mc.hold('E1', amount=100_000)
    mc.sell_limit = 100                    # 只卖一部分，剩余持仓
    rows = [{'code': 'E1', 'name': 'E1', 'score': -.5}]
    sold = set()
    for _ in range(SCORE_EXIT_CONFIRM_CALLS):     # 连续 3 次不合格才触发卖出
        sold = mc.ns['evaluate_exits'](mc.context, rows, True)
    assert mc.sells == ['E1']
    before = len(mc.attempts)
    mc.ns['execute_buys'](mc.context,
                          [{'code': 'E3', 'name': 'E3', 'score': .3}],
                          sold)
    assert len(mc.attempts) == before


def test_exit_confirmation_counts_consecutive_failures(mc):
    """score<=0 连续 3 次触发卖出；恢复正分清零。"""
    w = MediumWorld()
    w.hold('E1', amount=1000)
    rows = [{'code': 'E1', 'name': 'E1', 'score': -.5}]
    for i in range(3):
        w.ns['evaluate_exits'](w.context, rows, True)
        assert w.sells == (['E1'] if i == 2 else []), "第 %d 次" % (i + 1)
    w2 = MediumWorld()
    w2.hold('E1', amount=1000)
    w2.ns['evaluate_exits'](w2.context, rows, True)
    w2.ns['evaluate_exits'](w2.context,
                            [{'code': 'E1', 'name': 'E1', 'score': .5}], True)
    w2.ns['evaluate_exits'](w2.context, rows, True)
    assert w2.sells == []                 # 中间恢复过，重新计数


# ---------------------------------------------------------------------------
# 平台守卫：所有回测脚本的沙箱兼容性
# ---------------------------------------------------------------------------

def jq_backtest_scripts():
    """发现所有“贴进聚宽回测沙箱”的脚本：research/studies 下含 initialize(context) 的 py。

    判定依据（2026-11 核对）：本地 runner（v1_local/study_local_trend）没有
    initialize，用的是本机新版 pandas，可以 to_numpy/ffill；只有回测脚本
    才进 jqcore 沙箱。新回测脚本落盘后自动纳入，无需逐个写测试。
    """
    return sorted(p for p in (ROOT / 'research/studies').rglob('*.py')
                  if 'def initialize(context)' in p.read_text(encoding='utf-8'))


def test_no_pandas_modern_api_in_jq_scripts():
    """聚宽沙箱 pandas 旧版：所有回测脚本禁用 DataFrame.to_numpy 等新 API。

    真事故：2024-01-02 medium_correlation_jq.correlated 崩于 to_numpy，
    而池重建/评分恰好没用它，首日建仓才暴露。守卫按脚本特征自动覆盖。
    """
    import re
    assert jq_backtest_scripts(), '发现器失效：至少应扫到 v1.py 与 medium_correlation_jq.py'
    offenders = []
    for path in jq_backtest_scripts():
        text = path.read_text(encoding='utf-8')
        # 旧版 pandas 真正缺失的是 to_numpy（0.24+）；values 全版本可用。
        # ffill/bfill 是古董 API，沙箱有，不列入。
        if re.search(r'\.to_numpy\(', text):
            offenders.append(path.name)
    assert not offenders, '聚宽回测脚本使用了旧版 pandas 没有的 DataFrame.to_numpy: %s' % offenders


# ---------------------------------------------------------------------------
# study_breadth_jq.py：研究平台脚本（无 initialize，最小 ns 直接 exec）
# ---------------------------------------------------------------------------

def test_forward_returns_with_legacy_pandas_series(monkeypatch):
    jqdata = ModuleType("jqdata")
    monkeypatch.setitem(sys.modules, "jqdata", jqdata)

    def unavailable(_):
        raise AttributeError("'Series' object has no attribute 'to_numpy'")

    monkeypatch.setattr(pd.Series, "to_numpy", property(unavailable))
    pair = dict(signal_date="2026-09-01", signal_day=0, codes=["A", "B"],
                improved_index="I1", other_index="I2", momentum_gap=0.01,
                improved_change=0.1, other_change=-0.1)
    state = NS(day=0, pending=[pair], results=[])
    files = {}
    prices = {
        "2026-09-02": {"B": 20.0, "A": 10.0},
        "2026-10-09": {"B": 21.0, "A": 11.0},
        "2026-12-04": {"B": 22.0, "A": 12.0},
    }
    ns = dict(g=state, log=NS(info=lambda _: None),
              write_file=lambda name, content, append: files.update({name: content}))
    exec(compile(BREADTH_SCRIPT.read_text(encoding="utf-8"), str(BREADTH_SCRIPT), "exec"), ns)
    ns["quote_panel"] = lambda codes, today, *args, **kwargs: pd.DataFrame([prices[today]])

    for day, date in zip((1, 21, 61), prices):
        state.day = day
        ns["update_returns"](date)

    assert [row["horizon"] for row in state.results] == [20, 60]
    assert [row["spread"] for row in state.results] == pytest.approx([0.05, 0.10])
    assert not state.pending
    assert "etf_breadth_pairs.csv" in files
