"""Correlation diagnosis uses aligned past returns and symmetric future pricing."""
import math
from datetime import date, timedelta

import pytest

from research.studies.industry_momentum.study_switch_correlation import (
    correlation_before, forward_pair, pearson, reentry_within,
)


def world():
    calendar = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(150)]
    market, a, b = {}, 100, 200
    for i, day in enumerate(calendar):
        if i:
            r = .002 + .01 * math.sin(i)
            a *= math.exp(r)
            b *= math.exp(2 * r + .003)
        for code, price in [('A', a), ('B', b)]:
            market[day, code] = dict(close=str(price), event_close=str(price * 3), volume='1000', paused='0', quality='ok')
    return calendar, market, calendar[125]


def test_pearson_is_of_returns_and_constant_series_are_unknown():
    assert pearson([1, 2, 4, 3], [3, 5, 9, 7]) == pytest.approx(1, abs=1e-14)
    assert pearson([1, 2, 4, 3], [-1, -2, -4, -3]) == pytest.approx(-1, abs=1e-14)
    assert pearson([1, 1, 1], [2, 3, 4]) is None
    assert pearson([1, 2, 3], [2, math.nan, 4]) is None


def test_past_correlation_excludes_switch_day_and_future_prices():
    calendar, market, entry = world()
    result = correlation_before(calendar, market, 'A', 'B', entry, 120)
    assert result['correlation_status'] == 'complete'
    assert result['rho'] == pytest.approx(1, abs=1e-12)
    assert result['history_start'] == calendar[4]
    assert result['history_end'] == calendar[124]
    for day in calendar[125:]:
        market[day, 'A'] = dict(close='1', event_close='999999', volume='0')
    assert correlation_before(calendar, market, 'A', 'B', entry, 120) == result
    assert correlation_before(calendar, market, 'A', 'B', calendar[120], 120)['correlation_status'] == 'insufficient_history'


@pytest.mark.parametrize('code,field,value', [('A', None, None), ('B', 'volume', '0'),
                                           ('A', 'paused', '1'), ('A', 'event_close', '')])
def test_missing_or_inactive_history_is_not_skipped(code, field, value):
    calendar, market, entry = world()
    key = calendar[20], code
    if field is None:
        market.pop(key)
    else:
        market[key][field] = value
    assert correlation_before(calendar, market, 'A', 'B', entry, 120)['correlation_status'] == 'missing_or_inactive_history'


def test_adjustment_scale_and_raw_share_split_do_not_change_correlation():
    calendar, market, entry = world()
    original = correlation_before(calendar, market, 'A', 'B', entry, 120)
    for row in market.values():
        row['close'] = str(float(row['close']) / 6)
        row['event_close'] = str(float(row['event_close']) * 11)
    changed = correlation_before(calendar, market, 'A', 'B', entry, 120)
    assert changed['rho'] == pytest.approx(original['rho'], abs=1e-12)


def test_fixed_forward_pair_uses_same_close_and_counts_switch_cost_once():
    calendar = [f'2020-01-{i + 1:02d}' for i in range(12)]
    market = {(day, code): dict(close=str(100 * growth ** i), event_close=str(700 * growth ** i), volume='1000', paused='0')
              for i, day in enumerate(calendar) for code, growth in [('A', 1.01), ('B', 1.02)]}
    result = forward_pair(calendar, market, 'A', 'B', calendar[1], 5)
    assert result['forward_status'] == 'complete'
    assert result['old_return'] == pytest.approx(1.01 ** 5 - 1, abs=1e-12)
    assert result['new_return'] == pytest.approx(1.02 ** 5 - 1, abs=1e-12)
    assert result['after_switch_cost_advantage'] == pytest.approx(.9998 / 1.0002 * 1.02 ** 5 - 1.01 ** 5, abs=1e-12)
    assert forward_pair(calendar, market, 'A', 'B', calendar[9], 5)['forward_status'] == 'right_censored'
    market.pop((calendar[3], 'A'))
    assert forward_pair(calendar, market, 'A', 'B', calendar[1], 5)['forward_status'] == 'missing_or_inactive_path'


def test_returning_to_old_etf_includes_horizon_boundary_and_censors_unknown():
    days = [dict(positions=[dict(code='B')]) for _ in range(12)]
    days[0]['positions'] = [dict(code='A')]
    days[6]['positions'] = [dict(code='A'), dict(code='C')]
    assert reentry_within(days, 1, 'A', 5) is True
    assert reentry_within(days, 1, 'A', 4) is False
    assert reentry_within(days, 8, 'A', 5) is None
