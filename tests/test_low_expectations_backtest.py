"""独立回测入口的实际调度、八季财务、复权端点和导出回归测试。"""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import test_value_revenue_factor_study as baseline
from research.studies.expectations.internal.build_low_expectations_backtest import render


SCRIPT = Path(__file__).resolve().parents[1] / 'research/studies/expectations/study_jq.py'


class ProfitPlatform(baseline.Platform):
    def __init__(self, monkeypatch, **kwargs):
        monkeypatch.setattr(baseline, 'SCRIPT', SCRIPT)
        self.truncated, self.future_publication = False, False
        super().__init__(monkeypatch, **kwargs)
        jq = __import__('sys').modules['jqdata']
        jq.income = SimpleNamespace(statDate='statDate', pubDate='pubDate',
                                    np_parent_company_owners='parent')
        self.module.income = jq.income

    def get_history_fundamentals(self, codes, fields, watch_date, count, interval, stat_by_year):
        cutoff = pd.Timestamp(watch_date)
        assert cutoff < self.today and count == 8 and interval == '1q' and not stat_by_year
        assert fields == ['statDate', 'pubDate', 'parent']
        self.fundamental_calls.append(cutoff)
        latest = cutoff.to_period('Q') - 1
        if latest.end_time.normalize() + pd.Timedelta(days=30) > cutoff:
            latest -= 1
        rows = []
        for code in codes:
            idx = self.codes.index(code)
            for offset in range(8):
                quarter = latest - 7 + offset
                profit = 100 + idx
                if offset >= 4:
                    profit *= 1.2 if idx % 2 == 0 else .8
                published = quarter.end_time.normalize() + pd.Timedelta(days=30)
                if self.future_publication and code == self.codes[0] and offset == 7:
                    published = cutoff + pd.Timedelta(days=1)
                rows.append(dict(code=code, statDate=quarter.end_time.normalize(),
                                 pubDate=published, np_parent_company_owners=profit))
        result = pd.DataFrame(rows)
        return result.groupby('code').tail(5) if self.truncated else result


def read_export(platform, name):
    return pd.read_csv(io.StringIO(platform.files['low_expectations_v2_standalone_' + name + '.csv']))


def test_standalone_full_lifecycle_needs_no_input_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    platform = ProfitPlatform(monkeypatch)
    platform.run(142)
    platform.module.on_strategy_end(platform.context)
    metadata = json.loads(platform.files['low_expectations_v2_standalone_metadata.json'])
    assert metadata['no_external_input_files'] and metadata['status'] == 'diagnostics_completed'
    assert metadata['eligible_records'] == 40 * 8
    assert {row['horizon'] for row in platform.module.g.rows} == {20, 60, 120}
    groups = read_export(platform, 'frozen_groups')
    first = groups[groups.signal == str(platform.calendar[platform.start].date())]
    assert len(first) == 40
    assert set(first.cell) == {'A', 'B', 'C', 'D'}
    assert (first.loc[first.cell.isin(['A', 'B']), 'pe_ratio'] < 10).all()
    assert (first.loc[first.cell.isin(['A', 'C']), 'current_ttm'] >=
            first.loc[first.cell.isin(['A', 'C']), 'prior_ttm']).all()
    assert len(read_export(platform, 'periods')) == len(platform.module.g.rows)
    assert read_export(platform, 'summary').metric.isin(['A_minus_B_observed']).any()
    assert any('[导出完成]' in text for text in platform.logs)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('paused', [False, True])
def test_split_adjustment_and_paused_entry_members(monkeypatch, paused):
    platform = ProfitPlatform(monkeypatch, split=True, paused_entry=paused)
    platform.run(62)
    returns = read_export(platform, 'returns')
    first = returns[(returns.signal == str(platform.calendar[platform.start].date())) & (returns.horizon == 60)]
    stock = first[first.code == platform.codes[0]].iloc[0]
    row = next(row for row in platform.module.g.rows if row['horizon'] == 60)
    assert row['A_n'] == 3
    if paused:
        assert np.isnan(stock.future_return) and row['A_valid_n'] == 2
        assert np.isnan(row['A_strict']) and np.isfinite(row['A_observed'])
    else:
        assert stock.future_return == pytest.approx(1.0001 ** 60 - 1)
        assert row['A_valid_n'] == 3


