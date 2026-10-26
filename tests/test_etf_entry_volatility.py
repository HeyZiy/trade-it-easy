"""Entry diagnosis uses past adjusted prices and actual net segment P/L."""
import math
from datetime import date, timedelta

import pytest

from research.studies.etf_rotation.study_entry_volatility import (
    features_before_entry, reconstruct_mixed, summarize, volatility_band,
)


def world():
    calendar = [(date(2020, 1, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    market, price = {}, 100
    for i, day in enumerate(calendar):
        if i:
            price *= math.exp(.02 if i % 2 else -.01)
        market[day, 'A'] = dict(close=str(price), event_close=str(price * 3), volume='1000', paused='0', quality='ok')
    return calendar, market, calendar[25]


def test_volatility_uses_twenty_prior_returns_and_no_entry_or_future_close():
    calendar, market, entry = world()
    result = features_before_entry(calendar, market, 'A', entry)
    assert result['vol20'] == pytest.approx(math.sqrt(20 * .015 ** 2 / 19), abs=1e-14)
    assert result['feature_end'] == calendar[24]
    for day in calendar[25:]:
        market[day, 'A'] = dict(close='1', event_close='999999', volume='1000', paused='0')
    assert features_before_entry(calendar, market, 'A', entry) == result


def test_adjustment_base_and_raw_share_split_do_not_change_features():
    calendar, market, entry = world()
    original = features_before_entry(calendar, market, 'A', entry)
    scaled = {key: {**value, 'close': str(float(value['close']) / (6 if i >= 10 else 1)),
                    'event_close': str(float(value['event_close']) * 7)} for i, (key, value) in enumerate(market.items())}
    changed = features_before_entry(calendar, scaled, 'A', entry)
    assert changed['vol20'] == pytest.approx(original['vol20'], abs=1e-14)
    assert changed['top3_positive_share24'] == pytest.approx(original['top3_positive_share24'], abs=1e-12)


@pytest.mark.parametrize('field,value', [(None, None), ('volume', '0'), ('paused', '1'), ('event_close', '')])
def test_missing_or_inactive_history_is_not_filled_or_compacted(field, value):
    calendar, market, entry = world()
    key = calendar[10], 'A'
    if field is None:
        market.pop(key)
    else:
        market[key][field] = value
    assert features_before_entry(calendar, market, 'A', entry)['feature_status'] == 'missing_or_inactive_history'


def test_common_volatility_boundaries():
    assert [volatility_band(value) for value in [.009999, .01, .019999, .02]] == [
        'below_1pct', '1_to_2pct', '1_to_2pct', 'at_least_2pct']


def position(code, value, pnl, nav):
    return dict(code=code, name=code, quantity=10000, cost=10, value=value, pnl=pnl, nav=nav)


def test_switch_reconciles_incoming_commission_and_outgoing_net_profit():
    result = reconstruct_mixed([
        dict(date='2020-01-01', cash=-20, positions=[position('A', 103000, 3000, 102980)]),
        dict(date='2020-01-02', cash=4939, positions=[position('B', 99000, -1000, 103939)])])
    assert result['rows'][0]['net_pnl'] == 4959
    assert result['rows'][0]['closed'] is True
    assert result['rows'][0]['holding_days'] == 1
    assert result['rows'][1]['net_pnl'] == -1020
    assert result['rows'][1]['closed'] is False
    assert result['checks']['net_pnl_error'] == 0


def test_multi_holding_boundary_is_marked_incomplete_and_reconciled():
    result = reconstruct_mixed([
        dict(date='2020-01-01', cash=-20, positions=[position('A', 103000, 3000, 102980)]),
        dict(date='2020-01-02', cash=0, positions=[position('A', 50000, 0, 100000), position('B', 50000, 0, 100000)]),
        dict(date='2020-01-03', cash=0, positions=[position('B', 101000, 0, 101000)])])
    assert result['checks']['skipped_dates'] == ['2020-01-02', '2020-01-03']
    assert all(not row['complete_pnl'] for row in result['rows'])
    assert result['checks']['unallocated_pnl'] == -1980
    assert result['checks']['net_pnl_error'] == 0


def test_loss_coverage_normalizes_each_trade_instead_of_mixing_account_sizes():
    base = dict(code='A', year='2020', vol20=.015, top3_positive_share24=.4,
                historical_drawdown24=.03, observed_mae=-.02, observed_max_drawdown=.03)
    result = summarize([{**base, 'net_return': .1, 'net_pnl': 10, 'holding_days': 12},
                        {**base, 'net_return': -.05, 'net_pnl': -500, 'holding_days': 2}])
    assert result['return_profit_factor'] == 2
    assert result['long_winner_loss_coverage'] == 2
    assert result['quick_loss_3d_rate'] == .5
    assert result['net_pnl_yuan'] == -490
    assert result['mean_return_excluding_top5_winners'] == pytest.approx(-.05, abs=1e-12)
