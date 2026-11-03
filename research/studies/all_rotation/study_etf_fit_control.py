"""Same-ETF, same-entry-year descriptive controls for fixed ten-day outcomes.

Run: python research/studies/etf_rotation/study_etf_fit_control.py [export-dir] [output-dir]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.study_common import (
    HERE, counts_by, groups, json_text, mean, number, quantile, read_csv, read_json, write_csv, write_json,
)
from research.studies.etf_rotation.study_etf_fit_forward import evaluate


def stats(rows):
    return dict(meanReturn=mean(row['return10'] for row in rows),
                medianReturn=quantile((row['return10'] for row in rows), .5),
                winRate=mean(row['return10'] > 0 for row in rows),
                meanMae3=mean(row['mae3'] for row in rows), meanMae10=mean(row['mae10'] for row in rows))


def compare(metadata, signals, market):
    first = evaluate(metadata, signals, market)
    daily = evaluate(metadata, signals, market, all_days=True)
    events = [event for event in first['events'] if not event['initialObservedState'] and event['forwardStatus'] == 'complete']
    valid_daily = [event for event in daily['events'] if event['forwardStatus'] == 'complete']
    ranks = {(row['date'], row['code']): row['rank'] for row in signals}
    by_year, matched = [], []
    for control in ['all_days', 'not_rank1']:
        controls = [row for row in valid_daily if control == 'all_days' or number(ranks[(row['date'], row['code'])]) != 1]
        ordinary_groups = {(items[0]['code'], items[0]['entryYear']): items for items in groups(controls, ['code', 'entryYear'])}
        for code, year in sorted({(event['code'], event['entryYear']) for event in events}):
            selected = [event for event in events if event['code'] == code and event['entryYear'] == year]
            ordinary = ordinary_groups.get((code, year), [])
            a, b = stats(selected), stats(ordinary)
            by_year.append(dict(
                control=control, code=code, entryYear=year, signalCount=len(selected), controlDays=len(ordinary),
                status='matched' if ordinary else 'no_control', signalMeanReturn10=a['meanReturn'],
                controlMeanReturn10=b['meanReturn'], excessMeanReturn10=a['meanReturn'] - b['meanReturn'] if ordinary else None,
                signalMedianReturn10=a['medianReturn'], controlMedianReturn10=b['medianReturn'],
                signalWinRate10=a['winRate'], controlWinRate10=b['winRate'],
                excessWinRate10=a['winRate'] - b['winRate'] if ordinary else None,
                signalMeanMae3=a['meanMae3'], controlMeanMae3=b['meanMae3'],
                signalMeanMae10=a['meanMae10'], controlMeanMae10=b['meanMae10']))
            if ordinary:
                matched.extend(dict(control=control, code=code, date=event['date'], entryYear=year,
                                    return10=event['return10'], controlMeanReturn10=b['meanReturn'],
                                    excessReturn10=event['return10'] - b['meanReturn']) for event in selected)

    def summarize(control, code=None):
        selected = [row for row in by_year if row['control'] == control and (code is None or row['code'] == code)]
        rows = [row for row in selected if row['status'] == 'matched']
        n = sum(row['signalCount'] for row in rows)

        def weighted(key):
            return sum(row[key] * row['signalCount'] for row in rows) / n if n else None

        outcomes = [row for row in matched if row['control'] == control and (code is None or row['code'] == code)]
        years = list(dict.fromkeys(row['entryYear'] for row in rows))
        annual = [[row['excessReturn10'] for row in outcomes if row['entryYear'] == year] for year in years]
        leave_one_year = ([mean(row['excessReturn10'] for row in outcomes if row['entryYear'] != year) for year in years]
                          if len(years) > 1 else [])
        return dict(
            control=control, code=code or 'ALL_EVENT_WEIGHTED', matchedSignals=n,
            unmatchedSignals=sum(row['signalCount'] for row in selected if row['status'] != 'matched'),
            controlDays=sum(row['controlDays'] for row in rows), signalMeanReturn10=weighted('signalMeanReturn10'),
            matchedControlMeanReturn10=weighted('controlMeanReturn10'), excessMeanReturn10=weighted('excessMeanReturn10'),
            medianEventExcessReturn10=quantile((row['excessReturn10'] for row in outcomes), .5),
            signalWinRate10=weighted('signalWinRate10'), matchedControlWinRate10=weighted('controlWinRate10'),
            excessWinRate10=weighted('excessWinRate10'), signalMeanMae3=weighted('signalMeanMae3'),
            matchedControlMeanMae3=weighted('controlMeanMae3'), signalMeanMae10=weighted('signalMeanMae10'),
            matchedControlMeanMae10=weighted('controlMeanMae10'), sampleYears=len(years),
            positiveExcessYears=sum(mean(values) > 0 for values in annual),
            minLeaveOneYearExcessMean=min(leave_one_year) if leave_one_year else None,
            maxLeaveOneYearExcessMean=max(leave_one_year) if leave_one_year else None,
            pre2021ExcessMean=mean(row['excessReturn10'] for row in outcomes if row['entryYear'] < '2021'),
            from2021ExcessMean=mean(row['excessReturn10'] for row in outcomes if row['entryYear'] >= '2021'))

    return dict(method=dict(
        horizon=first['horizon'], source=metadata['source'], asOf=metadata['as_of'],
        signalDefinition='New rank-1 transitions; initial observed state excluded',
        primaryControl='All same-ETF same-entry-year days with finite score and a complete valid forward window; includes rank-1 days',
        secondaryControl='Same as primary, excluding every day on which that ETF ranks first',
        weighting='Year control means and win rates weighted by complete signal counts; overall also event-weighted across ETFs',
        prices=first['priceBasis'],
        limits=['Descriptive gross outcome contrast, not portfolio alpha or a causal effect',
                'Ten-day windows overlap, controls can overlap signals, and events cluster; no iid significance test',
                'Same-year matching does not match month, volatility or market regime; no future path used to construct the trading signal',
                'Complete-window selection excludes any paused, missing or inactive future day in both groups',
                'Year matching and control availability are retrospective research, not a live selection rule'],
        firstEventStatusCounts=counts_by(first['events'], ['forwardStatus']),
        dailyStatusCounts=counts_by(daily['events'], ['forwardStatus']),
        excludedInitialStates=sum(event['initialObservedState'] for event in first['events']), completeTransitions=len(events)),
        overall=[summarize(control) for control in ['all_days', 'not_rank1']],
        summary=[summarize(control, code) for control in ['all_days', 'not_rank1'] for code in sorted(metadata['codes'])],
        byYear=by_year, matched=matched)


def run(input_dir=HERE, output=HERE / 'results'):
    input_dir, output = Path(input_dir), Path(output)
    result = compare(read_json(input_dir / 'fit_metadata.json'), read_csv(input_dir / 'fit_signals.csv'),
                     read_csv(input_dir / 'fit_market.csv'))
    output.mkdir(parents=True, exist_ok=True)
    for filename, key in [('summary', 'summary'), ('by_year', 'byYear'), ('events', 'matched'), ('overall', 'overall')]:
        write_csv(output / f'fit_control_{filename}.csv', result[key])
    write_json(output / 'fit_control_method.json', result['method'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=HERE)
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'results')
    args = parser.parse_args()
    result = run(args.input, args.output)
    print(json_text({key: result[key] for key in ['method', 'overall', 'summary']}), end='')
