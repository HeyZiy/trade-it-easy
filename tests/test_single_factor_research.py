"""单因子诊断的时间边界、缺失处理与中性化验证，无聚宽账户。"""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest


PATH = Path(__file__).resolve().parents[1] / 'research/tools/single_factor/single_factor_test.py'
SPEC = importlib.util.spec_from_file_location('single_factor_research', PATH)
sf = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sf
SPEC.loader.exec_module(sf)


def test_group_membership_does_not_depend_on_future_missing_returns():
    factor = pd.Series(range(10), index=list('abcdefghij'), dtype=float)
    future = factor / 100
    future.loc['a'] = np.nan
    stats, groups = sf.evaluate_period(factor, future, 5, 5)
    assert groups.loc['a'] == groups.loc['b'] == 1
    assert np.isnan(stats['G1'])
    assert stats['G1_n'] == 2
    assert stats['G1_coverage'] == .5
    assert stats['coverage'] == .9
    assert stats['ic'] == pytest.approx(1)
    assert stats['G5'] == pytest.approx(.085)


def test_tied_factors_do_not_get_split_by_arbitrary_code_order():
    factor = pd.Series([0] * 10 + [1, 2, 3, 4])
    assert sf.assign_groups(factor, 5).isna().all()
    assert sf.assign_groups(pd.Series([2, 2]), 5).isna().all()


def test_negative_ic_keeps_negative_icir_and_iid_t_identity():
    series = pd.Series([-.2, -.1, -.15, -.05, -.12])
    stats = sf.summarize_ic(series, 0)
    assert stats['icir'] < 0
    assert stats['t_iid'] == pytest.approx(stats['icir'] * np.sqrt(len(series)))
    assert stats['t_hac'] == pytest.approx(stats['t_iid'])
    series.iloc[2] = np.nan
    assert np.isnan(sf.summarize_ic(series)['t_hac'])


def test_neutralization_aligns_codes_and_does_not_fill_missing_controls():
    codes = list('abcdefghijkl')
    industry = pd.Series(['A'] * 6 + ['B'] * 6, index=codes)
    cap = pd.Series(np.exp(np.arange(12) / 10), index=codes)
    factor = 3 * np.log(cap) + (industry == 'B') * 4 + pd.Series(
        [1, -2, 1, 2, -1, -1] * 2, index=codes)
    cap.loc['l'] = np.nan
    residual = sf.neutralize(factor, industry.iloc[::-1], cap.iloc[::-1])
    assert np.isnan(residual['l'])
    valid = residual.dropna()
    assert abs(valid.mean()) < 1e-12
    assert abs(valid @ np.log(cap.loc[valid.index])) < 1e-12
    assert abs(valid[industry.loc[valid.index] == 'B'].sum()) < 1e-12


def test_no_artificial_factor_when_controls_explain_everything():
    cap = pd.Series(np.exp(np.arange(10) / 10))
    assert sf.neutralize(3 * np.log(cap), pd.Series(['A'] * 10), cap).isna().all()
    assert sf.mad_outlier(pd.Series([1, 1, 1, 2]), 3).iloc[-1] == 2


def test_schedule_excludes_partial_month_and_incomplete_holding_period():
    dates = pd.bdate_range('2017-12-01', '2018-07-01')
    cfg = sf.Config(start='2018-01-01', end='2018-05-15')
    periods = sf.schedule(dates, cfg)
    assert len(periods) == 3
    assert periods[-1][-1] == pd.Timestamp('2018-05-01')
    for asof, signal, entry, exit_date in periods:
        assert asof < signal < entry < exit_date <= pd.Timestamp(cfg.end)
        assert signal.month != 5  # 5月15日不是月末信号


