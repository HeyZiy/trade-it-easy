"""Diagnose actual single-position switches; no filtered-account simulation.

Run: python research/studies/industry_momentum/study_switch_correlation.py [output-dir]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.study_common import (
    counts_by, groups, json_text, mean, number, quantile, read_csv, read_json,
    source_hashes, write_csv, write_json,
)
from research.studies.industry_momentum.study_entry_volatility import reconstruct_mixed, snapshots, usable

HERE = Path(__file__).resolve().parent
CROSS_ASSET = HERE.parent / 'etf_rotation'


HORIZONS, LOOKBACKS = [5, 10, 20], [120, 60]


def pearson(a, b):
    if len(a) != len(b) or len(a) < 3 or any(not math.isfinite(value) for value in [*a, *b]):
        return None
    ma, mb = mean(a), mean(b)
    va, vb = sum((value - ma) ** 2 for value in a), sum((value - mb) ** 2 for value in b)
    if va < 1e-20 or vb < 1e-20:
        return None
    return max(-1, min(1, sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)))


def correlation_before(calendar, market, old_code, new_code, entry, lookback):
    at = calendar.index(entry) if entry in calendar else -1
    if at < lookback + 1:
        return dict(correlation_status='insufficient_history', rho=None)
    dates = calendar[at - lookback - 1:at]
    old, new = [market.get((date, old_code)) for date in dates], [market.get((date, new_code)) for date in dates]
    if not all(usable(row) for row in [*old, *new]):
        return dict(correlation_status='missing_or_inactive_history', rho=None)

    def returns(bars):
        return [math.log(float(row['event_close']) / float(bars[i]['event_close'])) for i, row in enumerate(bars[1:])]

    rho = pearson(returns(old), returns(new))
    return dict(correlation_status='constant_or_invalid_returns' if rho is None else 'complete', rho=rho,
                correlation_band=(None if rho is None else 'at_least_0.8' if rho >= .8
                                  else '0.5_to_0.8' if rho >= .5 else 'below_0.5'),
                history_start=dates[0], history_end=dates[-1])


def forward_pair(calendar, market, old_code, new_code, entry, horizon, commission=.0002):
    def empty(status):
        return dict(forward_status=status, old_return=None, new_return=None, gross_advantage=None,
                    after_switch_cost_advantage=None, relative_mae=None)

    at = calendar.index(entry) if entry in calendar else -1
    if at < 0 or at + horizon >= len(calendar):
        return empty('right_censored')
    dates = calendar[at:at + horizon + 1]
    old, new = [market.get((date, old_code)) for date in dates], [market.get((date, new_code)) for date in dates]
    if not all(usable(row) for row in [*old, *new]):
        return empty('missing_or_inactive_path')
    a = [float(row['event_close']) / float(old[0]['event_close']) for row in old]
    b = [float(row['event_close']) / float(new[0]['event_close']) for row in new]
    investable = (1 - commission) / (1 + commission)
    return dict(forward_status='complete', old_return=a[-1] - 1, new_return=b[-1] - 1,
                gross_advantage=b[-1] - a[-1], after_switch_cost_advantage=investable * b[-1] - a[-1],
                relative_mae=min(0, *(investable * value / a[i] - 1 for i, value in enumerate(b))))


def reentry_within(days, at, old_code, horizon):
    if at + horizon >= len(days):
        return None
    return any(any(position['code'] == old_code for position in day['positions']) for day in days[at + 1:at + horizon + 1])


def summarize(events, forwards):
    returned = [row for row in events if row['reentry5'] is not None]
    returned10 = [row for row in events if row['reentry10'] is not None]
    actual = [row for row in events if row['new_segment_net_return'] is not None]
    result = dict(switches=len(events), median_correlation=quantile((row['rho'] for row in events), .5),
                  reentry5_n=len(returned), reentry5_rate=mean(row['reentry5'] for row in returned),
                  reentry10_n=len(returned10), reentry10_rate=mean(row['reentry10'] for row in returned10),
                  actual_complete_segments=len(actual), actual_mean_new_segment_return=mean(row['new_segment_net_return'] for row in actual),
                  actual_quick_loss_3d_rate=mean(row['new_holding_days'] <= 3 and row['new_segment_net_return'] < 0 for row in actual))
    for horizon in HORIZONS:
        rows = [row for row in forwards if row['horizon'] == horizon and row['forward_status'] == 'complete']
        result.update({f'n{horizon}': len(rows),
                       f'gross_advantage{horizon}': mean(row['gross_advantage'] for row in rows),
                       f'advantage{horizon}': mean(row['after_switch_cost_advantage'] for row in rows),
                       f'median_advantage{horizon}': quantile((row['after_switch_cost_advantage'] for row in rows), .5),
                       f'outperform_rate{horizon}': mean(row['after_switch_cost_advantage'] > 0 for row in rows),
                       f'relative_mae{horizon}': mean(row['relative_mae'] for row in rows)})
    return result


def grouped(events, forwards, keys):
    result = []
    for items in groups(events, keys):
        ids = {row['id'] for row in items}
        result.append({**{key: items[0][key] for key in keys},
                       **summarize(items, [row for row in forwards if row['id'] in ids])})
    return result


def contrast(rows, horizon):
    xs = [row for row in rows if row['horizon'] == horizon and row['forward_status'] == 'complete']
    high, rest = [row for row in xs if row['rho'] >= .8], [row for row in xs if row['rho'] < .8]
    high_mean, rest_mean = mean(row['after_switch_cost_advantage'] for row in high), mean(row['after_switch_cost_advantage'] for row in rest)
    return dict(high_n=len(high), other_n=len(rest), high_advantage=high_mean, other_advantage=rest_mean,
                high_minus_other=high_mean - rest_mean if high and rest else None)


def robustness(rows, horizon):
    def strata(key):
        return [{key: value, **contrast([row for row in rows if row[key] == value], horizon)}
                for value in sorted({row[key] for row in rows})]

    def leave_out(key):
        return [dict(excluded=value, **contrast([row for row in rows if row[key] != value], horizon))
                for value in sorted({row[key] for row in rows})]

    by_year, by_type = strata('year'), strata('switch_type')
    supported = [row for row in by_year if row['high_n'] >= 5 and row['other_n'] >= 5]
    return dict(horizon=horizon, **contrast(rows, horizon), by_year=by_year, by_switch_type=by_type,
                supported_years=len(supported), high_better_years=sum(row['high_minus_other'] > 0 for row in supported),
                equal_weight_year_difference=mean(row['high_minus_other'] for row in supported),
                leave_one_year_out=leave_out('year'), leave_one_pair_out=leave_out('pair'))


def partial_exit_reason(signal):
    if signal is None:
        return 'unknown_industry_or_missing_signal'
    if number(signal.get('eligible')) == 1:
        return 'still_eligible_rank_switch'
    score = number(signal.get('score'))
    if score is None:
        return 'invalid_score'
    if score >= 5:
        return 'score_at_least_5'
    if score <= 0:
        return 'score_nonpositive_or_drop_filter'
    return 'other_ineligible'


def pool_snapshots(calendar, market, codes, originals):
    quarters = {}
    for date in (date for date in calendar if date >= '2016-01-01'):
        quarters[f'{date[:4]}Q{math.ceil(int(date[5:7]) / 3)}'] = date
    rows, domestic = [], {'510300', '159915', '515180', '512100', '588000'}
    for quarter, date in quarters.items():
        pairs = []
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                result = correlation_before(calendar, market, a, b, date, 120)
                if result['correlation_status'] != 'complete':
                    continue
                group = ('original15' if a in originals else 'industry22') if (a in originals) == (b in originals) else 'cross_pool'
                pairs.append(dict(pool=group, a=a, b=b, rho=result['rho']))
        for pool in ['original15', 'industry22', 'cross_pool', 'original_domestic5']:
            selected = [pair for pair in pairs if pair['a'] in domestic and pair['b'] in domestic] if pool == 'original_domestic5' else [pair for pair in pairs if pair['pool'] == pool]
            rows.append(dict(quarter=quarter, as_of=date, pool=pool, valid_pairs=len(selected),
                             median_correlation=quantile((pair['rho'] for pair in selected), .5),
                             high_correlation_fraction=mean(pair['rho'] >= .8 for pair in selected)))
    return rows


def concentration(forwards):
    results = []
    for account in ['mixed37', 'original15']:
        for lookback in LOOKBACKS:
            for horizon in HORIZONS:
                rows = [row for row in forwards if row['account'] == account and row['lookback'] == lookback
                        and row['horizon'] == horizon and row['rho'] >= .8 and row['forward_status'] == 'complete']
                largest = sorted(rows, key=lambda row: -abs(row['after_switch_cost_advantage']))[:5]
                cases = [{**{key: row[key] for key in ['date', 'old_code', 'new_code', 'rho', 'old_score_export',
                          'partial_exit_reason', 'old_return', 'new_return', 'after_switch_cost_advantage']},
                          'mean_without_this_event': mean(other['after_switch_cost_advantage'] for other in rows if other['id'] != row['id'])}
                         for row in largest]
                results.append(dict(account=account, lookback=lookback, horizon=horizon, n=len(rows),
                                    mean_advantage=mean(row['after_switch_cost_advantage'] for row in rows), largest_absolute_cases=cases))
    return results


def run(output=HERE / 'results/switch_correlation'):
    cache = HERE / '_cache/pool37_event_adjusted_20260930'
    manifest_file = cache / 'manifest.json'
    manifest = read_json(manifest_file)
    if manifest['usable_for_returns'] is not True or len(manifest['verified_codes']) != 37 or manifest['failures']:
        raise ValueError('Unverified adjusted data')
    market_file = cache / 'market.csv'
    market_rows, market = read_csv(market_file), {}
    calendar = sorted({row['date'] for row in market_rows})
    for row in market_rows:
        key = row['date'], row['code'][:6]
        if key in market:
            raise ValueError('Duplicate market observation')
        market[key] = row
    metadata = read_json(CROSS_ASSET / 'fit_metadata.json')
    if metadata['export_complete'] is not True or metadata.get('errors'):
        raise ValueError('Incomplete original signal export')
    originals = {code[:6] for code in metadata['codes']}
    if len(originals) != 15:
        raise ValueError('Original reference universe changed')
    signal_file, signals = CROSS_ASSET / 'fit_signals.csv', {}
    for row in read_csv(signal_file):
        key = row['date'], row['code'][:6]
        if key in signals:
            raise ValueError('Duplicate original signal')
        signals[key] = row
    all_events, forwards, boundaries, reconciliations = [], [], {}, {}
    for account, filename in [('mixed37', 'position_mixed37.csv'), ('original15', 'position.csv')]:
        days = snapshots((CROSS_ASSET if account == 'original15' else HERE) / filename)
        rebuilt = reconstruct_mixed(days)
        if any(day['date'] not in calendar for day in days):
            raise ValueError('Account date missing from market calendar')
        reconciliations[account] = rebuilt['checks']
        segments = {(row['entry'], row['code']): row for row in rebuilt['rows']}
        multi = cash_entries = cash_exits = switches = 0
        for at in range(1, len(days)):
            prior, current = days[at - 1]['positions'], days[at]['positions']
            if len(prior) > 1 or len(current) > 1:
                multi += 1
                continue
            if not prior and current:
                cash_entries += 1
                continue
            if prior and not current:
                cash_exits += 1
                continue
            if not prior or not current or prior[0]['code'] == current[0]['code']:
                continue
            switches += 1
            old, new, date = prior[0], current[0], days[at]['date']
            old_signal = signals.get((date, old['code']))
            segment = segments.get((date, new['code']))
            if segment is None:
                raise ValueError('Missing incoming actual segment')
            old_group, new_group = ['original15' if item['code'] in originals else 'industry22' for item in [old, new]]
            for lookback in LOOKBACKS:
                event = dict(id=f'{account}|{date}|{lookback}', account=account, date=date, year=date[:4], lookback=lookback,
                             old_code=old['code'], old_name=old['name'], new_code=new['code'], new_name=new['name'],
                             switch_type=f'{old_group}->{new_group}', pair='|'.join(sorted([old['code'], new['code']])),
                             reentry5=reentry_within(days, at, old['code'], 5), reentry10=reentry_within(days, at, old['code'], 10),
                             partial_exit_reason=partial_exit_reason(old_signal), old_score_export=number((old_signal or {}).get('score')),
                             old_eligible_export=number((old_signal or {}).get('eligible')),
                             new_segment_net_return=segment['net_pnl'] / segment['entry_notional'] if segment['closed'] and segment['complete_pnl'] else None,
                             new_holding_days=segment['holding_days'], **correlation_before(calendar, market, old['code'], new['code'], date, lookback))
                all_events.append(event)
                if event['correlation_status'] == 'complete':
                    forwards.extend({**event, 'horizon': horizon, **forward_pair(calendar, market, old['code'], new['code'], date, horizon)} for horizon in HORIZONS)
        boundaries[account] = dict(single_to_single_switches=switches, excluded_multi_boundary_days=multi,
                                   cash_entries_after_first_day=cash_entries, cash_exits=cash_exits)
    events = [row for row in all_events if row['correlation_status'] == 'complete']
    summary = grouped(events, forwards, ['account', 'lookback', 'correlation_band'])
    by_type = grouped(events, forwards, ['account', 'lookback', 'switch_type', 'correlation_band'])
    robust = {f'{account}|{lookback}': [robustness([row for row in forwards if row['account'] == account and row['lookback'] == lookback], horizon)
              for horizon in HORIZONS] for account in ['mixed37', 'original15'] for lookback in LOOKBACKS}
    structures = pool_snapshots(calendar, market, sorted({row['code'][:6] for row in market_rows}), originals)
    sources = [market_file, manifest_file, signal_file, CROSS_ASSET / 'fit_metadata.json', CROSS_ASSET / 'position.csv', HERE / 'position_mixed37.csv']
    result = dict(method=dict(
        primary_lookback=120, sensitivity_lookback=60, horizons=HORIZONS,
        correlation='Pearson of aligned adjusted daily log returns ending strictly before switch; exact 121/61 exchange closes, no fill or skipping',
        correlation_bands={'below_0_5': 'rho < .5', 'middle': '.5 <= rho < .8', 'high': 'rho >= .8'},
        return_reference='Same switch-day close to fixed future close, ignoring actual exits; compares new vs old, not original-pool rank1',
        cost='Sell and buy at 0.02% each; new terminal value scaled by .9998/1.0002; no end liquidation, no spread/slippage; price-path reference, not actual fills',
        reentry='Old ETF appears in any actual closing holding within next 5/10 account trading days, potentially via other assets/cash; right censoring is unknown',
        partial_exit_reasons='Exact exported signals only for original15; industry exits unknown; zero score combines flat/nonpositive momentum and drop filter',
        sources=source_hashes(sources, base=HERE),
        limits=['Selected historical switches only; high vs low correlations have different assets, dates and scores',
                'Overlapping windows and repeated pairs; no independence/significance or causal ETF-filter effect',
                'Closing snapshots miss same-day round trips and cannot verify exact 10:40 exits or prices',
                'Cash boundaries and multi-position boundaries excluded from switch comparison',
                'Added22 adjusted history verified against SDK only in its permitted year; earlier events from public fund records',
                '60 and 120 days are preset diagnostics, not optimized parameters; no pool deletion or trading changes']),
        boundary_counts=boundaries, account_reconciliations=reconciliations,
        all_switch_exit_reasons=counts_by([row for row in all_events if row['lookback'] == 120], ['account', 'partial_exit_reason']),
        correlation_coverage=counts_by(all_events, ['account', 'lookback', 'correlation_status']),
        forward_coverage=counts_by(forwards, ['account', 'lookback', 'horizon', 'forward_status']), summary=summary,
        by_switch_type=by_type, partial_exit_reasons=grouped(events, forwards, ['account', 'lookback', 'partial_exit_reason', 'correlation_band']),
        robustness=robust, high_group_concentration=concentration(forwards))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for filename, rows in [('switch_events.csv', all_events), ('forward_pairs.csv', forwards), ('summary.csv', summary),
                           ('by_switch_type.csv', by_type), ('by_year.csv', grouped(events, forwards, ['account', 'lookback', 'year', 'correlation_band'])),
                           ('by_pair.csv', grouped(events, forwards, ['account', 'lookback', 'pair', 'correlation_band'])), ('pool_structure.csv', structures)]:
        write_csv(output / filename, rows, union=True)
    write_json(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'results/switch_correlation')
    args = parser.parse_args()
    result = run(args.output)
    print(json_text(dict(boundaries=result['boundary_counts'], coverage=result['correlation_coverage'],
                         summary=result['summary'], by_switch_type_120=[row for row in result['by_switch_type']
                         if row['account'] == 'mixed37' and row['lookback'] == 120])), end='')
