// Fixed-horizon study of first-place events from the matching JoinQuant export.
// node study_etf_fit_forward.mjs [export-directory] [output-directory]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { quantile } from './study_etf_fit.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const horizon = 10;
export function readCsv(file) {
  const text = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '');
  const rows = []; let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; } else quoted = !quoted;
    } else if (c === ',' && !quoted) { row.push(cell); cell = ''; }
    else if ((c === '\n' || c === '\r') && !quoted) {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); if (row.some(value => value !== '')) rows.push(row);
      row = []; cell = '';
    } else cell += c;
  }
  if (quoted) throw new Error(`Unclosed CSV field: ${file}`);
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const header = rows.shift();
  return rows.map(values => {
    if (values.length !== header.length) throw new Error(`CSV width mismatch: ${file}`);
    return Object.fromEntries(header.map((key, i) => [key, values[i]]));
  });
}
const number = value => value !== '' && value != null && Number.isFinite(Number(value)) ? Number(value) : null;
const mean = values => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
const validPrice = row => row && number(row.close_raw) > 0 && number(row.close_post) > 0;

export function evaluate(metadata, signalRows, marketRows, { allDays = false } = {}) {
  if (metadata.schema_version !== 1 || metadata.source !== 'JoinQuant strategy backtest export'
      || metadata.export_complete !== true || metadata.errors?.length) {
    throw new Error('Incomplete or unsupported JoinQuant export; inspect fit_metadata.json');
  }
  const expected = new Set(metadata.codes), signalDays = new Map(), market = new Map();
  for (const row of signalRows) {
    if (row.date > metadata.as_of || !expected.has(row.code)) throw new Error('Signal outside export boundary');
    if (!signalDays.has(row.date)) signalDays.set(row.date, new Map());
    const items = signalDays.get(row.date);
    if (items.has(row.code)) throw new Error('Duplicate signal key');
    items.set(row.code, row);
  }
  for (const row of marketRows) {
    if (row.date > metadata.as_of || !expected.has(row.code)) throw new Error('Market data outside export boundary');
    const key = `${row.date}|${row.code}`;
    if (market.has(key)) throw new Error('Duplicate market key');
    market.set(key, row);
  }
  const calendar = [...signalDays.keys()].sort();
  if (!calendar.length) throw new Error('Empty signals');
  if (metadata.expected_signal_dates
      && JSON.stringify(calendar) !== JSON.stringify(metadata.expected_signal_dates)) {
    throw new Error('Signal calendar has missing or additional days');
  }
  const tops = calendar.map(date => {
    const rows = [...signalDays.get(date).values()];
    if (rows.length !== expected.size) throw new Error(`Incomplete pool snapshot: ${date}`);
    const top = rows.filter(row => number(row.rank) === 1);
    if (top.length > 1) throw new Error(`Multiple first-place assets: ${date}`);
    if (top.length && number(top[0].eligible) !== 1) throw new Error('First place is ineligible');
    return top[0] || null;
  });
  const allMarketDates = [...new Set(marketRows.map(row => row.date))].sort();
  const events = [];
  for (let i = 0; i < calendar.length; i++) {
    const candidates = allDays
      ? [...signalDays.get(calendar[i]).values()].filter(row => number(row.score) !== null)
      : tops[i] && (i === 0 || tops[i - 1]?.code !== tops[i].code) ? [tops[i]] : [];
    for (const signal of candidates) {
    const event = { date: calendar[i], code: signal.code,
      initialObservedState: i === 0,
      entryYear: calendar[i].slice(0, 4), observedRankDays: 1, rankRunCensored: false,
      forwardStatus: 'complete', return10: null, mae3: null, mae10: null, mfe10: null,
      volatility20: null, riskScaledReturn10: null, reversalLag2Sigma: null,
      reversalObserved: false };
    let endRank = i + 1;
    while (endRank < tops.length && tops[endRank]?.code === signal.code) endRank++;
    event.observedRankDays = endRank - i;
    event.rankRunCensored = endRank === tops.length;
    const entry = market.get(`${event.date}|${event.code}`);
    const decisionPrice = number(signal.decision_price);
    if (!validPrice(entry) || !(decisionPrice > 0) || number(signal.paused) !== 0) {
      event.forwardStatus = 'invalid_entry'; events.push(event); continue;
    }
    if (i + horizon >= calendar.length) {
      event.forwardStatus = 'right_censored'; events.push(event); continue;
    }
    const following = calendar.slice(i + 1, i + horizon + 1)
      .map(date => market.get(`${date}|${event.code}`));
    if (following.some(row => !validPrice(row))) {
      event.forwardStatus = 'missing_price'; events.push(event); continue;
    }
    if (following.some(row => number(row.paused) !== 0 || !(number(row.volume) > 0) || !(number(row.money) > 0))) {
      event.forwardStatus = 'paused_or_zero_activity'; events.push(event); continue;
    }
    // Close-post / close-raw only aligns price units for outcome measurement;
    // it is not fed into the decision-time ranking or a trading rule.
    const adjustedEntry = decisionPrice * Number(entry.close_post) / Number(entry.close_raw);
    const returns = following.map(row => Number(row.close_post) / adjustedEntry - 1);
    event.return10 = returns.at(-1);
    event.mae3 = Math.min(0, ...returns.slice(0, 3));
    event.mae10 = Math.min(0, ...returns);
    event.mfe10 = Math.max(0, ...returns);
    const marketIndex = allMarketDates.indexOf(event.date);
    const past = allMarketDates.slice(Math.max(0, marketIndex - 21), marketIndex)
      .map(date => market.get(`${date}|${event.code}`));
    if (past.length === 21 && past.every(row => validPrice(row) && number(row.paused) === 0)) {
      const daily = past.slice(1).map((row, k) => Math.log(Number(row.close_post) / Number(past[k].close_post)));
      const average = mean(daily);
      const sigma = Math.sqrt(daily.reduce((s, value) => s + (value - average) ** 2, 0) / (daily.length - 1));
      if (sigma > 0) {
        event.volatility20 = sigma;
        event.riskScaledReturn10 = event.return10 / (sigma * Math.sqrt(horizon));
        let peak = adjustedEntry, peakIndex = 0;
        for (let j = 0; j < following.length; j++) {
          const price = Number(following[j].close_post);
          if (price > peak) { peak = price; peakIndex = j + 1; }
          if (Math.log(peak / price) >= 2 * sigma) {
            event.reversalObserved = true;
            event.reversalLag2Sigma = j + 1 - peakIndex;
            break;
          }
        }
      }
    }
    events.push(event);
    }
  }
  const group = selected => {
    const complete = selected.filter(event => event.forwardStatus === 'complete');
    const scaled = complete.map(event => event.riskScaledReturn10).filter(value => value !== null);
    return { code: selected[0].code, events: selected.length, complete10: complete.length,
      incomplete10: selected.length - complete.length,
      medianObservedRankDays: quantile(selected.map(event => event.observedRankDays), .5),
      censoredRankRuns: selected.filter(event => event.rankRunCensored).length,
      meanReturn10: mean(complete.map(event => event.return10)),
      medianReturn10: quantile(complete.map(event => event.return10), .5),
      winRate10: complete.length ? complete.filter(event => event.return10 > 0).length / complete.length : null,
      medianMae3: quantile(complete.map(event => event.mae3), .5),
      medianMae10: quantile(complete.map(event => event.mae10), .5),
      medianMfe10: quantile(complete.map(event => event.mfe10), .5),
      volatilityMatchedCount: scaled.length, meanRiskScaledReturn10: mean(scaled),
      reversalObservedCount: complete.filter(event => event.reversalObserved).length,
      medianObservedReversalLag: quantile(complete.filter(event => event.reversalObserved)
        .map(event => event.reversalLag2Sigma), .5) };
  };
  const codes = [...new Set(events.map(event => event.code))].sort();
  const summary = codes.map(code => group(events.filter(event => event.code === code)));
  const byYear = [];
  for (const code of codes) {
    const selected = events.filter(event => event.code === code);
    for (const year of [...new Set(selected.map(event => event.entryYear))].sort()) {
      byYear.push({ entryYear: year, ...group(selected.filter(event => event.entryYear === year)) });
    }
  }
  return { horizon, priceBasis: 'JoinQuant fq=post aligned to decision-time raw price; gross close-based outcomes, not executed trade returns',
    eventDefinition: allDays ? 'All pool days with a finite score (history available), irrespective of rank or eligibility'
      : 'First entry into eligible rank 1, including initial observed state',
    reversalDefinition: 'First decline of 2 pre-signal daily log-return standard deviations from the running observed peak within 10 days; no hit means unobserved, not never reverses',
    events, summary, byYear };
}

export function csv(rows) {
  if (!rows.length) return '';
  const keys = Object.keys(rows[0]);
  const escape = value => value == null ? '' : String(value).includes(',') ? `"${String(value).replaceAll('"', '""')}"` : String(value);
  return '\uFEFF' + [keys.join(','), ...rows.map(row => keys.map(key => escape(row[key])).join(','))].join('\n') + '\n';
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const input = process.argv[2] || here, output = process.argv[3] || path.join(here, 'results');
  const result = evaluate(JSON.parse(fs.readFileSync(path.join(input, 'fit_metadata.json'), 'utf8')),
    readCsv(path.join(input, 'fit_signals.csv')), readCsv(path.join(input, 'fit_market.csv')));
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, 'fit_forward_events.csv'), csv(result.events));
  fs.writeFileSync(path.join(output, 'fit_forward_summary.csv'), csv(result.summary));
  fs.writeFileSync(path.join(output, 'fit_forward_by_year.csv'), csv(result.byYear));
  console.log(JSON.stringify({ ...result, events: result.events.length, byYear: result.byYear.length }, null, 2));
}
