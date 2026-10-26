"""Reconcile single-position closing snapshots with the account's net P/L.

Run: python research/studies/etf_rotation/analyze_positions.py [csv] [--episodes]
Exit marks are reconstructed from NAV changes; they are not order-level prices.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.studies.etf_rotation.study_common import HERE, csv_fields, fee, json_text, number, ratio


STARTING_CASH = 100000
METHOD = ('Single-position close snapshots; incoming fees estimated at 0.02%/min5; '
          'exit net P/L recovered from account change; no order-level exit prices supplied')


def read_positions(file):
    rows = csv_fields(file, 'gb18030')
    if not rows or rows[0][0] != '日期' or len(rows[0]) <= 12 or rows[0][12] != '当日盈亏':
        raise ValueError('Unexpected CSV header')
    by_date = {}

    def numeric(value, label):
        result = number(value)
        if result is None:
            raise ValueError(f'Invalid numeric field: {label}={value}')
        return result

    for row in rows[1:]:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row[0]):
            raise ValueError(f'Invalid date: {row[0]}')
        day = by_date.setdefault(row[0], dict(date=row[0], positions=[], cash=None))
        if row[2] == 'Cash':
            if day['cash'] is not None:
                raise ValueError(f'Duplicate cash row: {row[0]}')
            day['cash'] = numeric(row[7], 'cash')
            continue
        # The export has an unlabelled NAV column before the position weight.
        match = re.fullmatch(r'(.*)\((\d{6}\.(?:XSHG|XSHE))\)', row[2])
        if len(row) != 17 or not match:
            raise ValueError(f'Unexpected security row: {row[0]}')
        day['positions'].append(dict(
            name=match[1], code=match[2], qty=numeric(row[4].removesuffix('股'), 'quantity'),
            value=numeric(row[7], 'position value'), cost=numeric(row[9], 'entry average'),
            reportedPnl=numeric(row[12], 'reported daily P/L'), nav=numeric(row[15], 'account value')))
    if not by_date:
        raise ValueError('Empty position history')
    return sorted(by_date.values(), key=lambda day: day['date'])


def analyze(file=HERE / 'position.csv', *, include_episodes=False):
    days = read_positions(file)
    assets, episodes = {}, []

    def asset(position):
        return assets.setdefault(position['code'], dict(
            code=position['code'], name=position['name'], holdingDays=0, netPnl=0,
            logContribution=0, drawdownPnl=0, drawdownPeakPoints=0, closedTrades=0, closedWinners=0))

    previous_nav = previous_cash = STARTING_CASH
    previous = episode = None
    reported_pnl = cash_error = continuous_error = entry_error = negative_days = negative_ratio = 0
    entry_count = cashflow_error = 0
    peak, peak_date, worst = STARTING_CASH, None, dict(drawdown=0)
    for index, day in enumerate(days):
        if day['cash'] is None or len(day['positions']) > 1:
            raise ValueError(f'Unsupported holdings: {day["date"]}')
        current = next(iter(day['positions']), None)
        nav = day['nav'] = current['nav'] if current else day['cash']
        if nav <= 0:
            raise ValueError(f'Nonpositive NAV: {day["date"]}')
        reconciliation = abs(nav - day['cash'] - (current['value'] if current else 0))
        cash_error = max(cash_error, reconciliation)
        if reconciliation > .011:
            raise ValueError(f'NAV/cash mismatch: {day["date"]}')
        delta, allocations = nav - previous_nav, []
        changed = (current or {}).get('code') != (previous or {}).get('code')
        if changed:
            buy_fee = fee(current['qty'] * current['cost']) if current else 0
            incoming = current['reportedPnl'] - buy_fee if current else 0
            outgoing = delta - incoming
            if previous:
                allocations.append(dict(code=previous['code'], pnl=outgoing, episodeIndex=episode['index']))
                episode['netPnl'] += outgoing
                sold_cash = (day['cash'] - previous_cash
                             + (current['qty'] * current['cost'] if current else 0) + buy_fee)
                episode['sellFeeEstimate'] = fee(sold_cash / (1 - .0002))
                episode['beforeFeePnlEstimate'] = (episode['netPnl'] + episode['buyFeeEstimate']
                                                   + episode['sellFeeEstimate'])
                cashflow_pnl = (sold_cash - episode['entryNotional'] - episode['buyFeeEstimate']
                                + previous_cash - episode['entryCashAfter'])
                cashflow_error = max(cashflow_error, abs(episode['netPnl'] - cashflow_pnl))
                if abs(episode['netPnl'] - cashflow_pnl) > .011:
                    raise ValueError(f'Episode cashflow mismatch: {day["date"]}')
                episode.update(exit=day['date'], exitDayIndex=index, closed=True)
                asset(previous)['closedTrades'] += 1
                asset(previous)['closedWinners'] += int(episode['netPnl'] > 0)
                episode = None
            else:
                entry_count += 1
                entry_error = max(entry_error, abs(outgoing))
                if abs(outgoing) > .011:
                    raise ValueError(f'Unexplained cash-entry change: {day["date"]}')
            if current:
                episode = dict(index=len(episodes), code=current['code'], name=current['name'], entry=day['date'],
                               entryDayIndex=index, closeHoldingDays=0, path=[],
                               entryNotional=current['qty'] * current['cost'], buyFeeEstimate=buy_fee,
                               entryCashAfter=day['cash'], netPnl=incoming, logContribution=0, closed=False)
                episodes.append(episode)
                allocations.append(dict(code=current['code'], pnl=incoming, episodeIndex=episode['index']))
        elif current:
            continuous_error = max(continuous_error, abs(delta - current['reportedPnl']))
            if abs(delta - current['reportedPnl']) > .011:
                raise ValueError(f'Unexplained holding-day change: {day["date"]}')
            allocations.append(dict(code=current['code'], pnl=delta, episodeIndex=episode['index']))
            episode['netPnl'] += delta
        elif abs(delta) > .011:
            raise ValueError(f'Unexplained cash-only change: {day["date"]}')
        if current:
            asset(current)['holdingDays'] += 1
            episode['closeHoldingDays'] += 1
        daily_return = delta / previous_nav
        link = math.log1p(daily_return) / daily_return if daily_return else 1
        for item in allocations:
            entry = assets.get(item['code']) or asset(current)
            contribution = item['pnl'] / previous_nav * link
            entry['netPnl'] += item['pnl']
            entry['logContribution'] += contribution
            holding = episodes[item['episodeIndex']]
            holding['logContribution'] += contribution
            holding['path'].append(dict(date=day['date'], dayIndex=index,
                                       returnOnEntryNotional=holding['netPnl'] / holding['entryNotional']))
        day.update(allocations=allocations, previousNav=previous_nav)
        reported_pnl += current['reportedPnl'] if current else 0
        if day['cash'] < 0:
            negative_days += 1
            negative_ratio = max(negative_ratio, -day['cash'] / nav)
        if nav > peak:
            peak, peak_date = nav, day['date']
        drawdown = nav / peak - 1
        if drawdown < worst['drawdown']:
            worst = dict(drawdown=drawdown, peakDate=peak_date, troughDate=day['date'], peak=peak, trough=nav)
        previous_nav, previous_cash, previous = nav, day['cash'], current
    recovery_date = None
    if 'troughDate' in worst:
        for day in days:
            if day['date'] > worst['troughDate'] and day['nav'] >= worst['peak'] and recovery_date is None:
                recovery_date = day['date']
            if ((worst['peakDate'] is None or day['date'] > worst['peakDate'])
                    and day['date'] <= worst['troughDate']):
                for item in day['allocations']:
                    assets[item['code']]['drawdownPnl'] += item['pnl']
                    assets[item['code']]['drawdownPeakPoints'] += item['pnl'] / worst['peak'] * 100
    profit = previous_nav - STARTING_CASH
    all_assets = list(assets.values())
    sum_net = sum(item['netPnl'] for item in all_assets)
    sum_log = sum(item['logContribution'] for item in all_assets)
    log_error = sum_log - math.log(previous_nav / STARTING_CASH)
    if abs(sum_net - profit) > max(1, entry_count) * .011:
        raise ValueError('Attribution does not reconcile')
    if abs(log_error) > .00001:
        raise ValueError('Log contributions do not reconcile')
    closed = [item for item in episodes if item['closed']]
    sorted_closed = sorted(closed, key=lambda item: -item['netPnl'])
    sorted_growth = sorted(closed, key=lambda item: -item['logContribution'])
    result = dict(
        source=str(file), method=METHOD, dates=[days[0]['date'], days[-1]['date']], tradingDays=len(days),
        endingNav=previous_nav, netProfit=profit, reportedPnl=reported_pnl, reportedPnlExcess=reported_pnl - profit,
        checks=dict(cashReconciliationMax=cash_error, continuousPnlErrorMax=continuous_error,
                    cashEntryFeeErrorMax=entry_error, episodeCashflowErrorMax=cashflow_error,
                    netAttributionError=sum_net - profit, logAttributionError=log_error),
        negativeCashDays=negative_days, negativeCashRatioMax=negative_ratio,
        worst={**worst, 'recoveryDate': recovery_date}, closedTrades=len(closed),
        winners=sum(item['netPnl'] > 0 for item in closed), losers=sum(item['netPnl'] < 0 for item in closed),
        beforeFeeWinnersEstimate=sum(item['beforeFeePnlEstimate'] > 0 for item in closed),
        beforeFeeLosersEstimate=sum(item['beforeFeePnlEstimate'] < 0 for item in closed),
        top5ClosedPnlShare=ratio(sum(item['netPnl'] for item in sorted_closed[:5]), profit),
        top10ClosedPnlShare=ratio(sum(item['netPnl'] for item in sorted_closed[:10]), profit),
        top5ClosedLogGrowthShare=ratio(sum(item['logContribution'] for item in sorted_growth[:5]), sum_log),
        top10ClosedLogGrowthShare=ratio(sum(item['logContribution'] for item in sorted_growth[:10]), sum_log),
        assets=[{**item, 'netProfitShare': ratio(item['netPnl'], profit),
                 'logGrowthShare': ratio(item['logContribution'], sum_log)}
                for item in sorted(all_assets, key=lambda item: -item['netPnl'])],
        topTrades=sorted_closed[:10], topGrowthTrades=sorted_growth[:10],
        openPositions=[item for item in episodes if not item['closed']])
    if include_episodes:
        result.update(episodes=episodes, calendar=[day['date'] for day in days])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=HERE / 'position.csv')
    parser.add_argument('--episodes', action='store_true')
    args = parser.parse_args()
    print(json_text(analyze(args.input, include_episodes=args.episodes)), end='')
