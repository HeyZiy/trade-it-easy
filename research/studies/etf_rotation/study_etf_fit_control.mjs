// Same-ETF, same-entry-year descriptive control of the fixed ten-day outcome.
// node study_etf_fit_control.mjs [export-directory] [output-directory]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { evaluate, readCsv, csv } from './study_etf_fit_forward.mjs';
import { quantile } from './study_etf_fit.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const mean = values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
const stats = rows => ({ meanReturn: mean(rows.map(x => x.return10)),
  medianReturn: quantile(rows.map(x => x.return10), .5),
  winRate: mean(rows.map(x => Number(x.return10 > 0))),
  meanMae3: mean(rows.map(x => x.mae3)), meanMae10: mean(rows.map(x => x.mae10)) });

export function compare(metadata, signals, market) {
  const first = evaluate(metadata, signals, market);
  const daily = evaluate(metadata, signals, market, { allDays: true });
  // Exclude left-censored initial states: they are not observed transitions.
  const events = first.events.filter(x => !x.initialObservedState && x.forwardStatus === 'complete');
  const validDaily = daily.events.filter(x => x.forwardStatus === 'complete');
  const ranks = new Map(signals.map(x => [`${x.date}|${x.code}`, x.rank]));
  const byYear = [], matched = [];
  for (const control of ['all_days', 'not_rank1']) {
    const controls = validDaily.filter(x => control === 'all_days' || Number(ranks.get(`${x.date}|${x.code}`)) !== 1);
    const groups = new Map();
    for (const row of controls) {
      const key = `${row.code}|${row.entryYear}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    for (const key of [...new Set(events.map(x => `${x.code}|${x.entryYear}`))].sort()) {
      const [code, entryYear] = key.split('|');
      const selected = events.filter(x => x.code === code && x.entryYear === entryYear);
      const ordinary = groups.get(key) || [], a = stats(selected), b = stats(ordinary);
      byYear.push({ control, code, entryYear, signalCount: selected.length, controlDays: ordinary.length,
        status: ordinary.length ? 'matched' : 'no_control',
        signalMeanReturn10: a.meanReturn, controlMeanReturn10: b.meanReturn,
        excessMeanReturn10: ordinary.length ? a.meanReturn - b.meanReturn : null,
        signalMedianReturn10: a.medianReturn, controlMedianReturn10: b.medianReturn,
        signalWinRate10: a.winRate, controlWinRate10: b.winRate,
        excessWinRate10: ordinary.length ? a.winRate - b.winRate : null,
        signalMeanMae3: a.meanMae3, controlMeanMae3: b.meanMae3,
        signalMeanMae10: a.meanMae10, controlMeanMae10: b.meanMae10 });
      if (ordinary.length) for (const event of selected) matched.push({ control, code, date: event.date,
        entryYear, return10: event.return10, controlMeanReturn10: b.meanReturn,
        excessReturn10: event.return10 - b.meanReturn });
    }
  }
  const summarize = (control, code = null) => {
    const rows = byYear.filter(x => x.control === control && (code === null || x.code === code) && x.status === 'matched');
    const n = rows.reduce((sum, row) => sum + row.signalCount, 0);
    const weighted = key => n ? rows.reduce((sum, row) => sum + row[key] * row.signalCount, 0) / n : null;
    const outcomes = matched.filter(x => x.control === control && (code === null || x.code === code));
    const years = [...new Set(rows.map(x => x.entryYear))];
    const annual = years.map(year => ({ year, values: outcomes.filter(x => x.entryYear === year).map(x => x.excessReturn10) }));
    const leaveOneYear = years.length > 1 ? years.map(year => mean(outcomes.filter(x => x.entryYear !== year).map(x => x.excessReturn10))) : [];
    return { control, code: code || 'ALL_EVENT_WEIGHTED', matchedSignals: n,
      unmatchedSignals: byYear.filter(x => x.control === control && (code === null || x.code === code) && x.status !== 'matched')
        .reduce((sum, row) => sum + row.signalCount, 0),
      controlDays: rows.reduce((sum, row) => sum + row.controlDays, 0),
      signalMeanReturn10: weighted('signalMeanReturn10'), matchedControlMeanReturn10: weighted('controlMeanReturn10'),
      excessMeanReturn10: weighted('excessMeanReturn10'),
      medianEventExcessReturn10: quantile(outcomes.map(x => x.excessReturn10), .5),
      signalWinRate10: weighted('signalWinRate10'), matchedControlWinRate10: weighted('controlWinRate10'),
      excessWinRate10: weighted('excessWinRate10'),
      signalMeanMae3: weighted('signalMeanMae3'), matchedControlMeanMae3: weighted('controlMeanMae3'),
      signalMeanMae10: weighted('signalMeanMae10'), matchedControlMeanMae10: weighted('controlMeanMae10'),
      sampleYears: years.length, positiveExcessYears: annual.filter(x => mean(x.values) > 0).length,
      minLeaveOneYearExcessMean: leaveOneYear.length ? Math.min(...leaveOneYear) : null,
      maxLeaveOneYearExcessMean: leaveOneYear.length ? Math.max(...leaveOneYear) : null,
      pre2021ExcessMean: mean(outcomes.filter(x => x.entryYear < '2021').map(x => x.excessReturn10)),
      from2021ExcessMean: mean(outcomes.filter(x => x.entryYear >= '2021').map(x => x.excessReturn10)) };
  };
  const summary = ['all_days', 'not_rank1'].flatMap(control => [...metadata.codes].sort().map(code => summarize(control, code)));
  const overall = ['all_days', 'not_rank1'].map(control => summarize(control));
  const statusCounts = rows => rows.reduce((counts, row) => {
    counts[row.forwardStatus] = (counts[row.forwardStatus] || 0) + 1; return counts;
  }, {});
  return { method: { horizon: first.horizon, source: metadata.source, asOf: metadata.as_of,
    signalDefinition: 'New rank-1 transitions; initial observed state excluded',
    primaryControl: 'All same-ETF same-entry-year days with finite score and a complete valid forward window; includes rank-1 days',
    secondaryControl: 'Same as primary, excluding every day on which that ETF ranks first',
    weighting: 'Year control means and win rates weighted by complete signal counts; overall also event-weighted across ETFs',
    prices: first.priceBasis,
    limits: ['Descriptive gross outcome contrast, not portfolio alpha or a causal effect',
      'Ten-day windows overlap, controls can overlap signals, and events cluster; no iid significance test',
      'Same-year matching does not match month, volatility or market regime; no future path used to construct the trading signal',
      'Complete-window selection excludes any paused, missing or inactive future day in both groups',
      'Year matching and control availability are retrospective research, not a live selection rule'],
    firstEventStatusCounts: statusCounts(first.events), dailyStatusCounts: statusCounts(daily.events),
    excludedInitialStates: first.events.filter(x => x.initialObservedState).length,
    completeTransitions: events.length }, overall, summary, byYear, matched };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const input = process.argv[2] || here, output = process.argv[3] || path.join(here, 'results');
  const result = compare(JSON.parse(fs.readFileSync(path.join(input, 'fit_metadata.json'), 'utf8')),
    readCsv(path.join(input, 'fit_signals.csv')), readCsv(path.join(input, 'fit_market.csv')));
  fs.mkdirSync(output, { recursive: true });
  for (const [name, rows] of Object.entries({ summary: result.summary, by_year: result.byYear, events: result.matched, overall: result.overall })) {
    fs.writeFileSync(path.join(output, `fit_control_${name}.csv`), csv(rows));
  }
  fs.writeFileSync(path.join(output, 'fit_control_method.json'), JSON.stringify(result.method, null, 2) + '\n');
  console.log(JSON.stringify({ method: result.method, overall: result.overall, summary: result.summary }, null, 2));
}