def test_missing_exit_does_not_change_group_membership(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    current = platform.module.get_current_data
    def missing_exit():
        quotes = current()
        if platform.today >= platform.calendar[platform.start + 21]:
            quotes.pop(platform.codes[0])
        return quotes
    platform.module.get_current_data = missing_exit
    platform.run(22)
    row = platform.module.g.rows[0]
    assert row['common_n'] == 40 and row['A_n'] == 3 and row['A_valid_n'] == 2
    assert row['A_coverage'] == pytest.approx(2/3)
    assert np.isnan(row['A_strict'])


def test_insufficient_quarters_keeps_scheduled_windows_without_fake_success(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    platform.truncated = True
    platform.run(82)
    platform.module.on_strategy_end(platform.context)
    metadata = json.loads(platform.files['low_expectations_v2_standalone_metadata.json'])
    assert metadata['status'] == 'no_eligible_eight_quarter_sample'
    assert metadata['eligible_records'] == 0 and metadata['mature_windows'] > 0
    assert set(read_export(platform, 'profit_features').profit_status) == {'insufficient_quarters'}
    summary = read_export(platform, 'summary')
    assert summary['n'].sum() == 0 and summary.scheduled_n.max() > 0


def test_unpublished_quarter_is_excluded_from_signals(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    platform.future_publication = True
    platform.run(1)
    features = read_export(platform, 'profit_features')
    assert features.loc[features.code == platform.codes[0], 'profit_status'].iloc[0] == 'not_visible'
    assert platform.module.g.audit[0]['common_sample_n'] == 39


def test_empty_universe_has_stable_audit_and_no_eligible_conclusion(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    all_st = platform.module.get_extras
    platform.module.get_extras = lambda *args, **kwargs: all_st(*args, **kwargs) | True
    platform.run(22)
    platform.module.on_strategy_end(platform.context)
    assert platform.module.g.rows[0]['common_n'] == 0
    assert platform.module.g.audit[0]['positive_pe_n'] == 0
    assert json.loads(platform.files['low_expectations_v2_standalone_metadata.json'])['eligible_records'] == 0


def test_financial_api_failure_is_not_silently_accepted(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    def failed(*args, **kwargs):
        raise RuntimeError('profit quota unavailable')
    platform.module.get_history_fundamentals = failed
    with pytest.raises(RuntimeError, match='profit quota unavailable'):
        platform.run(1)
    assert platform.module.g.signal_count == 0


def test_engine_globals_and_numpy_builtin_injection(monkeypatch):
    platform = ProfitPlatform(monkeypatch)
    assert not hasattr(__import__('sys').modules['jqdata'], 'set_option')
    for name, value in {'any': np.any, 'all': np.all, 'min': np.min, 'max': np.max,
                        'sum': np.sum, 'int': np.int64, 'float': np.float64,
                        'bool': np.bool_}.items():
        setattr(platform.module, name, value)
    platform.module.initialize(platform.context)
    platform.run(62)
    platform.module.on_strategy_end(platform.context)
    assert platform.module.g.rows
    assert isinstance(json.loads(platform.files['low_expectations_v2_standalone_metadata.json'])
                      ['unfinished_horizons'], int)


def test_generated_script_is_current_and_has_no_external_file_reads():
    source = render()
    assert SCRIPT.read_text(encoding='utf-8') == source
    assert 'from research.' not in source and 'expectations_v1_' not in source
    assert 'read_file(' not in source and 'read_csv(' not in source and 'read_text(' not in source
    assert 'def initialize(context):' in source and 'jq.set_option' not in source
