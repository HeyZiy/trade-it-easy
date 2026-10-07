"""独立预期差诊断：有意义的信号对照和平台时序回归，无网络/账户。"""
import importlib.util
import io
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.studies.value_revenue.internal import core
from research.studies.value_revenue.internal.build_platform import render


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'research/studies/value_revenue/study_jq.py'


def financial_rows(code='600001.XSHG'):
    return pd.DataFrame([
        dict(code=code, statDate='2024q2', pubDate='2024-08-20', inc_revenue_year_on_year=10, roe=3),
        dict(code=code, statDate='2024q3', pubDate='2024-10-25', inc_revenue_year_on_year=25, roe=4),
    ])


def test_financial_acceleration_requires_visible_consecutive_latest_quarters():
    rows = financial_rows()
    out = core.prepare_financials(rows, '2024-11-01')
    assert out.iloc[0]['improvement'] == 15
    assert out.iloc[0]['previous_published'] == pd.Timestamp('2024-08-20')
    assert core.prepare_financials(rows, '2024-10-24').empty
    rows.loc[0, 'statDate'] = '2024q1'
    assert core.prepare_financials(rows, '2024-11-01').empty


def test_financial_missing_latest_stale_or_ambiguous_is_not_backfilled():
    rows = financial_rows()
    rows.loc[1, 'inc_revenue_year_on_year'] = np.nan
    assert core.prepare_financials(rows, '2024-11-01').empty
    assert core.prepare_financials(financial_rows(), '2025-08-01').empty
    duplicate = pd.concat([financial_rows(), financial_rows().iloc[[1]]], ignore_index=True)
    assert core.prepare_financials(duplicate, '2024-11-01').empty
    rows = financial_rows()
    rows.loc[1, 'pubDate'] = '2024-01-01'  # 报告期尚未发生的伪披露
    assert core.prepare_financials(rows, '2024-11-01').empty


def matrix_frame():
    # 独立四格，因子没有中位边界同分歧义。
    return pd.DataFrame(dict(value=[1, 2, 3, 4, 5, 6, 7, 8],
                             improvement=[1, 2, 5, 6, 3, 4, 7, 8]))


def test_additive_advantage_does_not_get_called_interaction():
    frame = matrix_frame()
    cells = core.assign_cells(frame['value'], frame['improvement'])
    returns = cells.map({'A': .02, 'B': .05, 'C': .01, 'D': .04})
    stats = core.evaluate_cells(cells, returns, min_cell_n=2)
    assert stats['B'] == max(stats[c] for c in core.CELL_NAMES)
    assert stats['interaction'] == pytest.approx(0)
    returns.loc[cells == 'B'] += .02
    assert core.evaluate_cells(cells, returns, 2)['interaction'] == pytest.approx(.02)


def test_future_missing_returns_never_change_membership_or_hide_coverage():
    frame = matrix_frame()
    cells = core.assign_cells(frame['value'], frame['improvement'])
    returns = cells.map({'A': .02, 'B': .05, 'C': .01, 'D': .04})
    returns.loc[cells[cells == 'B'].index[0]] = np.nan
    stats = core.evaluate_cells(cells, returns, 2)
    assert stats['B_n'] == 2
    assert stats['B_valid_n'] == 1
    assert stats['B_coverage'] == .5
    assert np.isnan(stats['B']) and np.isnan(stats['interaction'])
    assert cells.equals(core.assign_cells(frame['value'], frame['improvement']))
    assert core.assign_cells(pd.Series([1] * 8), frame['improvement']).isna().all()


def test_regression_separates_interaction_from_main_effects_and_controls():
    rng = np.random.RandomState(8)
    n = 240
    frame = pd.DataFrame(dict(value=rng.rand(n), improvement=rng.randn(n),
                             market_cap=np.exp(rng.normal(4, 1, n)),
                             industry=np.where(np.arange(n) % 2, 'A', 'B'),
                             momentum120=rng.randn(n), revenue_yoy=rng.randn(n)))
    v, i = core.centered_rank(frame.value), core.centered_rank(frame.improvement)
    y = .03 * v + .04 * i + .2 * v * i + .02 * np.log(frame.market_cap)
    y += .06 * core.centered_rank(frame.momentum120) + .03 * core.centered_rank(frame.revenue_yoy)
    y += np.where(frame.industry == 'B', .01, 0)
    view = core.make_views(frame)['raw']
    stats = core.interaction_regression(view, frame, y)
    assert stats['regression_interaction'] == pytest.approx(.2)
    assert stats['regression_value'] == pytest.approx(.03)
    assert stats['regression_improvement'] == pytest.approx(.04)
    # 缺报价后保留原截面秩，不在配对样本上重新rank。
    y.iloc[:12] = np.nan
    assert core.interaction_regression(view, frame, y)['regression_interaction'] == pytest.approx(.2)