class PriceAPI:
    def __init__(self):
        self.calendar = pd.bdate_range('2017-11-01', '2018-08-01')
        self.calls = []

    def get_trade_days(self, start_date, end_date):
        return self.calendar[(self.calendar >= start_date) & (self.calendar <= end_date)]

    def get_price(self, codes, **kwargs):
        self.calls.append((codes, kwargs))
        assert kwargs['panel'] is False
        assert kwargs['fq'] == 'post'
        assert kwargs['fill_paused'] is False
        dates = self.calendar[self.calendar <= pd.Timestamp(kwargs['end_date'])]
        if 'start_date' in kwargs:
            dates = dates[dates >= pd.Timestamp(kwargs['start_date'])]
        else:
            dates = dates[-kwargs['count']:]
        field = kwargs['fields'][0]
        rows = []
        for date in dates:
            n = self.calendar.get_loc(date)
            for i, code in enumerate(codes):
                value = 10 * (1 + (i + 1) * .001) ** n
                rows.append({'time': date, 'code': code, field: value})
        return pd.DataFrame(rows)


def test_five_day_momentum_has_six_completed_dates_and_gap_is_named_correctly():
    api = PriceAPI()
    date = pd.Timestamp('2018-01-31')
    values = sf.factor_values(api, ['A', 'B'], date, sf.Config(factor='momentum'))
    assert values['A'] == pytest.approx(1.001 ** 5 - 1)
    assert api.calls[-1][1]['count'] == 6
    assert api.calls[-1][1]['end_date'] == date
    gap = sf.factor_values(api, ['A', 'B'], date, sf.Config(factor='gap'))
    assert gap['A'] == pytest.approx(.001)


def test_study_keeps_explicit_dates_and_benchmark_identical_endpoints():
    api = PriceAPI()
    cfg = sf.Config(start='2018-01-01', end='2018-05-15', factor='momentum',
                    groups=2, min_ic_stocks=4, neutralize=False)
    pools = []

    def pool(api, asof, signal):
        pools.append((asof, signal))
        return ['A', 'B', 'C', 'D']

    tables, snapshots = sf.run_study(cfg, api, pool)
    table = tables['raw']
    assert len(table) == 3
    assert np.allclose(table['ic'], 1)
    assert (table['coverage'] == 1).all()
    assert len(snapshots) == 12
    for (_, row), (asof, signal) in zip(table.iterrows(), pools):
        assert asof < signal < row['entry'] < row['exit']
        assert row['benchmark'] == pytest.approx(
            1.001 ** (api.calendar.get_loc(row['exit']) - api.calendar.get_loc(row['entry'])) - 1)


def test_missing_exit_is_not_reported_as_zero_return():
    class MissingAPI(PriceAPI):
        def get_price(self, codes, **kwargs):
            data = super().get_price(codes, **kwargs)
            if kwargs.get('start_date') == pd.Timestamp('2018-03-01'):
                data.loc[data['code'] == 'B', kwargs['fields'][0]] = np.nan
            return data

    values = sf.endpoint_return(MissingAPI(), ['A', 'B'], pd.Timestamp('2018-02-01'),
                                pd.Timestamp('2018-03-01'))
    assert values['A'] > 0
    assert np.isnan(values['B'])


def test_historical_pool_uses_asof_status_and_retains_later_delisted_names():
    class PoolAPI(PriceAPI):
        def get_index_stocks(self, index, date):
            assert date == pd.Timestamp('2018-01-31')
            return ['LATER_DELISTED', 'ST', 'NEW', 'PAUSED']

        def get_all_securities(self, types, date):
            return pd.DataFrame({'start_date': ['2010-01-01', '2010-01-01',
                                               '2018-01-01', '2010-01-01']},
                                index=['LATER_DELISTED', 'ST', 'NEW', 'PAUSED'])

        def get_extras(self, info, codes, **kwargs):
            assert info == 'is_st'
            return pd.DataFrame({c: [c == 'ST'] for c in codes})

        def get_price(self, codes, **kwargs):
            data = super().get_price(codes, **kwargs)
            data['paused'] = (data['code'] == 'PAUSED').astype(int)
            return data

    assert sf.default_pool(PoolAPI(), pd.Timestamp('2018-01-31'),
                           pd.Timestamp('2018-02-01'), sf.Config()) == ['LATER_DELISTED']
