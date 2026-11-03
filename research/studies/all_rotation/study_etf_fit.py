"""Observed holding-segment outcomes, normalized by entry notional.

Run: python research/studies/etf_rotation/study_etf_fit.py [position.csv] [output-dir]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.analyze_positions import analyze
from research.studies.etf_rotation.study_common import HERE, json_text, mean, quantile, write_csv, write_json


def episode_metrics(episode):
    values = [point['returnOnEntryNotional'] for point in episode['path']]
    if not values or any(not math.isfinite(value) or value <= -1 for value in values):
        raise ValueError(f'Invalid holding path: {episode["index"]}')
    peak, drawdown = 1, 0
    for value in values:
        peak = max(peak, 1 + value)
        drawdown = max(drawdown, 1 - (1 + value) / peak)
    end_return = episode['netPnl'] / episode['entryNotional']
    mfe, mae = max(0, *values), min(0, *values)
    return dict(episode=episode['index'], code=episode['code'], name=episode['name'],
                entry=episode['entry'], exit=episode.get('exit'), closed=episode['closed'],
                entryYear=episode['entry'][:4], closeHoldingDays=episode['closeHoldingDays'],
                elapsedTradingDays=(episode['exitDayIndex'] - episode['entryDayIndex']
                                    if episode['closed'] else None),
                netPnl=episode['netPnl'], returnOnEntryNotional=end_return,
                observedMfe=mfe, observedMae=mae, observedMaxDrawdown=drawdown,
                observedPeakGiveback=mfe - end_return, logContribution=episode['logContribution'])


def summarize(metrics):
    closed = [item for item in metrics if item['closed']]
    winners = [item for item in closed if item['returnOnEntryNotional'] > 0]
    losers = [item for item in closed if item['returnOnEntryNotional'] < 0]
    positive = sum(item['returnOnEntryNotional'] for item in winners)
    negative = -sum(item['returnOnEntryNotional'] for item in losers)
    years = {}
    for item in closed:
        years.setdefault(item['entryYear'], []).append(item['returnOnEntryNotional'])
    return dict(
        code=metrics[0]['code'] if metrics else None, name=metrics[0]['name'] if metrics else None,
        closedEpisodes=len(closed), openEpisodes=len(metrics) - len(closed), sampleYears=len(years),
        positiveMeanYears=sum(mean(values) > 0 for values in years.values()),
        winRate=len(winners) / len(closed) if closed else None,
        meanReturn=mean(item['returnOnEntryNotional'] for item in closed),
        medianReturn=quantile((item['returnOnEntryNotional'] for item in closed), .5),
        returnP10=quantile((item['returnOnEntryNotional'] for item in closed), .1),
        returnP90=quantile((item['returnOnEntryNotional'] for item in closed), .9),
        returnProfitFactor=positive / negative if negative > 0 else None,
        medianHoldingDays=quantile((item['closeHoldingDays'] for item in closed), .5),
        meanWinnerHoldingDays=mean(item['closeHoldingDays'] for item in winners),
        meanLoserHoldingDays=mean(item['closeHoldingDays'] for item in losers),
        medianObservedMae=quantile((item['observedMae'] for item in closed), .5),
        maeP10=quantile((item['observedMae'] for item in closed), .1),
        medianObservedMfe=quantile((item['observedMfe'] for item in closed), .5),
        medianObservedMaxDrawdown=quantile((item['observedMaxDrawdown'] for item in closed), .5),
        medianObservedGiveback=quantile((item['observedPeakGiveback'] for item in closed), .5),
        quickExitWithin3DaysRate=(sum(item['elapsedTradingDays'] <= 3 for item in closed) / len(closed)
                                 if closed else None),
        quickLosingExitWithin3DaysRate=(sum(item['elapsedTradingDays'] <= 3 and item['returnOnEntryNotional'] < 0
                                          for item in closed) / len(closed) if closed else None))


def run(input_file=HERE / 'position.csv', output=HERE / 'results'):
    data = analyze(input_file, include_episodes=True)
    episodes = [episode_metrics(item) for item in data['episodes']]
    codes = sorted({item['code'] for item in episodes})
    summary = [summarize([item for item in episodes if item['code'] == code]) for code in codes]
    by_year = []
    for code in codes:
        selected = [item for item in episodes if item['code'] == code]
        for year in sorted({item['entryYear'] for item in selected}):
            by_year.append({'entryYear': year, **summarize([item for item in selected if item['entryYear'] == year])})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for filename, rows in [('fit_summary.csv', summary), ('fit_by_year.csv', by_year), ('fit_episodes.csv', episodes)]:
        write_csv(output / filename, rows)
    result = dict(
        source=str(input_file), period=data['dates'], closedEpisodes=data['closedTrades'],
        openEpisodes=len(data['openPositions']), reconciliation=data['checks'],
        method='Observed actual holding segments, entry notional normalized; close snapshots plus reconstructed exit mark, no unobserved intraday prices',
        limits=['Open segment excluded from closed-trade distributions',
                'Year groups use entry year, not calendar-year account profit',
                'Short holding segments are not fixed 3/10-day forward observations',
                'Rank persistence and post-sale 10-day paths require full signals and adjusted market export',
                'Current local daily cache lacks trusted adjustments for all 15 members'], summary=summary)
    write_json(output / 'fit_method.json', result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=HERE / 'position.csv')
    parser.add_argument('output', nargs='?', type=Path, default=HERE / 'results')
    args = parser.parse_args()
    print(json_text(run(args.input, args.output)), end='')
