// Actual holding-segment study. No trading rules or source data are changed.
// node research/studies/etf_rotation/study_etf_fit.mjs [position.csv] [output-dir]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
export function quantile(values, probability) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return null;
  const at = (sorted.length - 1) * probability;
  const lo = Math.floor(at), hi = Math.ceil(at);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (at - lo);
}
const mean = values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;

export function episodeMetrics(episode) {
  const values = episode.path.map(point => point.returnOnEntryNotional);
  if (!values.length || values.some(value => !Number.isFinite(value) || value <= -1)) {
    throw new Error(`Invalid holding path: ${episode.index}`);
  }
  let peak = 1, maxDrawdown = 0;
  for (const value of values) {
    peak = Math.max(peak, 1 + value);
    maxDrawdown = Math.max(maxDrawdown, 1 - (1 + value) / peak);
  }
  const endReturn = episode.netPnl / episode.entryNotional;
  const mfe = Math.max(0, ...values), mae = Math.min(0, ...values);
  return {
    episode: episode.index, code: episode.code, name: episode.name,
    entry: episode.entry, exit: episode.exit || null, closed: episode.closed,
    entryYear: episode.entry.slice(0, 4), closeHoldingDays: episode.closeHoldingDays,
    elapsedTradingDays: episode.closed ? episode.exitDayIndex - episode.entryDayIndex : null,
    netPnl: episode.netPnl, returnOnEntryNotional: endReturn,
    observedMfe: mfe, observedMae: mae, observedMaxDrawdown: maxDrawdown,
    observedPeakGiveback: mfe - endReturn,
    logContribution: episode.logContribution,
  };
}

export function summarize(metrics) {
  const closed = metrics.filter(item => item.closed);
  const winners = closed.filter(item => item.returnOnEntryNotional > 0);
  const losers = closed.filter(item => item.returnOnEntryNotional < 0);
  const positiveReturns = winners.reduce((s, item) => s + item.returnOnEntryNotional, 0);
  const negativeReturns = -losers.reduce((s, item) => s + item.returnOnEntryNotional, 0);
  const years = new Map();
  for (const item of closed) {
    if (!years.has(item.entryYear)) years.set(item.entryYear, []);
    years.get(item.entryYear).push(item.returnOnEntryNotional);
  }
  return {
    code: metrics[0]?.code, name: metrics[0]?.name,
    closedEpisodes: closed.length, openEpisodes: metrics.length - closed.length,
    sampleYears: years.size,
    positiveMeanYears: [...years.values()].filter(values => mean(values) > 0).length,
    winRate: closed.length ? winners.length / closed.length : null,
    meanReturn: mean(closed.map(item => item.returnOnEntryNotional)),
    medianReturn: quantile(closed.map(item => item.returnOnEntryNotional), .5),
    returnP10: quantile(closed.map(item => item.returnOnEntryNotional), .1),
    returnP90: quantile(closed.map(item => item.returnOnEntryNotional), .9),
    returnProfitFactor: negativeReturns > 0 ? positiveReturns / negativeReturns : null,
    medianHoldingDays: quantile(closed.map(item => item.closeHoldingDays), .5),
    meanWinnerHoldingDays: mean(winners.map(item => item.closeHoldingDays)),
    meanLoserHoldingDays: mean(losers.map(item => item.closeHoldingDays)),
    medianObservedMae: quantile(closed.map(item => item.observedMae), .5),
    maeP10: quantile(closed.map(item => item.observedMae), .1),
    medianObservedMfe: quantile(closed.map(item => item.observedMfe), .5),
    medianObservedMaxDrawdown: quantile(closed.map(item => item.observedMaxDrawdown), .5),
    medianObservedGiveback: quantile(closed.map(item => item.observedPeakGiveback), .5),
    quickExitWithin3DaysRate: closed.length
      ? closed.filter(item => item.elapsedTradingDays <= 3).length / closed.length : null,
    quickLosingExitWithin3DaysRate: closed.length
      ? closed.filter(item => item.elapsedTradingDays <= 3 && item.returnOnEntryNotional < 0).length / closed.length : null,
  };
}

function csv(rows) {
  if (!rows.length) return '';
  const keys = Object.keys(rows[0]);
  const escape = value => {
    if (value == null) return '';
    const text = String(value);
    return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  return '\uFEFF' + [keys.join(','), ...rows.map(row => keys.map(key => escape(row[key])).join(','))].join('\n') + '\n';
}

export function run(input, output) {
  const data = JSON.parse(execFileSync(process.execPath,
    [path.join(here, 'analyze_positions.mjs'), input, '--episodes'],
    { encoding: 'utf8', maxBuffer: 20 * 1024 * 1024 }));
  const episodes = data.episodes.map(episodeMetrics);
  const codes = [...new Set(episodes.map(item => item.code))].sort();
  const summary = codes.map(code => summarize(episodes.filter(item => item.code === code)));
  const byYear = [];
  for (const code of codes) {
    const selected = episodes.filter(item => item.code === code);
    for (const year of [...new Set(selected.map(item => item.entryYear))].sort()) {
      byYear.push({ entryYear: year, ...summarize(selected.filter(item => item.entryYear === year)) });
    }
  }
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, 'fit_summary.csv'), csv(summary));
  fs.writeFileSync(path.join(output, 'fit_by_year.csv'), csv(byYear));
  fs.writeFileSync(path.join(output, 'fit_episodes.csv'), csv(episodes));
  const result = {
    source: input, period: data.dates, closedEpisodes: data.closedTrades,
    openEpisodes: data.openPositions.length, reconciliation: data.checks,
    method: 'Observed actual holding segments, entry notional normalized; close snapshots plus reconstructed exit mark, no unobserved intraday prices',
    limits: ['Open segment excluded from closed-trade distributions',
      'Year groups use entry year, not calendar-year account profit',
      'Short holding segments are not fixed 3/10-day forward observations',
      'Rank persistence and post-sale 10-day paths require full signals and adjusted market export',
      'Current local daily cache lacks trusted adjustments for all 15 members'],
    summary,
  };
  fs.writeFileSync(path.join(output, 'fit_method.json'), JSON.stringify(result, null, 2) + '\n');
  return result;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = run(process.argv[2] || path.join(here, 'position.csv'),
    process.argv[3] || path.join(here, 'results'));
  console.log(JSON.stringify(result, null, 2));
}