def test_hac_accounts_for_overlap_and_never_compresses_missing_periods():
    table = pd.DataFrame({'signal': pd.bdate_range('2024-01-01', periods=8)})
    metric_names = ['interaction', 'cheap_improvement_spread', 'expensive_improvement_spread',
                    'B_minus_D', 'B_minus_A', 'regression_interaction']
    metric_names += [name + suffix for name in core.FACTOR_NAMES for suffix in ('_ic', '_high_minus_low')]
    for name in metric_names + list(core.CELL_NAMES):
        table[name] = np.arange(8) / 100
    summary = core.summary_records(table, 120)
    row = next(r for r in summary if r['scope'] == 'all' and r['metric'] == 'interaction')
    assert row['hac_lags'] == 5 and np.isfinite(row['t_hac'])
    table.loc[2, 'interaction'] = np.nan
    row = next(r for r in core.summary_records(table, 120)
               if r['scope'] == 'all' and r['metric'] == 'interaction')
    assert row['n'] == 7 and np.isnan(row['t_hac'])


class Field:
    def in_(self, codes):
        return codes


class Query:
    def filter(self, codes):
        self.codes = codes
        return self


class Platform:
    """平台模拟：不存在未来交易日查询，收益端点只在到期后可读。"""
    def __init__(self, monkeypatch, split=False, paused_entry=False):
        self.calendar = pd.bdate_range('2023-01-02', periods=650)
        self.start = 480  # 2024年Q4
        self.today = self.calendar[self.start]
        self.codes = ['600%03d.XSHG' % i for i in range(40)]
        self.series = {c: pd.Series(10 * (1 + (i + 1) * .0001) ** np.arange(650),
                                   index=self.calendar) for i, c in enumerate(self.codes)}
        self.series['000906.XSHG'] = pd.Series(100.0, index=self.calendar)
        self.split_at = self.calendar[self.start + 40] if split else None
        if split:
            self.series[self.codes[0]].loc[self.split_at:] /= 2
        self.paused_entry = paused_entry
        self.logs, self.files, self.history_calls, self.fundamental_calls = [], {}, [], []
        self.callbacks = {}
        jq = ModuleType('jqdata')
        for name in ('get_all_securities', 'get_extras', 'get_fundamentals',
                     'get_history_fundamentals', 'get_price', 'get_industry',
                     'get_trade_days'):
            setattr(jq, name, getattr(self, name))
        jq.query = lambda *fields: Query()
        jq.valuation = SimpleNamespace(code=Field(), pe_ratio=Field(), market_cap=Field())
        jq.indicator = SimpleNamespace(statDate=Field(), pubDate=Field(),
                                       inc_revenue_year_on_year=Field(), roe=Field())
        # 回测引擎注入策略全局；这些函数并不是jqdata模块属性。
        self.engine_globals = dict(
            set_option=lambda *args: None,
            set_benchmark=lambda *args: None,
            run_daily=lambda func, time: self.callbacks.update({time: func}),
            record=lambda **kwargs: None,
            log=SimpleNamespace(info=self.logs.append, set_level=lambda *args: None),
            get_current_data=self.get_current_data, write_file=self.write_file,
        )
        # 若研究误下单，立刻失败，而不是让测试静默忽略。
        self.engine_globals['order_target_value'] = lambda *args: pytest.fail('Signal study must not trade')
        monkeypatch.setitem(__import__('sys').modules, 'jqdata', jq)
        spec = importlib.util.spec_from_file_location('expectations_platform_test', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        self.module.__dict__.update(self.engine_globals)
        spec.loader.exec_module(self.module)
        self.module.g = SimpleNamespace()
        self.module.MIN_IC_N, self.module.MIN_CELL_N = 12, 2
        self.context = SimpleNamespace(current_dt=self.today)
        self.module.initialize(self.context)

    def run(self, days):
        for date in self.calendar[self.start:self.start + days]:
            self.today = date
            self.context.current_dt = date
            self.module.on_open(self.context)
            self.module.on_signal(self.context)

    def get_all_securities(self, types, date):
        assert pd.Timestamp(date) < self.today
        return pd.DataFrame({'start_date': [self.calendar[0]] * len(self.codes)}, index=self.codes)

    def get_extras(self, info, codes, start_date, end_date, df):
        assert pd.Timestamp(end_date) < self.today
        return pd.DataFrame(False, index=[pd.Timestamp(end_date)], columns=codes)

    def get_fundamentals(self, query, date):
        assert pd.Timestamp(date) < self.today
        return pd.DataFrame([dict(code=c, pe_ratio=5 + i, market_cap=10 + (i * 7) % 43)
                             for i, c in enumerate(self.codes) if c in query.codes])

    def get_history_fundamentals(self, codes, fields, watch_date, count, interval, stat_by_year):
        cutoff = pd.Timestamp(watch_date)
        assert cutoff < self.today and count == 2 and interval == '1q' and not stat_by_year
        self.fundamental_calls.append(cutoff)
        latest = cutoff.to_period('Q') - 1
        if latest.end_time.normalize() + pd.Timedelta(days=30) > cutoff:
            latest -= 1
        rows = []
        for c in codes:
            idx = self.codes.index(c)
            for quarter in (latest - 1, latest):
                growth = (idx % 4) * 10 + (quarter.ordinal % 3) * (idx % 7)
                rows.append(dict(code=c, statDate=quarter.end_time.normalize(),
                                 pubDate=quarter.end_time.normalize() + pd.Timedelta(days=30),
                                 inc_revenue_year_on_year=growth, roe=3 + idx % 3))
        return pd.DataFrame(rows)

    def get_price(self, codes, **kwargs):
        end = pd.Timestamp(kwargs['end_date'])
        assert end < self.today
        fields = kwargs['fields']
        assert kwargs['fq'] == 'pre' and not kwargs['fill_paused']
        if fields == ['open']:
            self.history_calls.append((self.today, end, list(codes)))
        dates = self.calendar[self.calendar <= end]
        dates = dates[-kwargs['count']:] if 'count' in kwargs else dates[dates >= pd.Timestamp(kwargs['start_date'])]
        rows = []
        for date in dates:
            for code in codes:
                row = dict(code=code, time=date)
                for field in fields:
                    value = 0.0 if field == 'paused' else self.series[code].loc[date]
                    if (field != 'paused' and self.split_at is not None and code == self.codes[0]
                            and self.today >= self.split_at > date):
                        value /= 2  # 模拟以当前回测日为参照的历史前复权价格
                    row[field] = value
                rows.append(row)
        return pd.DataFrame(rows)

    def get_industry(self, codes, date):
        assert pd.Timestamp(date) < self.today
        return {c: {'sw_l1': {'industry_code': 'A' if self.codes.index(c) % 2 else 'B'}} for c in codes}

    def get_trade_days(self, end_date, count):
        assert pd.Timestamp(end_date) <= self.today
        return self.calendar[self.calendar <= pd.Timestamp(end_date)][-count:]

    def get_current_data(self):
        return {c: SimpleNamespace(day_open=s.loc[self.today],
                                  paused=self.paused_entry and c == self.codes[0]
                                  and self.today == self.calendar[self.start + 1])
                for c, s in self.series.items()}

    def write_file(self, name, text, append=False):
        self.files[name] = (self.files.get(name, '') if append else '') + text


def test_platform_freezes_signals_and_finishes_all_three_windows_without_future_queries(monkeypatch):
    platform = Platform(monkeypatch)
    platform.run(143)
    rows = pd.DataFrame(platform.module.g.rows)
    assert set(rows.horizon) == {20, 60, 120}
    first_signal = platform.calendar[platform.start]
    first = rows[(rows.signal == first_signal) & (rows['mode'] == 'raw')]
    assert set(first.horizon) == {20, 60, 120}
    for row in first.itertuples():
        assert row.asof < row.signal < row.entry < row.exit
        assert platform.calendar.get_loc(row.exit) - platform.calendar.get_loc(row.entry) == row.horizon
    assert all(today > entry for today, entry, _ in platform.history_calls)
    assert rows.groupby('horizon').size().to_dict() == {20: 14, 60: 10, 120: 4}
    platform.module.on_strategy_end(platform.context)
    assert len([name for name in platform.files if name.endswith('.csv')]) == 5
    periods = pd.read_csv(io.StringIO(platform.files['expectations_v1_periods.csv']))
    assert len(periods) == len(rows)
    assert any('[导出完成]' in line for line in platform.logs)
    import json
    metadata = json.loads(platform.files['expectations_v1_metadata.json'])
    assert metadata['unfinished_horizons'] > 0
    assert not metadata['historical_revisions_audited']


@pytest.mark.parametrize('paused', [False, True])
def test_platform_requeries_adjusted_entry_and_retains_paused_members(monkeypatch, paused):
    platform = Platform(monkeypatch, split=True, paused_entry=paused)
    platform.run(62)
    outputs = pd.read_csv(io.StringIO(platform.files['expectations_v1_returns.csv']))
    first = outputs[(outputs.signal == str(platform.calendar[platform.start].date()))
                    & (outputs.horizon == 60)]
    stock = first[first.code == platform.codes[0]].iloc[0]
    if paused:
        assert np.isnan(stock.future_return)
        row = next(r for r in platform.module.g.rows if r['horizon'] == 60 and r['mode'] == 'raw')
        assert row['pool_n'] == 40
        assert row['value_paired_n'] == 39
        assert row['value_coverage'] == pytest.approx(39 / 40)
    else:
        assert stock.future_return == pytest.approx(1.0001 ** 60 - 1)


def test_empty_first_signal_does_not_change_audit_csv_schema(monkeypatch):
    platform = Platform(monkeypatch)
    original = platform.module.build_snapshot
    calls = []

    def build(asof):
        frame, audit = original(asof)
        calls.append(asof)
        return (platform.module.empty_snapshot() if len(calls) == 1 else frame), audit

    platform.module.build_snapshot = build
    platform.run(42)
    audit = pd.read_csv(io.StringIO(platform.files['expectations_v1_audit.csv']))
    assert len(audit) == 3
    assert audit.iloc[0]['raw_factor_n'] == 0
    assert audit.iloc[1]['raw_factor_n'] == 40


def test_missing_exit_security_stays_in_frozen_groups(monkeypatch):
    platform = Platform(monkeypatch)
    original = platform.module.get_current_data
    def missing_exit():
        quotes = original()
        if platform.today >= platform.calendar[platform.start + 21]:
            quotes.pop(platform.codes[0])
        return quotes
    platform.module.get_current_data = missing_exit
    platform.run(22)
    raw = next(r for r in platform.module.g.rows if r['mode'] == 'raw')
    assert raw['pool_n'] == 40 and raw['value_paired_n'] == 39
    assert np.isnan(raw['interaction'])


def test_api_failure_does_not_silently_let_stocks_through(monkeypatch):
    platform = Platform(monkeypatch)
    def unavailable(*args, **kwargs):
        raise RuntimeError('quota unavailable')
    platform.module.get_extras = unavailable
    with pytest.raises(RuntimeError, match='quota unavailable'):
        platform.run(1)
    assert platform.module.g.signal_count == 0


def test_generated_single_file_matches_sources_and_has_no_repo_imports():
    assert SCRIPT.read_text(encoding='utf-8') == render()
    assert 'from research.' not in render()
    assert 'from jqdata import *' in render()
    assert 'jq.set_option' not in render()
    assert '_python_builtins.any(' in render()


def test_default_parameters_initialize_with_numpy_any_injected_by_platform(monkeypatch):
    platform = Platform(monkeypatch)
    platform.module.any = np.any
    # 聚宽可预置numpy名字；即使没有通配导入，合法默认参数也不能被误拒绝。
    platform.module.initialize(platform.context)
    assert platform.module.HORIZONS == (20, 60, 120)
    assert platform.module.g.signal_count == 0


def test_runtime_functions_are_injected_into_strategy_globals_not_jqdata(monkeypatch):
    platform = Platform(monkeypatch)
    import sys
    jq = sys.modules['jqdata']
    assert not hasattr(jq, 'set_option')
    assert not hasattr(jq, 'run_daily')
    assert set(platform.callbacks) == {'09:31', '14:55'}
    platform.run(62)
    platform.module.on_strategy_end(platform.context)
    assert 'expectations_v1_metadata.json' in platform.files


@pytest.mark.parametrize('horizons', [(), (0, 60), (True, 60), (20, 60.5), (20, 60, 60)])
def test_invalid_parameters_still_rejected_with_numpy_names(monkeypatch, horizons):
    platform = Platform(monkeypatch)
    platform.module.any, platform.module.int, platform.module.bool = np.any, np.int64, np.bool_
    platform.module.HORIZONS = horizons
    with pytest.raises(ValueError, match='Invalid HORIZONS'):
        platform.module.initialize(platform.context)


def test_complete_diagnostics_survive_numpy_builtin_name_injection(monkeypatch):
    platform = Platform(monkeypatch)
    for name, value in {'any': np.any, 'all': np.all, 'min': np.min, 'max': np.max,
                        'sum': np.sum, 'int': np.int64, 'float': np.float64,
                        'bool': np.bool_, 'round': np.round}.items():
        setattr(platform.module, name, value)
    platform.module.initialize(platform.context)
    platform.run(62)
    platform.module.on_strategy_end(platform.context)
    assert {row['horizon'] for row in platform.module.g.rows} == {20, 60}
    assert any('[导出完成]' in line for line in platform.logs)
    # sum(generator)容易在结束阶段留下生成器，检查实际导出类型与数量。
    import json
    metadata = json.loads(platform.files['expectations_v1_metadata.json'])
    assert isinstance(metadata['unfinished_horizons'], int)
    # 四个生效批次×三个窗口，已完成四个窗口，剩余八个。
    assert metadata['unfinished_horizons'] == 8
