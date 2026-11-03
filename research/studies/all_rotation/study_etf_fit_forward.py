"""Fixed ten-day outcomes of rank-1 events from matching JoinQuant exports.

Run: python research/studies/etf_rotation/study_etf_fit_forward.py [export-dir] [output-dir]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.study_common import (
    HERE, json_text, mean, number, positive, quantile, read_csv, read_json, write_csv,
)


HORIZON = 10
PRICE_BASIS = 'JoinQuant fq=post aligned to decision-time raw price; gross close-based outcomes, not executed trade returns'
REVERSAL = 'First decline of 2 pre-signal daily log-return standard deviations from the running observed peak within 10 days; no hit means unobserved, not never reverses'


def valid_price(row):
    return row is not None and positive(row.get('close_raw')) and positive(row.get('close_post'))


def evaluate(metadata, signal_rows, market_rows, *, all_days=False):
    if (metadata.get('schema_version') != 1 or metadata.get('source') != 'JoinQuant strategy backtest export'
            or metadata.get('export_complete') is not True or metadata.get('errors')):
        raise ValueError('Incomplete or unsupported JoinQuant export; inspect fit_metadata.json')
    expected, signal_days, market = set(metadata['codes']), {}, {}
    for row in signal_rows:
        if row['date'] > metadata['as_of'] or row['code'] not in expected:
            raise ValueError('Signal outside export boundary')
        items = signal_days.setdefault(row['date'], {})
        if row['code'] in items:
            raise ValueError('Duplicate signal key')
        items[row['code']] = row
    for row in market_rows:
        if row['date'] > metadata['as_of'] or row['code'] not in expected:
            raise ValueError('Market data outside export boundary')
        key = row['date'], row['code']
        if key in market:
            raise ValueError('Duplicate market key')
        market[key] = row
    calendar = sorted(signal_days)
    if not calendar:
        raise ValueError('Empty signals')
    if 'expected_signal_dates' in metadata and calendar != metadata['expected_signal_dates']:
        raise ValueError('Signal calendar has missing or additional days')
    tops = []
    for date in calendar:
        rows = list(signal_days[date].values())
        if len(rows) != len(expected):
            raise ValueError(f'Incomplete pool snapshot: {date}')
        top = [row for row in rows if number(row.get('rank')) == 1]
        if len(top) > 1:
            raise ValueError(f'Multiple first-place assets: {date}')
        if top and number(top[0].get('eligible')) != 1:
            raise ValueError('First place is ineligible')
        tops.append(top[0] if top else None)
    market_dates = sorted({row['date'] for row in market_rows})
    market_indexes = {date: i for i, date in enumerate(market_dates)}
    events = []
    for i, date in enumerate(calendar):
        if all_days:
            candidates = [row for row in signal_days[date].values() if number(row.get('score')) is not None]
        else:
            candidates = [tops[i]] if tops[i] and (i == 0 or (tops[i - 1] or {}).get('code') != tops[i]['code']) else []
        for signal in candidates:
            event = dict(date=date, code=signal['code'], initialObservedState=i == 0, entryYear=date[:4],
                         observedRankDays=1, rankRunCensored=False, forwardStatus='complete', return10=None,
                         mae3=None, mae10=None, mfe10=None, volatility20=None, riskScaledReturn10=None,
                         reversalLag2Sigma=None, reversalObserved=False)
            end_rank = i + 1
            while end_rank < len(tops) and (tops[end_rank] or {}).get('code') == signal['code']:
                end_rank += 1
            event['observedRankDays'] = end_rank - i
            event['rankRunCensored'] = end_rank == len(tops)
            events.append(event)
            entry = market.get((date, signal['code']))
            decision = number(signal.get('decision_price'))
            if not valid_price(entry) or not positive(decision) or number(signal.get('paused')) != 0:
                event['forwardStatus'] = 'invalid_entry'
                continue
            if i + HORIZON >= len(calendar):
                event['forwardStatus'] = 'right_censored'
                continue
            following = [market.get((day, signal['code'])) for day in calendar[i + 1:i + HORIZON + 1]]
            if not all(valid_price(row) for row in following):
                event['forwardStatus'] = 'missing_price'
                continue
            if any(number(row.get('paused')) != 0 or not positive(row.get('volume'))
                   or not positive(row.get('money')) for row in following):
                event['forwardStatus'] = 'paused_or_zero_activity'
                continue
            adjusted_entry = decision * float(entry['close_post']) / float(entry['close_raw'])
            returns = [float(row['close_post']) / adjusted_entry - 1 for row in following]
            event.update(return10=returns[-1], mae3=min(0, *returns[:3]), mae10=min(0, *returns), mfe10=max(0, *returns))
            at = market_indexes[date]
            past = [market.get((day, signal['code'])) for day in market_dates[max(0, at - 21):at]]
            if len(past) == 21 and all(valid_price(row) and number(row.get('paused')) == 0 for row in past):
                daily = [math.log(float(row['close_post']) / float(past[k]['close_post']))
                         for k, row in enumerate(past[1:])]
                average = mean(daily)
                sigma = math.sqrt(sum((value - average) ** 2 for value in daily) / (len(daily) - 1))
                if sigma > 0:
                    event.update(volatility20=sigma, riskScaledReturn10=event['return10'] / (sigma * math.sqrt(HORIZON)))
                    peak, peak_index = adjusted_entry, 0
                    for j, row in enumerate(following):
                        price = float(row['close_post'])
                        if price > peak:
                            peak, peak_index = price, j + 1
                        if math.log(peak / price) >= 2 * sigma:
                            event.update(reversalObserved=True, reversalLag2Sigma=j + 1 - peak_index)
                            break

    def group(selected):
        complete = [event for event in selected if event['forwardStatus'] == 'complete']
        scaled = [event['riskScaledReturn10'] for event in complete if event['riskScaledReturn10'] is not None]
        return dict(code=selected[0]['code'], events=len(selected), complete10=len(complete),
                    incomplete10=len(selected) - len(complete),
                    medianObservedRankDays=quantile((event['observedRankDays'] for event in selected), .5),
                    censoredRankRuns=sum(event['rankRunCensored'] for event in selected),
                    meanReturn10=mean(event['return10'] for event in complete),
                    medianReturn10=quantile((event['return10'] for event in complete), .5),
                    winRate10=mean(event['return10'] > 0 for event in complete),
                    medianMae3=quantile((event['mae3'] for event in complete), .5),
                    medianMae10=quantile((event['mae10'] for event in complete), .5),
                    medianMfe10=quantile((event['mfe10'] for event in complete), .5),
                    volatilityMatchedCount=len(scaled), meanRiskScaledReturn10=mean(scaled),
                    reversalObservedCount=sum(event['reversalObserved'] for event in complete),
                    medianObservedReversalLag=quantile((event['reversalLag2Sigma'] for event in complete
                                                       if event['reversalObserved']), .5))
    codes = sorted({event['code'] for event in events})
    summary = [group([event for event in events if event['code'] == code]) for code in codes]
    by_year = []
    for code in codes:
        selected = [event for event in events if event['code'] == code]
        for year in sorted({event['entryYear'] for event in selected}):
            by_year.append({'entryYear': year, **group([event for event in selected if event['entryYear'] == year])})
    return dict(horizon=HORIZON, priceBasis=PRICE_BASIS,
                eventDefinition=('All pool days with a finite score (history available), irrespective of rank or eligibility'
                                 if all_days else 'First entry into eligible rank 1, including initial observed state'),
                reversalDefinition=REVERSAL, events=events, summary=summary, byYear=by_year)


def run(input_dir=HERE, output=HERE / 'results'):
    input_dir, output = Path(input_dir), Path(output)
    result = evaluate(read_json(input_dir / 'fit_metadata.json'), read_csv(input_dir / 'fit_signals.csv'),
                      read_csv(input_dir / 'fit_market.csv'))
    output.mkdir(parents=True, exist_ok=True)
    for filename, key in [('fit_forward_events.csv', 'events'), ('fit_forward_summary.csv', 'summary'),
                          ('fit_forward_by_year.csv', 'byYear')]:
        write_csv(output / filename, result[key])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=HERE)
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'results')
    args = parser.parse_args()
    result = run(args.input, args.output)
    print(json_text({**result, 'events': len(result['events']), 'byYear': len(result['byYear'])}), end='')
