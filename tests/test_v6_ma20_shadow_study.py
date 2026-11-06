import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.studies.quality_momentum import study_v6_ma20_shadow_jq as study


DATES = pd.bdate_range('2020-11-30', periods=68)
ENTRY = 22
EXIT = 32
CODE = '600001.XSHG'


def prices():
    close = np.full(len(DATES), 100.0)
    close[ENTRY:] = 99 - 0.5 * np.arange(len(DATES) - ENTRY)
    return pd.DataFrame({'open': close + 0.25, 'close': close, 'low_limit': 65.0,
                         'high_limit': 120.0, 'paused': False}, index=DATES)


def tape_from(raw):
    def get_price(code, start_date, end_date, frequency, fields, skip_paused, fq):
        result = raw.loc[str(start_date):str(end_date), fields].copy()
        if fq == 'pre':
            result *= 2
        return result

    return study._daily_prices(get_price, CODE, DATES)


def span(exit_index=EXIT, reason='trend_break'):
    return SimpleNamespace(code=CODE, entry_date=DATES[ENTRY],
                           last_held=DATES[-1] if exit_index is None else DATES[exit_index - 1],
                           exit_date=pd.NaT if exit_index is None else DATES[exit_index],
                           exit_reason=reason, closure_source='filled_order')


def test_requires_three_held_closes_and_compares_open_to_open():
    raw = prices()
    expected_sell = ENTRY + 3
    row = study._shadow_trade(span(), tape_from(raw))
    assert row['outcome'] == 'earlier'
    assert row['ma20_confirmation_date'] == DATES[expected_sell - 1]
    assert row['ma20_exit_date'] == DATES[expected_sell]
    assert row['lead_sessions'] == EXIT - expected_sell
    assert row['giveback_saved_pp'] > 0
    assert row['exit_price_edge_pct'] == pytest.approx(
        (raw['open'].iloc[expected_sell] / raw['open'].iloc[EXIT] - 1) * 100)
    assert row['rebound20_pct'] == pytest.approx(
        (raw['close'].iloc[expected_sell + 20] / raw['open'].iloc[expected_sell] - 1) * 100)


def test_paused_and_limit_down_open_followed_by_recovery_reset_confirmation():
    raw = prices()
    raw.loc[DATES[ENTRY + 3], 'paused'] = True
    raw.loc[DATES[ENTRY + 3], 'close'] = raw['close'].iloc[ENTRY + 2]
    raw.loc[DATES[ENTRY + 4], ['open', 'close']] = [65.0, 101.0]
    row = study._shadow_trade(span(), tape_from(raw))
    assert row['blocked_opens'] == 2
    assert row['first_confirmation_date'] == DATES[ENTRY + 2]
    assert row['ma20_confirmation_date'] == DATES[ENTRY + 7]
    assert row['ma20_exit_date'] == DATES[ENTRY + 8]


def test_same_day_open_positions_nontrend_exits_and_missing_data_are_separate():
    raw = prices()
    tape = tape_from(raw)
    same_day = study._shadow_trade(span(ENTRY + 3), tape)
    open_row = study._shadow_trade(span(None), tape)
    nontrend = study._shadow_trade(span(reason='st'), tape)
    raw.loc[DATES[ENTRY + 3], 'low_limit'] = np.nan
    missing = study._shadow_trade(span(), tape_from(raw))
    assert same_day['outcome'] == 'same_day' and same_day['lead_sessions'] == 0
    assert open_row['outcome'] == 'earlier_while_blend_open' and np.isnan(open_row['lead_sessions'])
    assert nontrend['outcome'] == 'other_exit'
    assert missing['outcome'] == 'data_missing'
    summary = study._summarize(pd.DataFrame([same_day, open_row, nontrend, missing])).iloc[0]
    assert summary['closed_trend_evaluable'] == 1
    assert summary['earlier'] == 0 and summary['earlier_while_blend_open'] == 1


def test_run_reconciles_holds_exports_both_tables_and_censors_forward_returns(tmp_path, monkeypatch):
    recent_entry = len(DATES) - 8
    raw = prices()
    raw.loc[DATES[EXIT:recent_entry], ['open', 'close']] = 100.0
    raw.loc[DATES[recent_entry:], 'close'] = np.arange(99.0, 91.0, -1.0)

    def get_trade_days(start_date=None, end_date=None, count=None):
        valid = DATES[DATES <= pd.Timestamp(end_date)]
        return valid[-count:] if count is not None else valid[valid >= pd.Timestamp(start_date)]

    def get_price(code, start_date, end_date, frequency, fields, skip_paused, fq):
        return raw.loc[str(start_date):str(end_date), fields]

    monkeypatch.setitem(sys.modules, 'jqdata',
                        SimpleNamespace(get_trade_days=get_trade_days, get_price=get_price))
    tables = {
        'summary': pd.DataFrame([dict(account='blend', first_date=DATES[ENTRY], last_date=DATES[-1],
                                      trading_days=len(DATES) - ENTRY)]),
        'positions': pd.DataFrame(dict(account='blend', code=CODE,
                                       date=DATES[ENTRY:EXIT].append(DATES[recent_entry:]))),
        'holding_periods': pd.DataFrame([dict(account='blend', code=CODE, entry_date=DATES[ENTRY],
                                              exit_date=DATES[EXIT], exit_reason='trend_break',
                                              closure_source='filled_order')]),
    }
    for name, table in tables.items():
        table.to_csv(tmp_path / ('%s_%s.csv' % (study.INPUT_PREFIX, name)), index=False)
    report = study.run(tmp_path)
    assert report.iloc[0]['earlier'] == 1
    assert report.iloc[0]['earlier_while_blend_open'] == 1
    rows = pd.read_csv(tmp_path / (study.OUTPUT_PREFIX + '_trades.csv'))
    assert len(rows) == 2
    assert pd.isna(rows.iloc[-1]['rebound20_pct'])
    assert (tmp_path / (study.OUTPUT_PREFIX + '_summary.csv')).is_file()

    bad = tables['positions'].drop(index=1)
    bad.to_csv(tmp_path / (study.INPUT_PREFIX + '_positions.csv'), index=False)
    with pytest.raises(ValueError, match='持仓与平仓明细不一致'):
        study.run(tmp_path)
