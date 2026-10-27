"""Diagnose observed entry volatility without changing the pool or trading rules.

Run: python research/studies/industry_momentum/study_entry_volatility.py [output-dir]
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.analyze_positions import analyze
from research.studies.etf_rotation.study_common import (
    counts_by, csv_fields, fee, groups, json_text, mean, number, positive, quantile,
    read_csv, read_json, source_hashes, write_csv, write_json,
)

HERE = Path(__file__).resolve().parent
CROSS_ASSET = HERE.parent / 'etf_rotation'


BANDS = ['below_1pct', '1_to_2pct', 'at_least_2pct']


def volatility_band(sigma):
    return BANDS[0] if sigma < .01 else BANDS[1] if sigma < .02 else BANDS[2]


def snapshots(file):
    records = csv_fields(file, 'gb18030')
    if not records or records[0][0] != '日期' or len(records[0]) <= 12 or records[0][12] != '当日盈亏':
        raise ValueError('Unexpected snapshot header')
    by_date = {}
    for row in records[1:]:
        day = by_date.setdefault(row[0], dict(date=row[0], cash=None, positions=[]))
        if row[2] == 'Cash':
            if day['cash'] is not None:
                raise ValueError('Duplicate cash row')
            day['cash'] = number(row[7])
            continue
        match = re.fullmatch(r'(.*)\((\d{6})\.(?:XSHG|XSHE)\)', row[2])
        if len(row) != 17 or not match:
            raise ValueError('Malformed position row')
        position = dict(name=match[1], code=match[2], quantity=number(row[4].removesuffix('股')),
                        value=number(row[7]), cost=number(row[9]), pnl=number(row[12]), nav=number(row[15]))
        if any(value is None for value in position.values()):
            raise ValueError('Missing snapshot value')
        if any(item['code'] == position['code'] for item in day['positions']):
            raise ValueError('Duplicate position')
        day['positions'].append(position)
    return sorted(by_date.values(), key=lambda day: day['date'])


def reconstruct_mixed(days):
    previous, previous_nav, unallocated, max_error = [], 100000, 0, 0
    active, rows, skipped = {}, [], []
    for day in days:
        current = day['positions']
        nav = current[0]['nav'] if current else day['cash']
        if day['cash'] is None:
            raise ValueError(f'Snapshot cash/NAV mismatch: {day["date"]}')
        error = abs(nav - day['cash'] - sum(item['value'] for item in current))
        max_error = max(max_error, error)
        if error > .011 or any(abs(item['nav'] - nav) > .011 for item in current):
            raise ValueError(f'Snapshot cash/NAV mismatch: {day["date"]}')
        for position in current:
            if not any(item['code'] == position['code'] for item in previous):
                item = dict(code=position['code'], name=position['name'], entry=day['date'], exit='', closed=False,
                            complete_pnl=True, holding_days=0, entry_notional=position['cost'] * position['quantity'],
                            net_pnl=0, path=[])
                active[position['code']] = item
                rows.append(item)
            active[position['code']]['holding_days'] += 1
        change = nav - previous_nav
        if len(previous) > 1 or len(current) > 1:
            skipped.append(day['date'])
            unallocated += change
            for item in active.values():
                item['complete_pnl'] = False
        else:
            a, b = next(iter(previous), None), next(iter(current), None)
            if (a or {}).get('code') != (b or {}).get('code'):
                incoming = b['pnl'] - fee(b['cost'] * b['quantity']) if b else 0
                if a:
                    active[a['code']]['net_pnl'] += change - incoming
                elif abs(change - incoming) > .011:
                    raise ValueError('Unexplained cash entry P/L')
                if b:
                    active[b['code']]['net_pnl'] += incoming
            elif b:
                active[b['code']]['net_pnl'] += change
            elif abs(change) > .011:
                raise ValueError('Unexplained cash-only P/L')
            for item in active.values():
                item['path'].append(item['net_pnl'] / item['entry_notional'])
        for position in previous:
            if not any(item['code'] == position['code'] for item in current):
                active.pop(position['code']).update(exit=day['date'], closed=True)
        previous, previous_nav = current, nav
    reconciliation = sum(row['net_pnl'] for row in rows) + unallocated - (previous_nav - 100000)
    if abs(reconciliation) > .02:
        raise ValueError('Mixed segment P/L reconciliation failed')
    return dict(rows=rows, checks=dict(max_nav_error=max_error, net_pnl_error=reconciliation,
                                       skipped_dates=skipped, unallocated_pnl=unallocated))


def path_metrics(values):
    peak, drawdown = 1, 0
    for value in values:
        peak = max(peak, 1 + value)
        drawdown = max(drawdown, 1 - (1 + value) / peak)
    return dict(observed_mae=min([0, *values]), observed_mfe=max([0, *values]), observed_max_drawdown=drawdown)


def usable(row):
    return (row is not None and positive(row.get('close')) and positive(row.get('event_close'))
            and positive(row.get('volume')) and (row.get('paused') == '' or number(row.get('paused')) == 0)
            and (not row.get('quality') or row['quality'] == 'ok'))


def features_before_entry(calendar, market, code, entry):
    at = calendar.index(entry) if entry in calendar else -1
    if at < 25:
        return dict(feature_status='insufficient_calendar')
    dates = calendar[at - 25:at]
    rows = [market.get((date, code)) for date in dates]
    if not all(usable(row) for row in rows):
        return dict(feature_status='missing_or_inactive_history')
    prices = [float(row['event_close']) for row in rows]
    logs = [math.log(price / prices[i]) for i, price in enumerate(prices[1:])]
    recent = logs[-20:]
    average = mean(recent)
    sigma = math.sqrt(sum((value - average) ** 2 for value in recent) / 19)
    positive_logs = sorted((value for value in logs if value > 0), reverse=True)
    positive_sum = sum(positive_logs)
    peak, drawdown = prices[0], 0
    for price in prices:
        peak = max(peak, price)
        drawdown = max(drawdown, 1 - price / peak)
    return dict(feature_status='complete', feature_start=dates[0], feature_end=dates[-1], vol20=sigma,
                volatility_band=volatility_band(sigma), return24=prices[-1] / prices[0] - 1,
                historical_drawdown24=drawdown,
                top3_positive_share24=sum(positive_logs[:3]) / positive_sum if positive_sum > 0 else None,
                trend_efficiency24=sum(logs) / sum(map(abs, logs)) if sum(map(abs, logs)) > 0 else 0)


def summarize(rows):
    returns = [row['net_return'] for row in rows]
    winners = [row for row in rows if row['net_return'] > 0]
    losers = [row for row in rows if row['net_return'] < 0]
    gross_wins, gross_losses = sum(row['net_return'] for row in winners), -sum(row['net_return'] for row in losers)
    sorted_wins = sorted((row['net_return'] for row in winners), reverse=True)
    long_wins = [row for row in winners if row['holding_days'] >= 10]
    code_counts = sorted([(items[0]['code'], len(items)) for items in groups(rows, ['code'])], key=lambda item: -item[1])
    most_code, most_count = code_counts[0] if code_counts else (None, 0)
    return dict(
        n=len(rows), codes=len(code_counts), entry_years=len({row['year'] for row in rows}),
        mean_net_return=mean(returns), median_net_return=quantile(returns, .5),
        win_rate=len(winners) / len(rows) if rows else None,
        quick_loss_3d_rate=mean(row['net_return'] < 0 and row['holding_days'] <= 3 for row in rows),
        median_holding_days=quantile((row['holding_days'] for row in rows), .5),
        normalized_gross_wins=gross_wins, normalized_gross_losses=gross_losses,
        return_profit_factor=gross_wins / gross_losses if gross_losses > 0 else None,
        top5_winners_share_of_gross_wins=sum(sorted_wins[:5]) / gross_wins if gross_wins > 0 else None,
        mean_return_excluding_top5_winners=((sum(returns) - sum(sorted_wins[:5])) / (len(rows) - min(5, len(winners)))
                                          if len(rows) > min(5, len(winners)) else None),
        profitable_10plus_day_segments=len(long_wins),
        long_winner_loss_coverage=sum(row['net_return'] for row in long_wins) / gross_losses if gross_losses > 0 else None,
        net_pnl_yuan=sum(row['net_pnl'] for row in rows), median_vol20=quantile((row['vol20'] for row in rows), .5),
        mean_observed_mae=mean(row['observed_mae'] for row in rows),
        median_observed_max_drawdown=quantile((row['observed_max_drawdown'] for row in rows), .5),
        median_top3_positive_share24=quantile((row['top3_positive_share24'] for row in rows), .5),
        median_historical_drawdown24=quantile((row['historical_drawdown24'] for row in rows), .5),
        most_frequent_code=most_code, most_frequent_code_share=most_count / len(rows) if rows else None)


def grouped(rows, keys):
    return [{**{key: items[0][key] for key in keys}, **summarize(items)} for items in groups(rows, keys)]


def differences(rows):
    mid = [row for row in rows if row['volatility_band'] == BANDS[1]]
    high = [row for row in rows if row['volatility_band'] == BANDS[2]]
    return dict(mid_n=len(mid), high_n=len(high),
                high_minus_mid_mean=(mean(row['net_return'] for row in high) - mean(row['net_return'] for row in mid)
                                     if mid and high else None),
                high_minus_mid_quick_loss_rate=(mean(row['holding_days'] <= 3 and row['net_return'] < 0 for row in high)
                                               - mean(row['holding_days'] <= 3 and row['net_return'] < 0 for row in mid)
                                               if mid and high else None))


def robustness(rows):
    def strata(key):
        return [{key: value, **differences([row for row in rows if row[key] == value])}
                for value in sorted({row[key] for row in rows})]

    def leave_out(key):
        return [dict(excluded=value, **differences([row for row in rows if row[key] != value]))
                for value in sorted({row[key] for row in rows})]

    by_year, by_code = strata('year'), strata('code')
    year_strata = [row for row in by_year if row['mid_n'] >= 5 and row['high_n'] >= 5]
    code_strata = [row for row in by_code if row['mid_n'] >= 5 and row['high_n'] >= 5]
    return dict(pooled=differences(rows), by_year=by_year, by_code=by_code,
                equal_weight_year_difference=mean(row['high_minus_mid_mean'] for row in year_strata),
                qualifying_years=len(year_strata), high_better_years=sum(row['high_minus_mid_mean'] > 0 for row in year_strata),
                equal_weight_code_difference=mean(row['high_minus_mid_mean'] for row in code_strata),
                qualifying_codes=len(code_strata), leave_one_year_out=leave_out('year'), leave_one_code_out=leave_out('code'))


def segment_inputs():
    mixed_file = HERE / 'results/mixed37_comparison/mixed_holding_segments.csv'
    reference = read_csv(mixed_file)
    rebuilt = reconstruct_mixed(snapshots(HERE / 'position_mixed37.csv'))
    if len(reference) != len(rebuilt['rows']):
        raise ValueError('Mixed segment count changed')
    reference_map = {(row['entry'], row['code'][:6]): row for row in reference}
    mixed = []
    for row in rebuilt['rows']:
        old = reference_map.get((row['entry'], row['code']))
        if (not old or (old['complete_pnl'].lower() == 'true') != row['complete_pnl']
                or (old['closed'].lower() == 'true') != row['closed'] or old['exit'] != row['exit']
                or float(old['observed_days']) != row['holding_days']
                or abs(float(old['entry_notional']) - row['entry_notional']) > .011
                or (row['complete_pnl'] and abs(float(old['net_pnl']) - row['net_pnl']) > .011)):
            raise ValueError(f'Mixed segment differs from previous independent study: {row["entry"]} {row["code"]}')
        mixed.append({**{key: value for key, value in row.items() if key != 'path'}, 'account': 'mixed37',
                      'group': old['group'], 'net_return': row['net_pnl'] / row['entry_notional'], **path_metrics(row['path'])})
    original = analyze(CROSS_ASSET / 'position.csv', include_episodes=True)
    originals = [dict(account='original15', group='original15', code=row['code'][:6], name=row['name'],
                      entry=row['entry'], exit=row.get('exit', ''), closed=row['closed'], complete_pnl=True,
                      holding_days=row['closeHoldingDays'], entry_notional=row['entryNotional'], net_pnl=row['netPnl'],
                      net_return=row['netPnl'] / row['entryNotional'],
                      **path_metrics([point['returnOnEntryNotional'] for point in row['path']])) for row in original['episodes']]
    return dict(rows=mixed + originals, originalReconciliation=original['checks'], mixedReconciliation=rebuilt['checks'], mixedFile=mixed_file)


def run(output=HERE / 'results/entry_volatility'):
    cache = HERE / '_cache/pool37_event_adjusted_20260930'
    manifest = read_json(cache / 'manifest.json')
    if manifest['usable_for_returns'] is not True or len(manifest['verified_codes']) != 37 or manifest['failures']:
        raise ValueError('Adjusted snapshot is not verified')
    market_file = cache / 'market.csv'
    market_rows, market = read_csv(market_file), {}
    for row in market_rows:
        key = row['date'], row['code'][:6]
        if key in market:
            raise ValueError(f'Duplicate market observation {key}')
        market[key] = row
    calendar = sorted({row['date'] for row in market_rows})
    inputs = segment_inputs()
    all_rows = [{**row, 'year': row['entry'][:4], **features_before_entry(calendar, market, row['code'], row['entry'])}
                for row in inputs['rows']]
    for row in all_rows:
        if row['closed'] and row['complete_pnl'] and (not math.isfinite(row['net_return'])
                or abs(row['net_return'] - row['net_pnl'] / row['entry_notional']) > 1e-10):
            raise ValueError(f'Segment return does not reconcile: {row["entry"]} {row["code"]}')
        if row['feature_status'] == 'complete' and not row['feature_end'] < row['entry']:
            raise ValueError('Lookahead')
        row['included'] = row['closed'] and row['complete_pnl'] and row['feature_status'] == 'complete'
        row['exclusion'] = ('open_position' if not row['closed'] else 'incomplete_pnl' if not row['complete_pnl']
                            else row['feature_status'] if row['feature_status'] != 'complete' else '')
        concentration = row.get('top3_positive_share24')
        row['jump_concentration'] = ('no_positive_days' if concentration is None else
                                     'top3_at_least_half' if concentration >= .5 else 'top3_below_half')
    included, excluded = [row for row in all_rows if row['included']], [row for row in all_rows if not row['included']]
    summary, overall = grouped(included, ['account', 'group', 'volatility_band']), grouped(included, ['account', 'group'])
    diagnostics = {f'{row["account"]}|{row["group"]}': robustness([item for item in included
                   if item['account'] == row['account'] and item['group'] == row['group']]) for row in overall}
    cross_group = []
    for band in BANDS:
        rows = [row for row in included if row['account'] == 'mixed37' and row['volatility_band'] == band]
        orig = [row for row in rows if row['group'] == 'original15']
        industry = [row for row in rows if row['group'] == 'added_industry']
        years = []
        for year in sorted({row['year'] for row in rows}):
            a, b = [row for row in orig if row['year'] == year], [row for row in industry if row['year'] == year]
            years.append(dict(year=year, original_n=len(a), industry_n=len(b),
                              industry_minus_original_mean=mean(row['net_return'] for row in b) - mean(row['net_return'] for row in a)
                              if a and b else None))
        cross_group.append(dict(volatility_band=band, original_n=len(orig), industry_n=len(industry),
                                industry_minus_original_mean=mean(row['net_return'] for row in industry) - mean(row['net_return'] for row in orig)
                                if orig and industry else None,
                                equal_weight_year_gap=mean(row['industry_minus_original_mean'] for row in years
                                                           if row['original_n'] >= 3 and row['industry_n'] >= 3), by_year=years))
    sources = [market_file, inputs['mixedFile'], CROSS_ASSET / 'position.csv', HERE / 'position_mixed37.csv', cache / 'manifest.json']
    result = dict(method=dict(
        volatility='Sample std of 20 adjusted log returns from 21 closes ending BEFORE entry; 25 exact exchange closes required',
        common_bands_daily={'below_1pct': '[0, .01)', '1_to_2pct': '[.01, .02)', 'at_least_2pct': '[.02, infinity)'},
        pnl='Actual closed holding segments, net P/L including original account costs; mixed multi-holding-affected segments excluded',
        profit_factor='Sum positive segment returns / abs(sum negative segment returns), each normalized by its own entry notional',
        long_winner='Closed segment held at least 10 observed closing days, net return > 0; contribution divided by all losses',
        observed_risk='Cumulative actual net P/L / entry notional at held daily closes plus reconstructed exit mark; excludes intraday moves',
        quick_loss='Closed net-losing segment with at most 3 observed closing days',
        jump='Top 3 positive adjusted log returns / sum of all positive log returns in 24 pre-entry returns; concentration >= 50%',
        robustness='High minus medium volatility; year/code strata each need >=5 entries per band; omit-one diagnostics are descriptive',
        sources=source_hashes(sources, base=HERE),
        limits=['Observed selected entries only; no causal filter effect or alternative account return',
                'Segment returns are not additive or compound account returns; calendar-year risk is not entry-year mean return',
                'Group dates/scores/exposures differ; volatility conditioning does not eliminate selection bias',
                'Historical closes omit intraday adverse moves; SDK validation covers its authorized year only',
                'No historical AUM/spread/industry turnover series; no size effect claim',
                'No entry-day close is used in pre-entry features; no optimized thresholds or trading changes']),
        counts=dict(input_segments=len(all_rows), included=len(included), excluded=len(excluded)),
        exclusions=counts_by(excluded, ['account', 'group', 'exclusion']), original_account_reconciliation=inputs['originalReconciliation'],
        mixed_account_reconciliation=inputs['mixedReconciliation'], overall=overall, summary=summary,
        cross_group_same_volatility=cross_group, robustness=diagnostics)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for filename, rows in [('episodes.csv', all_rows), ('summary.csv', summary),
                           ('by_year.csv', grouped(included, ['account', 'group', 'volatility_band', 'year'])),
                           ('by_code.csv', grouped(included, ['account', 'group', 'volatility_band', 'code'])),
                           ('jump_concentration.csv', grouped(included, ['account', 'group', 'volatility_band', 'jump_concentration']))]:
        write_csv(output / filename, rows)
    write_json(output / 'summary.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'results/entry_volatility')
    args = parser.parse_args()
    result = run(args.output)
    diagnostic_keys = ['pooled', 'equal_weight_year_difference', 'qualifying_years', 'high_better_years',
                       'equal_weight_code_difference', 'qualifying_codes']
    print(json_text(dict(
        counts=result['counts'], exclusions=result['exclusions'], summary=result['summary'],
        cross_group_same_volatility=[{key: value for key, value in row.items() if key != 'by_year'}
                                     for row in result['cross_group_same_volatility']],
        robustness={key: {field: value[field] for field in diagnostic_keys}
                    for key, value in result['robustness'].items()})), end='')
